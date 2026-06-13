//! HTTP route handlers.
//!
//! GETs are always available; POST control endpoints return 403 when the
//! server was started with `--read-only`.

use crate::action_log::WatchdogLog;
use crate::reader::{self, WorkspacePaths};
use crate::run_log;
use crate::sse;
use crate::static_assets;
use crate::statusz;
use crate::watcher::WatchEvent;
use axum::{
    extract::{Path as AxumPath, Query, State},
    http::{header, StatusCode},
    response::{Html, IntoResponse, Json, Response},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::broadcast;
use tracing::warn;

/// Shared server state.
#[derive(Clone)]
pub struct AppState {
    pub paths: WorkspacePaths,
    pub watch_tx: broadcast::Sender<WatchEvent>,
    pub read_only: bool,
    pub started: Arc<Instant>,
    pub build_version: &'static str,
    /// The port the HTTP server actually bound (after any retry walk).
    pub port: u16,
    /// A build identifier: the crate version, plus a short git SHA when
    /// the build script could resolve one. Always non-empty.
    pub build_id: &'static str,
    /// `true` when started with `--no-dashboard`: the full dashboard
    /// routes are not mounted, but `/statusz` still is.
    pub dashboard_disabled: bool,
    /// Heartbeat staleness threshold the watchdog enforces (seconds);
    /// `/statusz` reports freshness against it.
    pub heartbeat_stale_threshold_seconds: u64,
    /// In-memory ring buffer of recent watchdog escalations, shared with
    /// the watchdog loops. `/statusz` surfaces its contents.
    pub action_log: Arc<WatchdogLog>,
    /// The heartbeat seq-liveness tracker, shared with the watchdog
    /// heartbeat loop. `/statusz` reads (does not advance) it to report the
    /// seq-change age alongside the timestamp age. The loop owns advancement.
    pub seq_liveness: Arc<std::sync::Mutex<crate::watchdog::SeqLiveness>>,
    /// Cumulative torn-write / non-monotonic-seq counters over the canonical
    /// active-tournament JSONL fold. The fold path accumulates into it on
    /// each read; `/statusz` surfaces it.
    pub fold_diagnostics: Arc<crate::fold_stats::FoldDiagnostics>,
    /// The tamper-evident audit ledger, when configured (`--ledger-dir`).
    /// `None` → no ledger. `/api/audit/verify` walks its chain and `/statusz`
    /// surfaces a chain-break indicator.
    pub ledger: Option<Arc<crate::ledger::AuditLedger>>,
    /// The latest diff-containment scan result; `/statusz` surfaces it as a
    /// hard ALERT when any generation escaped its mutable surface.
    pub diff_findings: Arc<crate::diff_containment::DiffContainmentFindings>,
    /// The latest promotion-gatekeeping scan result; `/statusz` surfaces it as
    /// an ALERT when a recorded promotion contradicts its recorded scores.
    pub promotion_gate_findings: Arc<crate::promotion_gate::PromotionGateFindings>,
}

pub fn router(state: AppState) -> Router {
    // The watchdog's own minimal surface. These two routes are mounted
    // unconditionally — they are part of the watchdog, not the dashboard,
    // so they remain reachable even under `--no-dashboard`.
    let mut router = Router::new()
        .route("/statusz", get(statusz_html))
        .route("/statusz.json", get(statusz_json))
        // The audit-ledger verify endpoint is part of the watchdog's own
        // surface (like `/statusz`), so it stays reachable under
        // `--no-dashboard` — an operator must be able to check chain
        // integrity even in watchdog-only mode.
        .route("/api/audit/verify", get(audit_verify));

    if !state.dashboard_disabled {
        // The full dashboard surface — UI, analytical API, SSE, and the
        // control endpoints. Slimmed away in watchdog-only mode.
        router = router
            .route("/", get(serve_root))
            .route("/static/*path", get(serve_static))
            .route("/api/state", get(api_state))
            .route("/api/epoch", get(api_epoch))
            .route("/api/lineage", get(api_lineage))
            .route("/api/run-log", get(api_run_log))
            .route("/api/active-runs", get(api_active_runs))
            .route("/api/active-tournament", get(api_active_tournament))
            .route("/api/tournaments", get(api_tournaments))
            .route(
                "/api/tournaments/:generation_id",
                get(api_tournament_detail),
            )
            .route("/api/health-report", get(api_health_report))
            .route("/api/heartbeat", get(api_heartbeat))
            .route("/api/health", get(api_health))
            .route("/events", get(events))
            .route("/api/control/pause", post(control_pause))
            .route("/api/control/skip-round", post(control_skip_round))
            .route("/api/control/kill/:run_id", post(control_kill))
            .route("/api/control/promote/:generation_id", post(control_promote))
            .route("/api/control/reject/:generation_id", post(control_reject))
            .route("/api/control/brief", post(control_brief))
            // Any unmatched GET is treated as a request for a bundled
            // static asset. This makes `index.html`'s relative references
            // (`style.css`, `app.js`, `icons.svg`) resolve at the
            // document root, where a browser requests them — without it
            // the page loads unstyled and inert. Explicit routes above
            // always win; unknown assets fall through to a 404 inside
            // `static_assets`.
            .fallback(get(serve_fallback));
    }

    router.with_state(state)
}

/// Build the `/statusz` view from current state.
fn build_statusz_view(s: &AppState) -> statusz::StatuszView {
    let identity = statusz::SupervisorIdentity {
        version: s.build_version,
        build: s.build_id,
        port: s.port,
        uptime_seconds: s.started.elapsed().as_secs(),
        workspace: s.paths.workspace.display().to_string(),
        read_only: s.read_only,
        dashboard_disabled: s.dashboard_disabled,
    };
    // Read the watchdog's seq tracker WITHOUT advancing it (the heartbeat
    // loop owns advancement) to report the same seq-change age it decides on.
    let seq_age_seconds = {
        let hb = reader::read_heartbeat(&s.paths);
        let thresholds = crate::watchdog::Thresholds {
            heartbeat_stale_warn: std::time::Duration::from_secs(
                s.heartbeat_stale_threshold_seconds,
            ),
            ..Default::default()
        };
        s.seq_liveness
            .lock()
            .map(|tracker| {
                tracker
                    .snapshot(hb.as_ref(), chrono::Utc::now(), &thresholds)
                    .seq_age_seconds
            })
            .unwrap_or(None)
    };
    // Audit-ledger integrity for the chain-break indicator. When no ledger
    // is configured this is the default not-configured/intact status.
    let audit_ledger = match &s.ledger {
        None => statusz::AuditStatus::default(),
        Some(ledger) => {
            let report = ledger.verify();
            statusz::AuditStatus {
                configured: true,
                intact: report.intact,
                records: report.records,
                first_break_seq: report.first_break_seq,
                break_reason: report.break_reason,
            }
        }
    };
    statusz::build_statusz(
        &s.paths,
        &identity,
        s.heartbeat_stale_threshold_seconds,
        seq_age_seconds,
        s.fold_diagnostics.view(),
        &s.action_log,
        audit_ledger,
        s.diff_findings.view(),
        s.promotion_gate_findings.view(),
    )
}

/// `GET /statusz` — the watchdog's terse self-contained operational page.
/// Always mounted, including under `--no-dashboard`.
async fn statusz_html(State(s): State<AppState>) -> Html<String> {
    Html(statusz::render_html(&build_statusz_view(&s)))
}

/// `GET /statusz.json` — the same operational data as JSON.
/// Always mounted, including under `--no-dashboard`.
async fn statusz_json(State(s): State<AppState>) -> Response {
    let view = build_statusz_view(&s);
    match serde_json::to_vec(&view) {
        Ok(bytes) => ([(header::CONTENT_TYPE, "application/json")], bytes).into_response(),
        Err(e) => {
            warn!(error=%e, "statusz serialization failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "statusz error").into_response()
        }
    }
}

/// `GET /api/audit/verify` — walk the tamper-evident audit ledger's
/// hash-chain and report whether it is intact.
///
/// Always 200. When no ledger is configured (`--ledger-dir` unset) the
/// response is `{ "configured": false }`. When one is configured the body
/// carries the full [`crate::ledger::VerifyReport`] plus `configured: true`
/// and the ledger `path`, so an operator can confirm a clean chain or pin
/// the first broken `seq`.
async fn audit_verify(State(s): State<AppState>) -> Json<serde_json::Value> {
    match &s.ledger {
        None => Json(serde_json::json!({ "configured": false })),
        Some(ledger) => {
            let report = ledger.verify();
            let mut body = serde_json::to_value(&report).unwrap_or(serde_json::Value::Null);
            if let Some(obj) = body.as_object_mut() {
                obj.insert("configured".to_string(), serde_json::Value::Bool(true));
                obj.insert(
                    "path".to_string(),
                    serde_json::Value::String(ledger.path().display().to_string()),
                );
            }
            Json(body)
        }
    }
}

async fn serve_root() -> Response {
    static_assets::serve("/")
}

async fn serve_static(AxumPath(path): AxumPath<String>) -> Response {
    static_assets::serve(&path)
}

async fn serve_fallback(uri: axum::http::Uri) -> Response {
    static_assets::serve(uri.path())
}

async fn api_state(State(s): State<AppState>) -> Json<serde_json::Value> {
    let snap = reader::build_snapshot(&s.paths);
    Json(serde_json::to_value(snap).unwrap_or(serde_json::Value::Null))
}

/// `GET /api/epoch` — the current epoch's full evaluation contract.
///
/// Always 200: a missing component degrades to empty/`null`, and no
/// current epoch yields `{ "epoch_id": null }`.
async fn api_epoch(State(s): State<AppState>) -> Json<serde_json::Value> {
    let view = crate::epoch::build_epoch_view(&s.paths);
    Json(serde_json::to_value(view).unwrap_or(serde_json::Value::Null))
}

/// `GET /api/lineage` — every generation directory in every epoch,
/// in-flight and resolved.
///
/// Always 200. Each node is `{generation_id, epoch_id,
/// parent_generation_id, promoted, created_at}` where `promoted` is
/// `null` while the generation is still being scored — so the dashboard
/// Tree can draw `v0` plus the in-flight `v1` mid-run.
async fn api_lineage(State(s): State<AppState>) -> Json<serde_json::Value> {
    let view = reader::build_lineage_view(&s.paths);
    Json(serde_json::to_value(view).unwrap_or_else(|_| serde_json::json!({"generations": []})))
}

/// `GET /api/run-log?limit=N` — the last `N` (default 40) goldfive
/// events from the active run's `events.jsonl`.
///
/// Always 200: no active run falls back to the most recent `events.jsonl`
/// under `epochs/`; a missing file yields `{"events": []}`.
async fn api_run_log(
    State(s): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Json<serde_json::Value> {
    let requested = params
        .get("limit")
        .and_then(|v| v.trim().parse::<usize>().ok());
    let limit = run_log::clamp_limit(requested);
    let log = run_log::build_run_log(&s.paths, limit);
    Json(serde_json::to_value(log).unwrap_or_else(|_| serde_json::json!({"events": []})))
}

/// `GET /api/active-runs` — each active run enriched with a computed
/// deadline fraction (`progress`), `elapsed_seconds`, and
/// `budget_seconds` for the per-entry progress bars.
async fn api_active_runs(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(
        serde_json::to_value(reader::read_active_runs_view(&s.paths))
            .unwrap_or(serde_json::Value::Null),
    )
}

async fn api_active_tournament(State(s): State<AppState>) -> Json<serde_json::Value> {
    // The canonical fold path: accumulate torn-write / seq-gap diagnostics
    // into the shared counter so `/statusz` can surface them.
    let (tournament, stats) = reader::read_active_tournament_with_stats(&s.paths);
    s.fold_diagnostics.record(stats);
    Json(
        tournament
            .map(|t| serde_json::to_value(t).unwrap_or(serde_json::Value::Null))
            .unwrap_or(serde_json::Value::Null),
    )
}

/// `GET /api/tournaments` — the bracket for the current epoch.
///
/// Always 200: a missing `index.db` yields an empty bracket with a
/// `note`, and any query failure degrades to empty rather than 500.
async fn api_tournaments(State(s): State<AppState>) -> Json<serde_json::Value> {
    let view = crate::tournaments::build_bracket(&s.paths);
    Json(serde_json::to_value(view).unwrap_or(serde_json::Value::Null))
}

/// `GET /api/tournaments/:generation_id` — full matchup detail for one
/// challenger generation.
///
/// Always 200: an invalid id, a missing `index.db`, or missing rows all
/// degrade to an empty/`null` payload.
async fn api_tournament_detail(
    State(s): State<AppState>,
    AxumPath(generation_id): AxumPath<String>,
) -> Json<serde_json::Value> {
    if !is_safe_id(&generation_id) {
        // Treat a malformed id as "no such matchup" rather than erroring.
        let empty = serde_json::json!({
            "epoch_id": crate::reader::read_current_epoch(&s.paths),
            "generation_id": generation_id,
            "patches": [],
            "ab_grid": [],
        });
        return Json(empty);
    }
    let detail = crate::tournaments::build_matchup_detail(&s.paths, &generation_id);
    Json(serde_json::to_value(detail).unwrap_or(serde_json::Value::Null))
}

/// `GET /api/health-report` — the latest loop-health report.
///
/// Always 200: no report yields `{healthy:true, findings:[]}`.
async fn api_health_report(State(s): State<AppState>) -> Json<serde_json::Value> {
    let report = crate::tournaments::build_health_report(&s.paths);
    Json(serde_json::to_value(report).unwrap_or(serde_json::Value::Null))
}

async fn api_heartbeat(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(
        reader::read_heartbeat(&s.paths)
            .map(|h| serde_json::to_value(h).unwrap_or(serde_json::Value::Null))
            .unwrap_or(serde_json::Value::Null),
    )
}

#[derive(Serialize)]
struct Health {
    status: &'static str,
    version: &'static str,
    uptime_seconds: u64,
    read_only: bool,
    workspace: String,
    /// The TCP port the server is bound to — for the dashboard footer.
    port: u16,
    /// Build identifier: crate version plus a short git SHA when known.
    /// Always non-empty.
    build: &'static str,
}

async fn api_health(State(s): State<AppState>) -> Json<Health> {
    Json(Health {
        status: "ok",
        version: s.build_version,
        uptime_seconds: s.started.elapsed().as_secs(),
        read_only: s.read_only,
        workspace: s.paths.workspace.display().to_string(),
        port: s.port,
        build: s.build_id,
    })
}

async fn events(State(s): State<AppState>) -> Response {
    let rx = s.watch_tx.subscribe();
    sse::build_sse(s.paths.clone(), rx).into_response()
}

// ---------- control endpoints ----------

fn forbidden_if_read_only(s: &AppState) -> Option<Response> {
    if s.read_only {
        Some((StatusCode::FORBIDDEN, "supervisor is read-only").into_response())
    } else {
        None
    }
}

#[derive(Deserialize, Default)]
struct EmptyBody {
    #[serde(default)]
    #[allow(dead_code)]
    reason: Option<String>,
}

/// Atomic write: `path.tmp` -> rename to `path`.
async fn atomic_write(path: &std::path::Path, contents: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp = path.with_extension({
        let mut e = path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();
        if !e.is_empty() {
            e.push('.');
        }
        e.push_str("tmp");
        e
    });
    tokio::fs::write(&tmp, contents).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

async fn write_control_marker(s: &AppState, name: &str, payload: serde_json::Value) -> Response {
    let path = s.paths.control_dir().join(name);
    let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    match atomic_write(&path, &body).await {
        Ok(_) => (
            StatusCode::ACCEPTED,
            Json(serde_json::json!({
                "accepted": true,
                "path": path.display().to_string()
            })),
        )
            .into_response(),
        Err(e) => {
            warn!(?path, error=%e, "control write failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("write failed: {e}"),
            )
                .into_response()
        }
    }
}

async fn control_pause(State(s): State<AppState>, body: Option<Json<EmptyBody>>) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    let reason = body.and_then(|Json(b)| b.reason).unwrap_or_default();
    write_control_marker(
        &s,
        "pause_epoch",
        serde_json::json!({"reason": reason, "ts": chrono::Utc::now()}),
    )
    .await
}

async fn control_skip_round(State(s): State<AppState>, body: Option<Json<EmptyBody>>) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    let reason = body.and_then(|Json(b)| b.reason).unwrap_or_default();
    write_control_marker(
        &s,
        "skip_round",
        serde_json::json!({"reason": reason, "ts": chrono::Utc::now()}),
    )
    .await
}

async fn control_kill(State(s): State<AppState>, AxumPath(run_id): AxumPath<String>) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    if !is_safe_id(&run_id) {
        return (StatusCode::BAD_REQUEST, "invalid run_id").into_response();
    }
    let path = s.paths.control_dir().join("kill_runs").join(&run_id);
    let payload = serde_json::json!({"run_id": run_id, "ts": chrono::Utc::now()});
    let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    match atomic_write(&path, &body).await {
        Ok(_) => (StatusCode::ACCEPTED, Json(payload)).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("write failed: {e}"),
        )
            .into_response(),
    }
}

