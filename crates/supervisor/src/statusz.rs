//! `/statusz` — the watchdog's own minimal operational surface.
//!
//! This is deliberately *not* the dashboard. It reports only what the
//! supervisor itself is responsible for and can directly observe:
//!
//!   * the supervisor process: version, build, bound port, uptime,
//!     workspace, read-only flag;
//!   * the process tree it polices: the orchestrator pid and each
//!     in-flight run worker pid;
//!   * per-run wall-clock deadlines and time remaining / overrun;
//!   * heartbeat freshness against the staleness threshold;
//!   * the watchdog actions (SIGTERM/SIGKILL escalations) actually taken
//!     this process lifetime, from the in-memory action ring buffer.
//!
//! No lineage, no scores, no tournament brackets — those are analytical
//! and belong to the (separate) dashboard service. Everything here is a
//! pure function of `(state files, action log, now)` so it can be unit
//! tested without a running server.

use crate::action_log::{Action, WatchdogLog};
use crate::reader::{self, WorkspacePaths};
use crate::state::{ActiveRun, Heartbeat};
use chrono::{DateTime, Utc};
use serde::Serialize;
use std::sync::Arc;

/// The supervisor's own identity and lifetime.
#[derive(Debug, Clone, Serialize)]
pub struct SupervisorInfo {
    /// Crate version.
    pub version: String,
    /// Build identifier (version + short git SHA when known).
    pub build: String,
    /// The TCP port the HTTP server is bound to.
    pub port: u16,
    /// Whole seconds the supervisor process has been up.
    pub uptime_seconds: u64,
    /// Absolute workspace path being watched.
    pub workspace: String,
    /// Whether control-file writes are disabled (`--read-only`).
    pub read_only: bool,
    /// Whether the full dashboard routes are disabled (`--no-dashboard`):
    /// `true` means watchdog-only mode.
    pub dashboard_disabled: bool,
    /// This supervisor process's own pid.
    pub pid: i32,
}

/// Heartbeat freshness from the watchdog's point of view.
#[derive(Debug, Clone, Serialize)]
pub struct HeartbeatStatus {
    /// `true` once a heartbeat file with a timestamp has been seen.
    pub present: bool,
    /// The orchestrator pid carried by the heartbeat, when present.
    pub orchestrator_pid: Option<i32>,
    /// The last heartbeat timestamp (RFC-3339), when present.
    pub last_heartbeat: Option<String>,
    /// Age of the last heartbeat TIMESTAMP in whole seconds. A periodic
    /// timer keeps this fresh even when the orchestrator loop is wedged, so
    /// it is the weaker of the two liveness signals — see `seq_age_seconds`.
    pub age_seconds: Option<i64>,
    /// The orchestrator's progress cursor (`seq`) from the heartbeat, when
    /// present. Advances only on genuine loop progress (RUNTIME-V2 Phase 4).
    pub seq: Option<u64>,
    /// Age in whole seconds since `seq` last *changed*, when the watchdog is
    /// tracking it. This is the stronger liveness signal: it keeps growing
    /// when the loop is wedged even though the timestamp stays fresh. `None`
    /// for a heartbeat that carries no `seq` (pre-Phase-4 writer), in which
    /// case staleness is judged from `age_seconds` (the timestamp).
    pub seq_age_seconds: Option<u64>,
    /// `true` when the age exceeds the staleness threshold. Computed from
    /// `seq_age_seconds` when seq is tracked, else from the timestamp age.
    pub stale: bool,
    /// The staleness threshold the watchdog is using (seconds).
    pub stale_threshold_seconds: u64,
    /// The orchestrator's current phase string, when reported.
    pub phase: Option<String>,
    /// Whole seconds since the orchestrator process started, when known.
    pub orchestrator_uptime_seconds: Option<i64>,
}

