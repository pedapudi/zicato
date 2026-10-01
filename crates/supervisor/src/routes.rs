//! HTTP route handlers for the watchdog's operational surface: `/statusz`,
//! `/statusz.json`, and `/api/audit/verify`. Every route is a read-only GET.

use crate::action_log::WatchdogLog;
use crate::reader::{self, WorkspacePaths};
use crate::statusz;
use axum::{
    extract::State,
    http::{header, StatusCode},
    response::{Html, IntoResponse, Json, Response},
    routing::get,
    Router,
};
use std::sync::Arc;
use std::time::Instant;
use tracing::warn;

/// Shared server state.
#[derive(Clone)]
pub struct AppState {
    pub paths: WorkspacePaths,
    pub started: Arc<Instant>,
    pub build_version: &'static str,
    /// The port the HTTP server actually bound (after any retry walk).
    pub port: u16,
    /// A build identifier: the crate version, plus a short git SHA when
    /// the build script could resolve one. Always non-empty.
    pub build_id: &'static str,
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
    /// The latest index-vs-canonical divergence-audit result; `/statusz`
    /// surfaces its findings.
    pub divergence_findings: Arc<crate::divergence::DivergenceFindings>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/statusz", get(statusz_html))
        .route("/statusz.json", get(statusz_json))
        .route("/api/audit/verify", get(audit_verify))
        .with_state(state)
}

/// Build the `/statusz` view from current state.
fn build_statusz_view(s: &AppState) -> statusz::StatuszView {
    let identity = statusz::SupervisorIdentity {
        version: s.build_version,
        build: s.build_id,
        port: s.port,
        uptime_seconds: s.started.elapsed().as_secs(),
        workspace: s.paths.workspace.display().to_string(),
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
        &s.action_log,
        audit_ledger,
        s.diff_findings.view(),
        s.promotion_gate_findings.view(),
        s.divergence_findings.view(),
    )
}

/// `GET /statusz` — the watchdog's terse self-contained operational page.
async fn statusz_html(State(s): State<AppState>) -> Html<String> {
    Html(statusz::render_html(&build_statusz_view(&s)))
}

/// `GET /statusz.json` — the same operational data as JSON.
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