async fn control_promote(
    State(s): State<AppState>,
    AxumPath(generation_id): AxumPath<String>,
) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    if !is_safe_id(&generation_id) {
        return (StatusCode::BAD_REQUEST, "invalid generation_id").into_response();
    }
    let path = s.paths.control_dir().join("promote").join(&generation_id);
    let payload = serde_json::json!({"generation_id": generation_id, "ts": chrono::Utc::now()});
    let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    match atomic_write(&path, &body).await {
        Ok(_) => (StatusCode::ACCEPTED, Json(payload)).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("write failed: {e}"),
        )
            .into_response(),
    }
}

async fn control_reject(
    State(s): State<AppState>,
    AxumPath(generation_id): AxumPath<String>,
) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    if !is_safe_id(&generation_id) {
        return (StatusCode::BAD_REQUEST, "invalid generation_id").into_response();
    }
    let path = s.paths.control_dir().join("reject").join(&generation_id);
    let payload = serde_json::json!({"generation_id": generation_id, "ts": chrono::Utc::now()});
    let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    match atomic_write(&path, &body).await {
        Ok(_) => (StatusCode::ACCEPTED, Json(payload)).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("write failed: {e}"),
        )
            .into_response(),
    }
}

async fn control_brief(State(s): State<AppState>, body: String) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    // The on-disk control file keeps its protocol name
    // (`rubric_replacement.txt`) — it is part of the runtime control
    // contract the orchestrator consumes, not a UI-facing label.
    let path = s.paths.control_dir().join("rubric_replacement.txt");
    match atomic_write(&path, body.as_bytes()).await {
        Ok(_) => (
            StatusCode::ACCEPTED,
            Json(serde_json::json!({
                "accepted": true,
                "bytes": body.len(),
                "path": path.display().to_string(),
            })),
        )
            .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("write failed: {e}"),
        )
            .into_response(),
    }
}

/// Conservative ID validator: reject path-traversal / separators / spaces.
pub(crate) fn is_safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 200
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
        && id != "."
        && id != ".."
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_ids() {
        assert!(is_safe_id("gen-abc123"));
        assert!(is_safe_id("run_42"));
        assert!(is_safe_id("2026-05-14_test"));
        assert!(!is_safe_id(""));
        assert!(!is_safe_id(".."));
        assert!(!is_safe_id("a/b"));
        assert!(!is_safe_id("a b"));
        assert!(!is_safe_id("a\0b"));
    }
}