/// Per-run deadline status — strictly the supervisor's deadline view.
#[derive(Debug, Clone, Serialize)]
pub struct RunDeadlineStatus {
    pub run_id: String,
    /// The worker pid the watchdog would signal.
    pub pid: Option<i32>,
    /// When the run started (RFC-3339), when known.
    pub started_at: Option<String>,
    /// The wall-clock deadline (RFC-3339), when known.
    pub deadline: Option<String>,
    /// Seconds remaining until the deadline; negative once past it.
    /// `null` when the run carries no deadline.
    pub remaining_seconds: Option<i64>,
    /// `true` when the run is past its deadline.
    pub over_deadline: bool,
    /// Seconds the run is over its deadline (`0` when within deadline,
    /// `null` when the run carries no deadline).
    pub over_by_seconds: Option<i64>,
}

/// The whole `/statusz` payload.
#[derive(Debug, Clone, Serialize)]
pub struct StatuszView {
    /// When this view was assembled (RFC-3339).
    pub generated_at: String,
    pub supervisor: SupervisorInfo,
    pub heartbeat: HeartbeatStatus,
    /// Per-run deadline rows, one per `active_runs/*.json`.
    pub runs: Vec<RunDeadlineStatus>,
    /// Count of runs currently past their deadline.
    pub runs_over_deadline: usize,
    /// A single human-readable summary line.
    pub summary: String,
    /// Recent watchdog escalations from the in-memory ring buffer,
    /// newest last.
    pub watchdog_actions: Vec<Action>,
    /// Cumulative torn-write / non-monotonic-seq counters over the canonical
    /// active-tournament JSONL fold (process lifetime).
    pub fold_diagnostics: crate::fold_stats::FoldDiagnosticsView,
    /// Integrity status of the tamper-evident audit ledger.
    pub audit_ledger: AuditStatus,
    /// Latest diff-containment scan result (record #2).
    pub diff_containment: crate::diff_containment::DiffContainmentView,
    /// Latest promotion-gatekeeping scan result (record #3).
    pub promotion_gate: crate::promotion_gate::PromotionGateView,
}

/// The audit ledger's configured-ness and chain integrity, for `/statusz`.
#[derive(Debug, Clone, Serialize)]
pub struct AuditStatus {
    /// `true` when a ledger is configured (`--ledger-dir`).
    pub configured: bool,
    /// `true` when the chain verified intact (always `true` when not
    /// configured — there is no chain to break).
    pub intact: bool,
    /// Number of records in the chain (`0` when not configured).
    pub records: u64,
    /// The seq of the first broken record, when the chain is broken.
    pub first_break_seq: Option<u64>,
    /// A human-readable reason for the first break, when the chain is broken.
    pub break_reason: Option<String>,
}

impl Default for AuditStatus {
    fn default() -> Self {
        // The not-configured baseline: no chain, trivially intact.
        Self {
            configured: false,
            intact: true,
            records: 0,
            first_break_seq: None,
            break_reason: None,
        }
    }
}

/// Inputs the supervisor knows about itself, threaded in from `AppState`.
#[derive(Debug, Clone)]
pub struct SupervisorIdentity {
    pub version: &'static str,
    pub build: &'static str,
    pub port: u16,
    pub uptime_seconds: u64,
    pub workspace: String,
    pub read_only: bool,
    pub dashboard_disabled: bool,
}

/// Compute per-run deadline status. Pure and `now`-injected.
fn run_deadline_status(run: &ActiveRun, now: DateTime<Utc>) -> RunDeadlineStatus {
    let (remaining_seconds, over_deadline, over_by_seconds) = match run.deadline {
        Some(deadline) => {
            let remaining = (deadline - now).num_seconds();
            let over = remaining < 0;
            (Some(remaining), over, Some((-remaining).max(0)))
        }
        None => (None, false, None),
    };
    RunDeadlineStatus {
        run_id: run.run_id.clone(),
        pid: run.pid,
        started_at: run.started_at.map(|t| t.to_rfc3339()),
        deadline: run.deadline.map(|t| t.to_rfc3339()),
        remaining_seconds,
        over_deadline,
        over_by_seconds,
    }
}

/// Heartbeat freshness, pure and `now`-injected.
///
/// `seq_age_seconds` is the seq-change age computed by the watchdog's
/// stateful tracker (threaded in by the caller); when present it — not the
/// timestamp age — decides staleness, because the periodic timer keeps the
/// timestamp fresh even on a wedged loop. When absent (legacy heartbeat with
/// no `seq`), staleness falls back to the timestamp age, exactly as before.
fn heartbeat_status(
    hb: Option<&Heartbeat>,
    now: DateTime<Utc>,
    stale_threshold_seconds: u64,
    seq_age_seconds: Option<u64>,
) -> HeartbeatStatus {
    match hb.and_then(|h| h.last_heartbeat.map(|ts| (h, ts))) {
        Some((h, last)) => {
            let age = (now - last).num_seconds();
            // Prefer the seq-change age for the staleness verdict; fall back
            // to the timestamp age when seq is not being tracked.
            let stale = match seq_age_seconds {
                Some(seq_age) => seq_age >= stale_threshold_seconds,
                None => age.max(0) as u64 >= stale_threshold_seconds,
            };
            HeartbeatStatus {
                present: true,
                orchestrator_pid: h.pid,
                last_heartbeat: Some(last.to_rfc3339()),
                age_seconds: Some(age),
                seq: h.seq,
                seq_age_seconds,
                stale,
                stale_threshold_seconds,
                phase: h.phase.clone(),
                orchestrator_uptime_seconds: h.started_at.map(|s| (now - s).num_seconds().max(0)),
            }
        }
        None => HeartbeatStatus {
            present: false,
            orchestrator_pid: hb.and_then(|h| h.pid),
            last_heartbeat: None,
            age_seconds: None,
            seq: hb.and_then(|h| h.seq),
            seq_age_seconds,
            stale: false,
            stale_threshold_seconds,
            phase: hb.and_then(|h| h.phase.clone()),
            orchestrator_uptime_seconds: None,
        },
    }
}

/// Assemble the full `/statusz` view from disk + the action log.
///
/// `seq_age_seconds` is the watchdog's seq-change age for the current
/// heartbeat (a read-only snapshot of its stateful tracker). Threaded in
/// rather than recomputed here so `/statusz` reports exactly the figure the
/// watchdog is deciding on. `None` means seq is not being tracked (legacy
/// heartbeat) and staleness falls back to the timestamp age.
// A pure assembler: each parameter is one independent, already-computed input
// surface (identity, thresholds, the two heartbeat ages, the fold/ledger/diff
// diagnostics, the action ring). Bundling them into a struct would only move
// the same fields behind one more name, so the explicit signature is clearer.
#[allow(clippy::too_many_arguments)]
pub fn build_statusz(
    paths: &WorkspacePaths,
    identity: &SupervisorIdentity,
    heartbeat_stale_threshold_seconds: u64,
    seq_age_seconds: Option<u64>,
    fold_diagnostics: crate::fold_stats::FoldDiagnosticsView,
    action_log: &Arc<WatchdogLog>,
    audit_ledger: AuditStatus,
    diff_containment: crate::diff_containment::DiffContainmentView,
    promotion_gate: crate::promotion_gate::PromotionGateView,
) -> StatuszView {
    let now = Utc::now();

    let hb = reader::read_heartbeat(paths);
    let heartbeat = heartbeat_status(
        hb.as_ref(),
        now,
        heartbeat_stale_threshold_seconds,
        seq_age_seconds,
    );

    let runs: Vec<RunDeadlineStatus> = reader::read_active_runs(paths)
        .iter()
        .map(|r| run_deadline_status(r, now))
        .collect();
    let runs_over_deadline = runs.iter().filter(|r| r.over_deadline).count();

    let summary = if runs.is_empty() {
        "no active runs".to_string()
    } else if runs_over_deadline == 0 {
        format!("all {} run(s) within deadline", runs.len())
    } else {
        format!(
            "{runs_over_deadline} run(s) OVER deadline / {} active",
            runs.len()
        )
    };

    StatuszView {
        generated_at: now.to_rfc3339(),
        supervisor: SupervisorInfo {
            version: identity.version.to_string(),
            build: identity.build.to_string(),
            port: identity.port,
            uptime_seconds: identity.uptime_seconds,
            workspace: identity.workspace.clone(),
            read_only: identity.read_only,
            dashboard_disabled: identity.dashboard_disabled,
            pid: std::process::id() as i32,
        },
        heartbeat,
        runs,
        runs_over_deadline,
        summary,
        watchdog_actions: action_log.snapshot(),
        fold_diagnostics,
        audit_ledger,
        diff_containment,
        promotion_gate,
    }
}

/// Minimal HTML escape for the few user-controlled strings rendered
/// (workspace path, phase, run ids). Terse but correct.
fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Format a signed second count as a terse `h:mm:ss` (or `-h:mm:ss`).
fn hms(total: i64) -> String {
    let neg = total < 0;
    let t = total.unsigned_abs();
    let h = t / 3600;
    let m = (t % 3600) / 60;
    let s = t % 60;
    format!("{}{h}:{m:02}:{s:02}", if neg { "-" } else { "" })
}

/// Render the `/statusz` view as a self-contained terse HTML page.
///
/// Inline CSS, monospace, no JS, no framework. A single `<meta refresh>`
/// keeps it current without scripting.
pub fn render_html(v: &StatuszView) -> String {
    let mut out = String::with_capacity(4096);
    out.push_str(
        "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>zicato supervisor / statusz</title>\
<meta http-equiv=\"refresh\" content=\"5\">\
<style>\
body{background:#111;color:#ddd;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:1.5rem;max-width:60rem}\
h1{font-size:14px;color:#fff;border-bottom:1px solid #444;padding-bottom:.3rem}\
h2{font-size:13px;color:#9cf;margin:1.4rem 0 .3rem}\
table{border-collapse:collapse;width:100%}\
td,th{text-align:left;padding:.15rem .8rem .15rem 0;white-space:nowrap}\
th{color:#888;font-weight:normal}\
.ok{color:#7c7}.warn{color:#fc6}.bad{color:#f77;font-weight:bold}\
.dim{color:#777}\
.summary{padding:.4rem .6rem;margin:.6rem 0;border-left:3px solid #444}\
.summary.ok{border-color:#7c7}.summary.bad{border-color:#f77}\
</style></head><body>",
    );

    out.push_str("<h1>zicato supervisor &mdash; /statusz</h1>");

    // Summary line.
    let sum_class = if v.runs_over_deadline > 0 {
        "bad"
    } else {
        "ok"
    };
    out.push_str(&format!(
        "<div class=\"summary {sum_class}\">{}</div>",
        esc(&v.summary)
    ));

    // Supervisor.
    let s = &v.supervisor;
    out.push_str("<h2>supervisor</h2><table>");
    out.push_str(&format!(
        "<tr><th>version</th><td>{}</td></tr>",
        esc(&s.version)
    ));
    out.push_str(&format!(
        "<tr><th>build</th><td>{}</td></tr>",
        esc(&s.build)
    ));
    out.push_str(&format!("<tr><th>pid</th><td>{}</td></tr>", s.pid));
    out.push_str(&format!("<tr><th>port</th><td>{}</td></tr>", s.port));
    out.push_str(&format!(
        "<tr><th>uptime</th><td>{}</td></tr>",
        hms(s.uptime_seconds as i64)
    ));
    out.push_str(&format!(
        "<tr><th>workspace</th><td>{}</td></tr>",
        esc(&s.workspace)
    ));
    out.push_str(&format!(
        "<tr><th>read-only</th><td>{}</td></tr>",
        s.read_only
    ));
    out.push_str(&format!(
        "<tr><th>mode</th><td>{}</td></tr>",
        if s.dashboard_disabled {
            "watchdog-only (--no-dashboard)"
        } else {
            "watchdog + dashboard"
        }
    ));
    out.push_str("</table>");

    // Heartbeat.
    let h = &v.heartbeat;
    out.push_str("<h2>heartbeat</h2><table>");
    if h.present {
        let (cls, label) = if h.stale {
            ("bad", "STALE")
        } else {
            ("ok", "fresh")
        };
        out.push_str(&format!(
            "<tr><th>state</th><td class=\"{cls}\">{label}</td></tr>"
        ));
        out.push_str(&format!(
            "<tr><th>timestamp age</th><td>{}</td></tr>",
            h.age_seconds.map(hms).unwrap_or_else(|| "?".into()),
        ));
        // The seq-change age is the authoritative liveness signal when the
        // orchestrator publishes a `seq` cursor; surface both so an operator
        // can see a wedged loop (fresh timestamp, growing seq age).
        if let Some(seq) = h.seq {
            out.push_str(&format!("<tr><th>seq</th><td>{seq}</td></tr>"));
        }
        match h.seq_age_seconds {
            Some(seq_age) => out.push_str(&format!(
                "<tr><th>seq age</th><td>{} (threshold {}s)</td></tr>",
                hms(seq_age as i64),
                h.stale_threshold_seconds,
            )),
            None => out.push_str(&format!(
                "<tr><th>seq age</th><td class=\"dim\">untracked \
(no seq; staleness from timestamp, threshold {}s)</td></tr>",
                h.stale_threshold_seconds,
            )),
        }
        out.push_str(&format!(
            "<tr><th>last</th><td>{}</td></tr>",
            esc(h.last_heartbeat.as_deref().unwrap_or("-"))
        ));
    } else {
        out.push_str("<tr><th>state</th><td class=\"warn\">no heartbeat file</td></tr>");
    }
    out.push_str(&format!(
        "<tr><th>orchestrator pid</th><td>{}</td></tr>",
        h.orchestrator_pid
            .map(|p| p.to_string())
            .unwrap_or_else(|| "-".into())
    ));
    if let Some(p) = &h.phase {
        out.push_str(&format!("<tr><th>phase</th><td>{}</td></tr>", esc(p)));
    }
    if let Some(u) = h.orchestrator_uptime_seconds {
        out.push_str(&format!("<tr><th>orch uptime</th><td>{}</td></tr>", hms(u)));
    }
    out.push_str("</table>");

    // Runs / deadlines.
    out.push_str("<h2>runs &amp; deadlines</h2>");
    if v.runs.is_empty() {
        out.push_str("<p class=\"dim\">no active runs</p>");
    } else {
        out.push_str(
            "<table><tr><th>run_id</th><th>pid</th><th>started</th>\
<th>deadline</th><th>remaining</th><th>status</th></tr>",
        );
        for r in &v.runs {
            let (cls, status) = if r.over_deadline {
                (
                    "bad",
                    format!("OVER by {}", hms(r.over_by_seconds.unwrap_or(0))),
                )
            } else if r.remaining_seconds.is_some() {
                ("ok", "within deadline".to_string())
            } else {
                ("dim", "no deadline".to_string())
            };
            out.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>\
<td class=\"{cls}\">{}</td><td class=\"{cls}\">{}</td></tr>",
                esc(&r.run_id),
                r.pid.map(|p| p.to_string()).unwrap_or_else(|| "-".into()),
                esc(r.started_at.as_deref().unwrap_or("-")),
                esc(r.deadline.as_deref().unwrap_or("-")),
                r.remaining_seconds.map(hms).unwrap_or_else(|| "-".into()),
                status,
            ));
        }
        out.push_str("</table>");
    }

    // Watchdog actions.
    out.push_str("<h2>watchdog actions</h2>");
    if v.watchdog_actions.is_empty() {
        out.push_str(
            "<p class=\"dim\">no escalations this process lifetime \
(in-memory; cleared on restart)</p>",
        );
    } else {
        out.push_str(
            "<table><tr><th>when</th><th>trigger</th><th>pid</th>\
<th>run_id</th><th>outcome</th></tr>",
        );
        for a in v.watchdog_actions.iter().rev() {
            let cls = match a.outcome {
                crate::action_log::Outcome::Failed => "bad",
                crate::action_log::Outcome::KilledForcefully => "warn",
                _ => "dim",
            };
            out.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>\
<td class=\"{cls}\">{}</td></tr>",
                esc(&a.ts.to_rfc3339()),
                a.trigger.as_str(),
                a.pid,
                esc(a.run_id.as_deref().unwrap_or("-")),
                a.outcome.as_str(),
            ));
        }
        out.push_str("</table>");
    }

    // Fold diagnostics: torn-write / non-monotonic-seq counters over the
    // canonical active-tournament JSONL fold (process lifetime).
    let fd = &v.fold_diagnostics;
    out.push_str("<h2>fold diagnostics</h2><table>");
    let pf_cls = if fd.parse_failures > 0 { "bad" } else { "ok" };
    let sg_cls = if fd.seq_gaps > 0 { "warn" } else { "ok" };
    out.push_str(&format!(
        "<tr><th>torn writes (parse failures)</th><td class=\"{pf_cls}\">{}</td></tr>",
        fd.parse_failures
    ));
    out.push_str(&format!(
        "<tr><th>non-monotonic seq (gaps)</th><td class=\"{sg_cls}\">{}</td></tr>",
        fd.seq_gaps
    ));
    out.push_str(&format!(
        "<tr><th>folds observed</th><td class=\"dim\">{}</td></tr>",
        fd.folds
    ));
    out.push_str("</table>");

    // Audit ledger: the tamper-evident hash-chain's integrity.
    let al = &v.audit_ledger;
    out.push_str("<h2>audit ledger</h2><table>");
    if !al.configured {
        out.push_str(
            "<tr><th>state</th><td class=\"dim\">not configured \
(pass --ledger-dir to enable the tamper-evident ledger)</td></tr>",
        );
    } else {
        let (cls, label) = if al.intact {
            ("ok", "INTACT")
        } else {
            ("bad", "CHAIN BREAK")
        };
        out.push_str(&format!(
            "<tr><th>chain</th><td class=\"{cls}\">{label}</td></tr>"
        ));
        out.push_str(&format!(
            "<tr><th>records</th><td>{}</td></tr>",
            al.records
        ));
        if let Some(seq) = al.first_break_seq {
            out.push_str(&format!(
                "<tr><th>first break</th><td class=\"bad\">seq {seq}</td></tr>"
            ));
        }
        if let Some(reason) = &al.break_reason {
            out.push_str(&format!(
                "<tr><th>reason</th><td class=\"bad\">{}</td></tr>",
                esc(reason)
            ));
        }
    }
    out.push_str("</table>");

    // Diff-containment: are mutations confined to the mutation sites?
    let dc = &v.diff_containment;
    out.push_str("<h2>diff containment</h2><table>");
    if !dc.scanned {
        out.push_str(
            "<tr><th>state</th><td class=\"dim\">not scanned \
(pass --diff-containment to enable the attestation)</td></tr>",
        );
    } else {
        let quarantined = dc.quarantined.len();
        let (cls, label) = if quarantined > 0 {
            ("bad", "OUT-OF-BOUNDS MUTATIONS")
        } else {
            ("ok", "contained")
        };
        out.push_str(&format!(
            "<tr><th>state</th><td class=\"{cls}\">{label}</td></tr>"
        ));
        out.push_str(&format!(
            "<tr><th>pairs scanned</th><td>{}</td></tr>",
            dc.pairs_scanned
        ));
        if dc.pairs_skipped > 0 {
            out.push_str(&format!(
                "<tr><th>pairs skipped</th><td class=\"dim\">{} (fail-open)</td></tr>",
                dc.pairs_skipped
            ));
        }
        out.push_str(&format!(
            "<tr><th>quarantined</th><td class=\"{}\">{}</td></tr>",
            if quarantined > 0 { "bad" } else { "ok" },
            quarantined
        ));
    }
    out.push_str("</table>");
    // The offending generations + files, when any.
    if !dc.quarantined.is_empty() {
        out.push_str(
            "<table><tr><th>generation</th><th>parent</th>\
<th>file</th><th>diff</th></tr>",
        );
        for att in &dc.quarantined {
            for vio in &att.violations {
                out.push_str(&format!(
                    "<tr><td>{}</td><td>{}</td><td class=\"bad\">{}</td><td class=\"bad\">{}</td></tr>",
                    esc(&att.generation_id),
                    esc(&att.parent_generation_id),
                    esc(&vio.path),
                    vio.kind.as_str(),
                ));
            }
        }
        out.push_str("</table>");
    }

    // Promotion gatekeeping: does each recorded promotion match its scores?
    let pg = &v.promotion_gate;
    out.push_str("<h2>promotion gate</h2><table>");
    if !pg.scanned {
        out.push_str(
            "<tr><th>state</th><td class=\"dim\">not scanned \
(pass --promotion-gate to enable the recompute)</td></tr>",
        );
    } else {
        let n = pg.contradictions.len();
        let (cls, label) = if n > 0 {
            ("bad", "DECISION CONTRADICTS SCORES")
        } else {
            ("ok", "consistent")
        };
        out.push_str(&format!(
            "<tr><th>state</th><td class=\"{cls}\">{label}</td></tr>"
        ));
        out.push_str(&format!(
            "<tr><th>promotions checked</th><td>{}</td></tr>",
            pg.promotions_checked
        ));
        if pg.skipped > 0 {
            out.push_str(&format!(
                "<tr><th>skipped</th><td class=\"dim\">{} (no scalar evidence)</td></tr>",
                pg.skipped
            ));
        }
        out.push_str(&format!(
            "<tr><th>contradictions</th><td class=\"{}\">{}</td></tr>",
            if n > 0 { "bad" } else { "ok" },
            n
        ));
    }
    out.push_str("</table>");
    if !pg.contradictions.is_empty() {
        out.push_str(
            "<table><tr><th>challenger</th><th>champion</th>\
<th>delta_scalar</th><th>detail</th></tr>",
        );
        for c in &pg.contradictions {
            out.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td class=\"bad\">{:.6}</td><td class=\"bad\">{}</td></tr>",
                esc(&c.challenger_generation_id),
                esc(&c.champion_generation_id),
                c.delta_scalar,
                esc(&c.detail),
            ));
        }
        out.push_str("</table>");
    }

    out.push_str(&format!(
        "<p class=\"dim\">generated {}</p>",
        esc(&v.generated_at)
    ));
    out.push_str("</body></html>");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::action_log::{Outcome, Trigger};
    use chrono::Duration as ChDuration;

    #[test]
    fn run_within_deadline_is_not_flagged() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(4242),
            started_at: Some(now - ChDuration::seconds(60)),
            deadline: Some(now + ChDuration::seconds(300)),
            ..Default::default()
        };
        let st = run_deadline_status(&run, now);
        assert!(!st.over_deadline);
        assert_eq!(st.over_by_seconds, Some(0));
        assert!(st.remaining_seconds.unwrap() > 0);
    }

    #[test]
    fn over_deadline_run_is_flagged() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r-late".into(),
            pid: Some(4242),
            started_at: Some(now - ChDuration::seconds(1000)),
            deadline: Some(now - ChDuration::seconds(90)),
            ..Default::default()
        };
        let st = run_deadline_status(&run, now);
        assert!(st.over_deadline);
        assert!(st.remaining_seconds.unwrap() < 0);
        assert!(st.over_by_seconds.unwrap() >= 89);
    }

    #[test]
    fn run_without_deadline_is_not_over() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r-nodl".into(),
            pid: Some(1),
            started_at: Some(now),
            deadline: None,
            ..Default::default()
        };
        let st = run_deadline_status(&run, now);
        assert!(!st.over_deadline);
        assert_eq!(st.remaining_seconds, None);
        assert_eq!(st.over_by_seconds, None);
    }

    #[test]
    fn stale_heartbeat_is_flagged() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(10015),
            last_heartbeat: Some(now - ChDuration::seconds(120)),
            ..Default::default()
        };
        // No seq tracked → staleness falls back to the timestamp age.
        let st = heartbeat_status(Some(&hb), now, 90, None);
        assert!(st.present);
        assert!(st.stale);
        assert_eq!(st.orchestrator_pid, Some(10015));
    }

    #[test]
    fn fresh_heartbeat_is_not_stale() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(10015),
            last_heartbeat: Some(now - ChDuration::seconds(3)),
            ..Default::default()
        };
        let st = heartbeat_status(Some(&hb), now, 90, None);
        assert!(!st.stale);
    }

    #[test]
    fn missing_heartbeat_is_not_present() {
        let st = heartbeat_status(None, Utc::now(), 90, None);
        assert!(!st.present);
        assert!(!st.stale);
    }

    #[test]
    fn seq_age_drives_staleness_when_tracked() {
        // A FRESH timestamp but a stale seq-change age (the loop is wedged
        // while the periodic timer keeps the timestamp current) must read
        // STALE — the seq age is authoritative when tracked.
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(10015),
            last_heartbeat: Some(now - ChDuration::seconds(2)),
            seq: Some(7),
            ..Default::default()
        };
        let st = heartbeat_status(Some(&hb), now, 90, Some(120));
        assert!(st.stale, "stale seq age must mark the heartbeat stale");
        assert_eq!(st.seq, Some(7));
        assert_eq!(st.seq_age_seconds, Some(120));
        // The fresh timestamp age is still surfaced alongside.
        assert!(st.age_seconds.unwrap() < 10);
    }

    #[test]
    fn fresh_seq_age_is_not_stale_even_with_old_timestamp() {
        // The complementary case: a stale TIMESTAMP but a fresh seq-change
        // age means the loop just advanced — not stale.
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(10015),
            last_heartbeat: Some(now - ChDuration::seconds(300)),
            seq: Some(42),
            ..Default::default()
        };
        let st = heartbeat_status(Some(&hb), now, 90, Some(1));
        assert!(!st.stale);
        assert_eq!(st.seq_age_seconds, Some(1));
    }

    #[test]
    fn html_renders_non_empty_and_self_contained() {
        let view = StatuszView {
            generated_at: Utc::now().to_rfc3339(),
            supervisor: SupervisorInfo {
                version: "0.1.0".into(),
                build: "0.1.0".into(),
                port: 7892,
                uptime_seconds: 10,
                workspace: "/tmp/ws".into(),
                read_only: false,
                dashboard_disabled: true,
                pid: 1234,
            },
            heartbeat: heartbeat_status(None, Utc::now(), 90, None),
            runs: vec![],
            runs_over_deadline: 0,
            summary: "no active runs".into(),
            watchdog_actions: vec![Action {
                ts: Utc::now(),
                trigger: Trigger::RunDeadline,
                pid: 4242,
                run_id: Some("r-late".into()),
                outcome: Outcome::KilledForcefully,
            }],
            fold_diagnostics: Default::default(),
            audit_ledger: Default::default(),
            diff_containment: Default::default(),
            promotion_gate: Default::default(),
        };
        let html = render_html(&view);
        assert!(html.starts_with("<!doctype html>"));
        assert!(html.contains("/statusz"));
        // Self-contained: inline style, no external asset references.
        assert!(html.contains("<style>"));
        assert!(!html.contains("src=\""));
        assert!(!html.contains("<script"));
        // The watchdog action is surfaced.
        assert!(html.contains("r-late"));
        assert!(html.contains("killed_forcefully"));
    }

    #[test]
    fn html_escapes_run_ids_and_paths() {
        let view = StatuszView {
            generated_at: Utc::now().to_rfc3339(),
            supervisor: SupervisorInfo {
                version: "0.1.0".into(),
                build: "0.1.0".into(),
                port: 7892,
                uptime_seconds: 10,
                workspace: "/tmp/<evil>".into(),
                read_only: false,
                dashboard_disabled: false,
                pid: 1,
            },
            heartbeat: heartbeat_status(None, Utc::now(), 90, None),
            runs: vec![],
            runs_over_deadline: 0,
            summary: "no active runs".into(),
            watchdog_actions: vec![],
            fold_diagnostics: Default::default(),
            audit_ledger: Default::default(),
            diff_containment: Default::default(),
            promotion_gate: Default::default(),
        };
        let html = render_html(&view);
        assert!(html.contains("/tmp/&lt;evil&gt;"));
        assert!(!html.contains("/tmp/<evil>"));
    }
}
