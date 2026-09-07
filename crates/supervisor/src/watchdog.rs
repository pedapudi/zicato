//! Watchdog tasks: heartbeat staleness + run staleness/deadline checks.
//!
//! Each tick reads runtime files and verifies process identity before
//! admitting concurrent escalations. Orphan mutation also holds the stable
//! writer guard through record inspection, termination and finalization.
//!
//! Deadline enforcement is a first-class, default-on trigger: every
//! board-entry run carries a `deadline` (`started_at +
//! wall_clock_budget_seconds`). When `now` passes that deadline the
//! watchdog SIGTERM→SIGKILLs the run's worker pid. Because the supervisor
//! is its own OS process this holds even when the orchestrator's event
//! loop is wedged. Run-staleness (`last_progress` not advancing) is a
//! separate, complementary trigger — a run can be killed for stalling OR
//! for blowing its wall-clock budget.

use crate::action_log::{Action, Trigger, WatchdogLog};
use crate::ledger::{AuditLedger, RecordKind};
use crate::reader::{self, WorkspacePaths};
use crate::reap::{self, producer_is_dead};
use crate::signal::{self, escalate_owned_target, KillTarget};
use chrono::{DateTime, Utc};
use futures::stream::{FuturesUnordered, StreamExt};
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast::Sender;
use tracing::warn;

/// Diff-containment configuration threaded into [`runs_loop`].
///
/// `enabled` gates the per-tick scan (off by default → the loop behaves
/// exactly as before); `findings` is the shared store the scan writes its
/// latest result into for `/statusz`. Bundled into a struct so the loop's
/// signature stays readable as the integrity-notary surface grows.
#[derive(Clone)]
pub struct DiffContainmentConfig {
    pub enabled: bool,
    pub findings: Arc<crate::diff_containment::DiffContainmentFindings>,
}

/// Promotion-gatekeeping configuration threaded into [`runs_loop`].
///
/// `enabled` gates the per-tick scan (off by default); `findings` is the
/// shared store the scan writes its latest result into for `/statusz`.
#[derive(Clone)]
pub struct PromotionGateConfig {
    pub enabled: bool,
    pub findings: Arc<crate::promotion_gate::PromotionGateFindings>,
}

/// Index-vs-canonical divergence-audit configuration threaded into
/// [`runs_loop`].
///
/// `enabled` gates the per-tick audit (off by default); `findings` is the
/// shared store; `stuck_age_seconds` is the (c)-check threshold.
#[derive(Clone)]
pub struct DivergenceConfig {
    pub enabled: bool,
    pub findings: Arc<crate::divergence::DivergenceFindings>,
    pub stuck_age_seconds: i64,
}

/// Record one watchdog escalation in the in-memory ring AND, when a
/// tamper-evident ledger is configured, append it to the persisted
/// hash-chained ledger too.
///
/// The ledger is the INTEGRITY NOTARY's durable record: the in-memory ring
/// is cleared on restart, but the ledger persists and chains every action so
/// the history cannot be silently edited. `ledger` is `None` when no ledger
/// is configured (the default), in which case this is exactly the prior
/// behavior — a single `ring.record(...)`.
fn record_action(ring: &WatchdogLog, ledger: Option<&Arc<AuditLedger>>, action: Action) {
    if let Some(ledger) = ledger {
        ledger.append(
            RecordKind::WatchdogAction,
            serde_json::json!({
                "trigger": action.trigger.as_str(),
                "pid": action.pid,
                "run_id": action.run_id,
                "outcome": action.outcome.as_str(),
            }),
        );
    }
    ring.record(action);
}

/// Observe promote/reject decision transitions and epoch contract-hash
/// changes from the canonical (orchestrator-written) state and stamp each new
/// one into the tamper-evident ledger.
///
/// Read-only and alarm-only: this never blocks a promotion or writes the
/// orchestrator's trees — it only records what it observes into the
/// supervisor's own chain. De-duplication lives in the [`TransitionObserver`],
/// so a steady-state poll appends nothing.
fn observe_transitions(
    paths: &WorkspacePaths,
    ledger: &Arc<AuditLedger>,
    observer: &mut crate::ledger::TransitionObserver,
) {
    // Decisions: every resolved generation across every epoch.
    let lineage = reader::build_lineage_view(paths);
    observer.observe_decisions(
        ledger,
        lineage
            .generations
            .iter()
            .map(|g| (g.epoch_id.as_str(), g.generation_id.as_str(), g.promoted)),
    );

    // Contract hash: the current epoch's frozen contract.
    let epoch = crate::epoch::build_epoch_view(paths);
    if let (Some(epoch_id), Some(contract_hash)) = (epoch.epoch_id, epoch.contract_hash) {
        observer.observe_contract(ledger, &epoch_id, &contract_hash);
    }
}

/// Run one diff-containment scan over the workspace and surface its findings.
///
/// READ-ONLY / ALARM-ONLY (v1): scans every materialised child generation,
/// records the latest scan into the shared findings store for `/statusz`,
/// writes a quarantine finding into the epoch health dir for each violating
/// pair, and — when a ledger is configured — appends a hard alert record per
/// quarantined generation. Never blocks a promotion, never writes the
/// orchestrator's trees. De-duplicated against the previous scan's quarantine
/// set so a standing violation is alerted ONCE (until it clears and recurs),
/// not on every tick.
fn run_diff_containment_scan(
    paths: &WorkspacePaths,
    diff: &DiffContainmentConfig,
    ledger: Option<&Arc<AuditLedger>>,
    previously_quarantined: &mut HashSet<(String, String)>,
) {
    let view = crate::diff_containment::scan_workspace(paths);
    for finding in &view.range_attestations {
        crate::range_containment::write_finding(paths, finding);
    }

    // Persist a quarantine finding for each violating pair, and alert the
    // ledger only for generations not already quarantined in the prior scan.
    let mut current: HashSet<(String, String)> = HashSet::new();
    for att in &view.quarantined {
        let key = (att.epoch_id.clone(), att.generation_id.clone());
        current.insert(key.clone());
        crate::diff_containment::write_quarantine_finding(paths, att);
        if !previously_quarantined.contains(&key) {
            warn!(
                epoch_id = %att.epoch_id,
                generation_id = %att.generation_id,
                parent = %att.parent_generation_id,
                violations = att.violations.len(),
                "DIFF-CONTAINMENT ALERT: generation mutated files outside its mutable surface",
            );
            if let Some(ledger) = ledger {
                ledger.append(
                    crate::ledger::RecordKind::DiffContainmentAlert,
                    serde_json::json!({
                        "epoch_id": att.epoch_id,
                        "generation_id": att.generation_id,
                        "parent_generation_id": att.parent_generation_id,
                        "violations": att.violations,
                    }),
                );
            }
        }
    }
    *previously_quarantined = current;
    diff.findings.record(view);
}

/// Run one promotion-gatekeeping scan and surface its findings.
///
/// READ-ONLY / ALARM-ONLY (v1): re-applies the gate's scalar rule to every
/// recorded promotion in the current epoch and alarms when a promotion is not
/// supported by the recorded scores. Records the latest scan into the shared
/// store for `/statusz` and — when a ledger is configured — appends a hard
/// alert per newly-observed contradiction. Never blocks a promotion. The alarm
/// is de-duplicated against the prior scan's contradiction set (keyed by
/// generation) so a standing contradiction alerts once.
fn run_promotion_gate_scan(
    paths: &WorkspacePaths,
    gate: &PromotionGateConfig,
    ledger: Option<&Arc<AuditLedger>>,
    previously_flagged: &mut HashSet<(String, String)>,
) {
    let view = crate::promotion_gate::scan_current_epoch(paths);

    let mut current: HashSet<(String, String)> = HashSet::new();
    for c in &view.contradictions {
        let key = (c.epoch_id.clone(), c.challenger_generation_id.clone());
        current.insert(key.clone());
        if !previously_flagged.contains(&key) {
            warn!(
                epoch_id = %c.epoch_id,
                challenger = %c.challenger_generation_id,
                champion = %c.champion_generation_id,
                delta_scalar = c.delta_scalar,
                "PROMOTION-GATE ALERT: {}",
                c.detail,
            );
            if let Some(ledger) = ledger {
                ledger.append(
                    crate::ledger::RecordKind::PromotionContradiction,
                    serde_json::json!({
                        "epoch_id": c.epoch_id,
                        "challenger_generation_id": c.challenger_generation_id,
                        "champion_generation_id": c.champion_generation_id,
                        "recorded_decision": c.recorded_decision,
                        "delta_scalar": c.delta_scalar,
                        "promote_margin": c.promote_margin,
                        "detail": c.detail,
                    }),
                );
            }
        }
    }
    *previously_flagged = current;
    gate.findings.record(view);
}

/// Run one index-vs-canonical divergence audit and surface its findings.
///
/// READ-ONLY (v1): joins the canonical lineage / epoch config against the
/// index and records the latest findings into the shared store for `/statusz`.
/// When a ledger is configured, appends a hard alert per newly-observed
/// finding, de-duplicated by `(code, generation_id)` so a standing divergence
/// alerts once. Never writes the index or the canonical trees.
fn run_divergence_audit(
    paths: &WorkspacePaths,
    divergence: &DivergenceConfig,
    ledger: Option<&Arc<AuditLedger>>,
    previously_seen: &mut HashSet<(String, Option<String>)>,
) {
    let view = crate::divergence::audit(paths, Utc::now(), divergence.stuck_age_seconds);

    let mut current: HashSet<(String, Option<String>)> = HashSet::new();
    for f in &view.findings {
        let key = (f.code.to_string(), f.generation_id.clone());
        current.insert(key.clone());
        if !previously_seen.contains(&key) {
            warn!(
                code = f.code,
                epoch_id = %f.epoch_id,
                generation_id = ?f.generation_id,
                "DIVERGENCE FINDING: {}",
                f.detail,
            );
            if let Some(ledger) = ledger {
                ledger.append(
                    crate::ledger::RecordKind::DivergenceFinding,
                    serde_json::json!({
                        "code": f.code,
                        "epoch_id": f.epoch_id,
                        "generation_id": f.generation_id,
                        "detail": f.detail,
                    }),
                );
            }
        }
    }
    *previously_seen = current;
    divergence.findings.record(view);
}

/// Thresholds for watchdog decisions.
#[derive(Debug, Clone, Copy)]
pub struct Thresholds {
    pub heartbeat_stale_warn: Duration,
    pub heartbeat_stale_kill: Duration,
    /// Warn threshold for per-run staleness (``last_progress`` not
    /// advancing). With the per-run heartbeat thread beating every ~3s
    /// this threshold is only reached when the thread itself is wedged,
    /// not during a normal slow LLM call.
    pub run_stale_warn: Duration,
    /// Kill threshold for per-run staleness. This is a **far backstop**
    /// for a genuinely wedged process: the primary kill trigger is the
    /// per-board wall-clock deadline (``decide_run_deadline``). When a
    /// run's ``wall_clock_budget_seconds`` is known,
    /// ``decide_run`` replaces this fixed threshold with 2x the budget;
    /// this default covers runs whose budget field is absent.
    pub run_stale_kill: Duration,
    /// Grace between SIGTERM and SIGKILL for heartbeat/staleness kills.
    pub grace: Duration,
    /// Grace between SIGTERM and SIGKILL for deadline-overrun kills
    /// (`--run-kill-grace`).
    pub run_kill_grace: Duration,
    /// Off-switch for per-run deadline enforcement
    /// (`--run-deadline-kill-disabled`). Deadline enforcement is on by
    /// default; this disables it for a read-only observability supervisor
    /// attached to a run it should not police.
    pub run_deadline_kill_disabled: bool,
    /// Hard ceiling (`--max-run-seconds`) on a single run's enforced
    /// wall-clock window, measured from `started_at`. The deadline a run
    /// record carries is **orchestrator-written and therefore untrusted**:
    /// a far-future (or accidentally huge) deadline would silently disable
    /// the very watchdog meant to bound the run. The deadline path clamps
    /// the effective cutoff to `started_at + max_run_seconds`, so a run is
    /// always killable no matter what deadline was written. The default is
    /// generous (well above any normal per-board budget) so legitimate runs
    /// are never clipped; it only fires on an implausible deadline.
    pub max_run_seconds: Duration,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            heartbeat_stale_warn: Duration::from_secs(30),
            heartbeat_stale_kill: Duration::from_secs(90),
            // 120s warn: the per-run heartbeat thread beats every ~3s, so
            // 120s of no progress means the worker thread itself is stuck.
            run_stale_warn: Duration::from_secs(120),
            // 600s backstop: used only when wall_clock_budget_seconds is
            // absent; otherwise decide_run computes 2x the per-run budget.
            run_stale_kill: Duration::from_secs(600),
            grace: Duration::from_secs(5),
            run_kill_grace: Duration::from_secs(5),
            run_deadline_kill_disabled: false,
            // 6h ceiling: a board entry that legitimately runs longer than
            // this is extraordinary; an orchestrator-written deadline beyond
            // it is treated as untrusted and clamped to started_at + 6h.
            max_run_seconds: Duration::from_secs(6 * 3600),
        }
    }
}

/// What the heartbeat watchdog wants to do this tick.
///
/// **The watchdog never kills the orchestrator.** An orchestrator whose
/// heartbeat has gone stale may simply be slow — a GC pause, a slow LLM
/// endpoint, or a process paused under a debugger — none of which is a
/// reason to destroy in-flight tournament work. Past the kill threshold
/// the watchdog therefore *escalates the warning* (`Stale`) rather than
/// signalling the orchestrator pid; automatic orchestrator restart is a
/// process-supervisor concern (systemd/supervisord/k8s), exactly as
/// RUNTIME.md §3.2 and ROBUSTNESS.md §2.4 already promise. There is no
/// `Kill` variant by design: `decide_heartbeat` cannot ever produce one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeartbeatAction {
    /// Heartbeat is fresh; nothing to do.
    Nothing,
    /// Heartbeat is stale past the warn threshold — surface it.
    Warn,
    /// Heartbeat is stale past the (former kill) deep-stale threshold —
    /// surface it more loudly, but still **only a warning**. The
    /// orchestrator pid is never signalled.
    Stale,
    /// No heartbeat file / no `last_heartbeat` field yet.
    MissingHeartbeat,
}

/// Decide what to do about the orchestrator heartbeat this tick.
///
/// This function is **warn-only by construction** — it can return
/// `Nothing`, `Warn`, `Stale`, or `MissingHeartbeat`, but never a kill.
/// The orchestrator is policed by an out-of-band process supervisor, not
/// by zicato's own watchdog (it would otherwise be able to kill the very
/// loop it exists to protect — see RUNTIME.md §3.2 / ROBUSTNESS.md §2.4).
/// The `heartbeat_stale_kill` threshold is retained only as a *deep-stale*
/// boundary that raises the warning's severity (`Warn` → `Stale`).
pub fn decide_heartbeat(
    heartbeat: Option<&crate::state::Heartbeat>,
    now: DateTime<Utc>,
    t: &Thresholds,
) -> HeartbeatAction {
    let Some(hb) = heartbeat else {
        return HeartbeatAction::MissingHeartbeat;
    };
    let Some(last) = hb.last_heartbeat else {
        return HeartbeatAction::MissingHeartbeat;
    };
    let age = now.signed_duration_since(last);
    let age_secs = age.num_seconds().max(0) as u64;

    classify_age(age_secs, t)
}

/// Classify a staleness age (seconds) into a warn-only `HeartbeatAction`.
/// Shared by the timestamp path ([`decide_heartbeat`]) and the seq-advance
/// path ([`SeqLiveness::observe`]) so both apply the same warn/deep-stale
/// thresholds. Never returns a kill — there is no kill variant.
fn classify_age(age_secs: u64, t: &Thresholds) -> HeartbeatAction {
    // Deep-stale: past the former kill threshold. We do NOT kill — we
    // escalate the warning's severity and leave the restart decision to
    // the operator / process supervisor.
    if age_secs >= t.heartbeat_stale_kill.as_secs() {
        return HeartbeatAction::Stale;
    }
    if age_secs >= t.heartbeat_stale_warn.as_secs() {
        return HeartbeatAction::Warn;
    }
    HeartbeatAction::Nothing
}

/// Stateful tracker for the heartbeat's progress cursor (`seq`).
///
/// The heartbeat timestamp is refreshed by a periodic timer, so it stays
/// fresh even when the orchestrator's loop is wedged — the timer is a
/// separate thread. The `seq` cursor, by contrast, only advances when the
/// loop makes genuine progress (RUNTIME-V2 Phase 4). Tracking *when seq
/// last changed* therefore detects a wedged loop that a fresh timestamp
/// would hide.
///
/// **Warn-only, like the rest of the heartbeat path** — this tracker
/// classifies staleness but never escalates to a kill.
#[derive(Debug, Clone, Default)]
pub struct SeqLiveness {
    /// The last `seq` value observed, once any heartbeat carried one.
    last_seq: Option<u64>,
    /// When `last_seq` last *changed* (not merely re-observed). Anchored on
    /// first observation so an orchestrator that legitimately sits on seq 0
    /// before its first transition is measured from when we started
    /// watching, not from epoch zero.
    last_seq_change_at: Option<DateTime<Utc>>,
}

/// The outcome of folding one heartbeat observation into a [`SeqLiveness`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SeqObservation {
    /// The warn-only action implied by seq-change age (when seq is present)
    /// or by timestamp age (the back-compat fallback when seq is absent).
    pub action: HeartbeatAction,
    /// Age in seconds of the last *seq change*, when seq is being tracked.
    /// `None` when the heartbeat carries no seq (old writer) — the action
    /// then comes from the timestamp path and `timestamp_age_seconds`
    /// carries the meaningful figure.
    pub seq_age_seconds: Option<u64>,
    /// Age in seconds of the heartbeat *timestamp*, always computed when a
    /// timestamp is present. Surfaced alongside `seq_age_seconds` so an
    /// operator can see both signals.
    pub timestamp_age_seconds: Option<u64>,
}

impl SeqLiveness {
    pub fn new() -> Self {
        Self::default()
    }

    /// The last seq value observed, for surfacing on `/statusz`.
    pub fn last_seq(&self) -> Option<u64> {
        self.last_seq
    }

    /// When the tracked `seq` last changed, when any has been observed.
    pub fn last_seq_change_at(&self) -> Option<DateTime<Utc>> {
        self.last_seq_change_at
    }

    /// Read-only snapshot of the seq/timestamp ages for `/statusz`, WITHOUT
    /// advancing the change anchor (the watchdog loop owns advancement).
    ///
    /// When `seq` is present we report the age since the last *observed*
    /// change anchor; if the live heartbeat already shows a newer seq than
    /// the tracker has folded in yet (a tick race), we treat that as a
    /// fresh change at `now`. When `seq` is absent we fall back to the
    /// timestamp age, mirroring [`observe`].
    pub fn snapshot(
        &self,
        heartbeat: Option<&crate::state::Heartbeat>,
        now: DateTime<Utc>,
        t: &Thresholds,
    ) -> SeqObservation {
        let Some(hb) = heartbeat else {
            return SeqObservation {
                action: HeartbeatAction::MissingHeartbeat,
                seq_age_seconds: None,
                timestamp_age_seconds: None,
            };
        };
        let timestamp_age_seconds = hb
            .last_heartbeat
            .map(|last| now.signed_duration_since(last).num_seconds().max(0) as u64);
        match hb.seq {
            Some(seq) => {
                // If the live seq already exceeds what we've folded in, the
                // change is at-or-after now; report a zero-age fresh change.
                let anchor = match self.last_seq {
                    Some(prev) if prev == seq => self.last_seq_change_at.unwrap_or(now),
                    _ => now,
                };
                let seq_age = now.signed_duration_since(anchor).num_seconds().max(0) as u64;
                SeqObservation {
                    action: classify_age(seq_age, t),
                    seq_age_seconds: Some(seq_age),
                    timestamp_age_seconds,
                }
            }
            None => {
                let action = match timestamp_age_seconds {
                    Some(age) => classify_age(age, t),
                    None => HeartbeatAction::MissingHeartbeat,
                };
                SeqObservation {
                    action,
                    seq_age_seconds: None,
                    timestamp_age_seconds,
                }
            }
        }
    }

    /// Fold one heartbeat observation in, advancing the change-anchor when
    /// `seq` moved, and return the warn-only classification.
    ///
    /// Staleness source:
    /// * `seq` present → age since the last seq *change* (the true liveness
    ///   signal). A fresh timestamp on an unmoving seq is treated as stale.
    /// * `seq` absent (legacy heartbeat) → falls back to timestamp age so
    ///   older orchestrators keep their existing semantics.
    ///
    /// Never returns a kill — `classify_age` has no kill outcome.
    pub fn observe(
        &mut self,
        heartbeat: Option<&crate::state::Heartbeat>,
        now: DateTime<Utc>,
        t: &Thresholds,
    ) -> SeqObservation {
        let Some(hb) = heartbeat else {
            return SeqObservation {
                action: HeartbeatAction::MissingHeartbeat,
                seq_age_seconds: None,
                timestamp_age_seconds: None,
            };
        };

        let timestamp_age_seconds = hb
            .last_heartbeat
            .map(|last| now.signed_duration_since(last).num_seconds().max(0) as u64);

        match hb.seq {
            Some(seq) => {
                // Advance the change anchor only when seq actually moved
                // (or on the very first observation).
                match self.last_seq {
                    Some(prev) if prev == seq => { /* unchanged: keep anchor */ }
                    _ => {
                        self.last_seq = Some(seq);
                        self.last_seq_change_at = Some(now);
                    }
                }
                let anchor = self.last_seq_change_at.unwrap_or(now);
                let seq_age = now.signed_duration_since(anchor).num_seconds().max(0) as u64;
                SeqObservation {
                    action: classify_age(seq_age, t),
                    seq_age_seconds: Some(seq_age),
                    timestamp_age_seconds,
                }
            }
            None => {
                // Back-compat: no seq cursor → timestamp age drives the
                // classification, exactly as before Phase 4.
                let action = match timestamp_age_seconds {
                    Some(age) => classify_age(age, t),
                    None => HeartbeatAction::MissingHeartbeat,
                };
                SeqObservation {
                    action,
                    seq_age_seconds: None,
                    timestamp_age_seconds,
                }
            }
        }
    }
}

/// Staleness-trigger outcome for an active run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunAction {
    Nothing,
    Warn,
    Kill { pid: i32 },
}

/// Run-staleness check: has `last_progress` (falling back to `started_at`)
/// stopped advancing past the warn/kill thresholds? This is independent of
/// the wall-clock deadline — see [`decide_run_deadline`].
///
/// Kill criterion: when the run record carries ``wall_clock_budget_seconds``
/// the effective kill threshold is ``2 × budget`` (a genuinely wedged
/// process that did not die at its own deadline and did not get caught by
/// the deadline trigger). When the budget is absent the static
/// ``t.run_stale_kill`` backstop applies.
pub fn decide_run(run: &crate::state::ActiveRun, now: DateTime<Utc>, t: &Thresholds) -> RunAction {
    let reference = run.last_progress.or(run.started_at);
    let Some(reference) = reference else {
        return RunAction::Nothing;
    };
    let age_secs = now.signed_duration_since(reference).num_seconds().max(0) as u64;

    // Effective kill threshold: 2x the per-run budget when known, else the
    // fixed backstop. This prevents a single slow LLM call from being
    // mis-classified as stalled even when the per-run heartbeat thread is
    // beating normally.
    let effective_kill_secs = run
        .wall_clock_budget_seconds
        .map(|b| (2.0 * b).ceil() as u64)
        .unwrap_or_else(|| t.run_stale_kill.as_secs());

    if age_secs >= effective_kill_secs {
        if let Some(pid) = run.pid {
            return RunAction::Kill { pid };
        }
        return RunAction::Warn;
    }
    if age_secs >= t.run_stale_warn.as_secs() {
        return RunAction::Warn;
    }
    RunAction::Nothing
}

/// Deadline-trigger outcome for an active run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunDeadlineAction {
    /// Deadline not reached, or there is nothing safe/sensible to signal.
    None,
    /// Deadline passed: ask the worker to stop (SIGTERM).
    Sigterm { pid: i32 },
    /// Deadline + grace passed and the worker is still alive: SIGKILL.
    Sigkill { pid: i32 },
}

/// Whether `pid` is one this watchdog is allowed to signal as a *run*
/// worker. The watchdog kills run pids only — never pid 0/1, never a
/// non-positive sentinel, never the supervisor's own pid, and never a
/// protected pid (the orchestrator pid carried by the heartbeat). This is
/// a pure guard so it can be unit-tested without spawning processes.
pub fn is_signalable_run_pid(pid: i32, protected: &HashSet<i32>) -> bool {
    // pid 0 addresses the whole process group; pid 1 is init. Neither is a
    // run worker, and signalling them would be catastrophic.
    if pid <= 1 {
        return false;
    }
    // Never signal ourselves.
    if pid == std::process::id() as i32 {
        return false;
    }
    // Never signal the orchestrator (heartbeat pid) or any other pid the
    // caller has explicitly fenced off.
    if protected.contains(&pid) {
        return false;
    }
    true
}

/// Select a recorded worker's group only when it owns that group identifier.
/// Protected groups and foreign memberships fall back to a single-process target.
/// A group may outlive its leader. The caller must verify the selected target's
/// recorded identity before any signal; target selection alone grants no authority.
pub fn resolve_kill_target(
    run: &crate::state::ActiveRun,
    vetted_pid: i32,
    protected_pgids: &HashSet<i32>,
) -> KillTarget {
    let leader = KillTarget::Leader { pid: vetted_pid };
    let Some(pgid) = run.pgid else {
        return leader;
    };
    // Only negate the worker's OWN group. The worker is its group's leader
    // (start_new_session → pgid == pid), so a pgid that does not equal the
    // vetted leader pid is a group we have NOT identity-matched; refuse it.
    let observed_group = signal::pgid_of(vetted_pid);
    if pgid != vetted_pid
        || observed_group.is_some_and(|group| group != pgid)
        || (observed_group.is_none() && signal::is_alive(vetted_pid))
    {
        return leader;
    }
    if !signal::is_negatable_pgid(pgid, protected_pgids) {
        return leader;
    }
    KillTarget::Group {
        pgid,
        leader_pid: vetted_pid,
    }
}

/// Build the set of process groups the watchdog must never negate: its own
/// group and the orchestrator's. Reading the orchestrator's pgid from its
/// (heartbeat) pid is best-effort — `getpgid` can race a just-exited
/// orchestrator — but the supervisor's own pgid is always fenced. Negating
/// a protected group would signal the supervisor and/or the orchestrator.
fn protected_pgids(heartbeat_pid: Option<i32>) -> HashSet<i32> {
    let mut set = HashSet::new();
    set.insert(signal::own_pgid());
    if let Some(pid) = heartbeat_pid {
        if let Some(pgid) = signal::pgid_of(pid) {
            set.insert(pgid);
        }
    }
    set
}

/// The effective, **clamped** deadline the watchdog enforces for a run.
///
/// The deadline a run record carries is orchestrator-written and untrusted:
/// a far-future value would disable the watchdog. When the run has a
/// `started_at` we cap the enforced cutoff at `started_at + max_run_seconds`,
/// so the run is always killable no matter what deadline was written. When
/// `started_at` is absent there is no anchor to clamp against, so the
/// written deadline is used as-is (the watchdog has nothing better).
///
/// Returns `None` only when the run carries no deadline at all.
pub fn effective_deadline(
    run: &crate::state::ActiveRun,
    max_run_seconds: Duration,
) -> Option<DateTime<Utc>> {
    let written = run.deadline?;
    let Some(started) = run.started_at else {
        // No anchor → cannot clamp; honour the written deadline.
        return Some(written);
    };
    let ceiling = chrono::Duration::from_std(max_run_seconds)
        .map(|d| started + d)
        // An absurd max_run_seconds that overflows chrono → no clamp.
        .unwrap_or(written);
    Some(written.min(ceiling))
}

/// Decide whether an active run has blown its per-board wall-clock budget.
///
/// Timing classification also verifies the recorded worker identity against
/// the kernel. The asynchronous loop owns signalling and grace-period waits.
///
/// The enforced deadline is the **clamped** [`effective_deadline`]
/// (`min(written, started_at + max_run_seconds)`) — not the raw written
/// deadline — so an untrusted far-future deadline cannot disable the kill.
///
/// * before the effective deadline → [`RunDeadlineAction::None`]
/// * past it (within `grace`) → [`RunDeadlineAction::Sigterm`]
/// * past it + `grace`, worker still alive → [`RunDeadlineAction::Sigkill`]
///
/// The classification measures overrun from the effective deadline. The run
/// loop admits one escalation, whose grace begins when SIGTERM is sent.
/// An exited leader can still own surviving group members; only a stopped
/// target collapses back to `None`. Signal and identity guards apply first.
pub fn decide_run_deadline(
    run: &crate::state::ActiveRun,
    now: DateTime<Utc>,
    grace: Duration,
    max_run_seconds: Duration,
    protected: &HashSet<i32>,
) -> RunDeadlineAction {
    let Some(deadline) = effective_deadline(run, max_run_seconds) else {
        return RunDeadlineAction::None;
    };
    if now <= deadline {
        return RunDeadlineAction::None;
    }
    let Some(pid) = decide_run_kill_request(run, protected) else {
        return RunDeadlineAction::None;
    };

    let overrun = now.signed_duration_since(deadline);
    let grace = chrono::Duration::from_std(grace).unwrap_or_else(|_| chrono::Duration::zero());
    if overrun > grace {
        RunDeadlineAction::Sigkill { pid }
    } else {
        RunDeadlineAction::Sigterm { pid }
    }
}

/// Return the recorded owner only when its target passes signal and identity guards.
/// Surviving descendants retain an owned group after leader exit. Missing saved
/// identity, unreadable live identity, and a replacement leader refuse escalation.
pub fn decide_run_kill_request(
    run: &crate::state::ActiveRun,
    protected: &HashSet<i32>,
) -> Option<i32> {
    let pid = run.pid?;
    if !is_signalable_run_pid(pid, protected) {
        return None;
    }
    let mut groups = protected_pgids(None);
    groups.extend(protected.iter().filter_map(|pid| signal::pgid_of(*pid)));
    let target = resolve_kill_target(run, pid, &groups);
    if !signal::verified_target(target, run.pid_start_time) || target.is_gone() {
        return None;
    }
    Some(pid)
}

/// Long-running heartbeat watchdog task.
///
/// **Warn-only for the orchestrator.** This loop never signals the
/// orchestrator pid. A stale heartbeat is surfaced (`warn!` + `/statusz`)
/// so an operator or out-of-band process supervisor can decide whether to
/// restart; the watchdog does not make that decision because the
/// orchestrator may legitimately be slow (GC, a slow LLM endpoint, a
/// debugger pause) and killing it would destroy in-flight work — exactly
/// the failure RUNTIME.md §3.2 and ROBUSTNESS.md §2.4 promise will not
/// happen. Run-worker enforcement (deadline/staleness) lives in
/// [`runs_loop`] and is unaffected.
pub async fn heartbeat_loop(
    paths: WorkspacePaths,
    thresholds: Thresholds,
    interval: Duration,
    _log: Arc<WatchdogLog>,
    seq_liveness: Arc<std::sync::Mutex<SeqLiveness>>,
    shutdown: Sender<()>,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut shutdown_rx = shutdown.subscribe();
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                let hb = reader::read_heartbeat(&paths);
                // Carried across ticks (shared with `/statusz`): tracks when
                // the heartbeat's `seq` cursor last advanced so a wedged loop
                // (fresh timestamp, frozen seq) is caught. A poisoned lock is
                // unreachable in practice (no panics under it); skip the tick.
                let obs = match seq_liveness.lock() {
                    Ok(mut tracker) => tracker.observe(hb.as_ref(), Utc::now(), &thresholds),
                    Err(_) => continue,
                };
                match obs.action {
                    HeartbeatAction::Nothing => {}
                    HeartbeatAction::Warn => {
                        warn!(
                            ?hb,
                            seq_age_seconds = ?obs.seq_age_seconds,
                            timestamp_age_seconds = ?obs.timestamp_age_seconds,
                            "heartbeat is stale (warn threshold)"
                        );
                    }
                    HeartbeatAction::Stale => {
                        // Deep-stale past the former kill threshold. We do
                        // NOT signal the orchestrator: restart is an
                        // operator / process-supervisor decision. Surface
                        // it loudly and move on.
                        warn!(
                            ?hb,
                            seq_age_seconds = ?obs.seq_age_seconds,
                            timestamp_age_seconds = ?obs.timestamp_age_seconds,
                            "orchestrator heartbeat is deeply stale; NOT killing it \
                             (orchestrator restart is a process-supervisor concern \
                             — see RUNTIME.md §3.2)"
                        );
                    }
                    HeartbeatAction::MissingHeartbeat => {
                        // Don't spam: just debug-level after the initial warn.
                        tracing::debug!("no heartbeat file present");
                    }
                }
            }
            _ = shutdown_rx.recv() => break,
        }
    }
}

/// Resource cleanup requires both the leader and all group members to stop.
fn run_processes_gone(run: &crate::state::ActiveRun) -> bool {
    run.pid.is_some_and(|pid| !signal::is_alive(pid))
        && run
            .pgid
            .is_none_or(|pgid| !signal::group_has_live_members(pgid))
}

/// One locked snapshot of orphan ownership, shared by its pending escalations.
struct OrphanBatch {
    _guard: WorkspaceGuard,
    paths: WorkspacePaths,
    heartbeat: Option<crate::state::Heartbeat>,
    runs: Vec<crate::state::ActiveRun>,
}

struct WorkspaceGuard(File);

impl Drop for WorkspaceGuard {
    fn drop(&mut self) {
        // A child can inherit the open file description during process creation.
        // Closing our descriptor alone would leave its lock held by that child.
        // SAFETY: this guard owns a valid descriptor until File is dropped.
        if unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) } != 0 {
            warn!(error = %std::io::Error::last_os_error(), "workspace unlock failed");
        }
    }
}

impl OrphanBatch {
    fn acquire(paths: &WorkspacePaths) -> Option<Arc<Self>> {
        // Never unlink the guard: Python and Rust must lock the same inode.
        let guard = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(paths.lock_guard())
            .ok()?;
        // SAFETY: guard owns this descriptor for the whole batch lifetime.
        if unsafe { libc::flock(guard.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return None;
        }
        let guard = WorkspaceGuard(guard);
        if metadata_writer_may_be_live(paths) {
            return None;
        }
        let heartbeat = reader::read_heartbeat(paths);
        let runs = reader::read_active_runs(paths);
        if !runs.iter().any(producer_is_dead) {
            return None;
        }
        Some(Arc::new(Self {
            _guard: guard,
            paths: paths.clone(),
            heartbeat,
            runs,
        }))
    }
}

/// Metadata-only writers cannot be excluded by the kernel lease alone.
fn metadata_writer_may_be_live(paths: &WorkspacePaths) -> bool {
    let bytes = match std::fs::read(paths.lock()) {
        Ok(bytes) => bytes,
        Err(error) => return error.kind() != std::io::ErrorKind::NotFound,
    };
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return true;
    };
    if value["owner_id"]
        .as_str()
        .is_some_and(|owner| !owner.is_empty())
    {
        // Exclusive flock proves that this recorded kernel lease ended.
        return false;
    }
    let Some(pid) = value["pid"]
        .as_i64()
        .and_then(|pid| i32::try_from(pid).ok())
    else {
        return true;
    };
    signal::is_same_process(pid, value["start_time"].as_f64())
}

fn finalize_orphan(batch: &OrphanBatch, run: &crate::state::ActiveRun) {
    let paths = &batch.paths;
    let path = paths.active_runs_dir().join(format!("{}.json", run.run_id));
    let current = std::fs::read(&path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<crate::state::ActiveRun>(&bytes).ok());
    if current.is_some_and(|current| {
        current.pid == run.pid
            && current.pid_start_time == run.pid_start_time
            && current.producer_pid == run.producer_pid
            && current.producer_start_time == run.producer_start_time
            && current.snapshot_path == run.snapshot_path
    }) && producer_is_dead(run)
        && run_processes_gone(run)
    {
        reap::reap_orphaned_snapshot(run);
        #[cfg(test)]
        tests::observe_orphan_removal(&path);
        if let Err(error) = std::fs::remove_file(path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                warn!(%error, run_id = %run.run_id, "orphan state cleanup failed");
            }
        }
    }
}

fn finish_owner(owners: &mut HashMap<(i32, u64), Option<Arc<OrphanBatch>>>, owner: (i32, u64)) {
    if let Some(Some(batch)) = owners.remove(&owner) {
        for run in &batch.runs {
            if run.pid == Some(owner.0) && run.pid_start_time.map(f64::to_bits) == Some(owner.1) {
                finalize_orphan(&batch, run);
            }
        }
    }
}

async fn enforce_run(
    paths: &WorkspacePaths,
    run: &crate::state::ActiveRun,
    target: KillTarget,
    trigger: Trigger,
    grace: Duration,
    log: &Arc<WatchdogLog>,
    ledger: Option<&Arc<AuditLedger>>,
) {
    let outcome = escalate_owned_target(target, run.pid_start_time, grace).await;
    let action = Action {
        ts: Utc::now(),
        trigger,
        pid: target.leader_pid(),
        run_id: Some(run.run_id.clone()),
        outcome: outcome.into(),
    };
    let (action_log, action_ledger) = (log.clone(), ledger.cloned());
    if let Err(error) = tokio::task::spawn_blocking(move || {
        record_action(&action_log, action_ledger.as_ref(), action);
    })
    .await
    {
        warn!(%error, "termination action recording failed");
    }
    if outcome == signal::EscalationOutcome::Failed || !target.is_gone() {
        return;
    }
    if trigger == Trigger::KillRequest {
        reader::clear_kill_request(paths, &run.run_id);
    }
}

/// Enforce orphan, requested, deadline, and stale-run termination in that order.
///
/// Each verified owner has one pending escalation. Independent owners advance
/// concurrently, so one grace period cannot delay another run's deadline.
/// Integrity scans run in a separate task and perform filesystem work off-thread.
#[allow(clippy::too_many_arguments)]
pub async fn runs_loop(
    paths: WorkspacePaths,
    thresholds: Thresholds,
    interval: Duration,
    log: Arc<WatchdogLog>,
    ledger: Option<Arc<AuditLedger>>,
    diff: DiffContainmentConfig,
    promotion_gate: PromotionGateConfig,
    divergence: DivergenceConfig,
    shutdown: Sender<()>,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut shutdown_rx = shutdown.subscribe();
    let audits = tokio::spawn(integrity_loop(
        paths.clone(),
        interval,
        ledger.clone(),
        diff,
        promotion_gate,
        divergence,
        shutdown.subscribe(),
    ));
    let mut pending = FuturesUnordered::new();
    let mut owners = HashMap::new();
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                let observed_heartbeat = reader::read_heartbeat(&paths);
                let observed_runs = reader::read_active_runs(&paths);
                let batch = observed_runs.iter().any(producer_is_dead)
                    .then(|| OrphanBatch::acquire(&paths)).flatten();
                let heartbeat = batch.as_ref().and_then(|batch| batch.heartbeat.clone())
                    .or(observed_heartbeat);
                let heartbeat_pid = heartbeat.as_ref().and_then(|hb| hb.pid);
                let protected: HashSet<i32> = heartbeat_pid.into_iter().collect();
                let groups = protected_pgids(heartbeat_pid);
                let requests = reader::read_kill_requests(&paths);
                let runs = batch.as_ref().map(|batch| batch.runs.clone())
                    .unwrap_or(observed_runs);
                for run in runs {
                    let now = Utc::now();
                    let orphaned = batch.is_some() && producer_is_dead(&run);
                    let trigger = if orphaned {
                        Some(Trigger::OrchestratorReap)
                    } else if requests.contains(&run.run_id) {
                        Some(Trigger::KillRequest)
                    } else if !thresholds.run_deadline_kill_disabled && !matches!(
                        decide_run_deadline(&run, now, thresholds.run_kill_grace,
                            thresholds.max_run_seconds, &protected), RunDeadlineAction::None
                    ) {
                        Some(Trigger::RunDeadline)
                    } else {
                        match decide_run(&run, now, &thresholds) {
                            RunAction::Kill { .. } => Some(Trigger::RunStale),
                            RunAction::Warn => {
                                warn!(run_id = %run.run_id, "active run is stalled");
                                None
                            }
                            RunAction::Nothing => None,
                        }
                    };
                    let Some(trigger) = trigger else { continue; };
                    let Some(pid) = decide_run_kill_request(&run, &protected) else {
                        if let Some(batch) = batch.as_ref().filter(|_| orphaned) {
                            if run_processes_gone(&run) {
                                finalize_orphan(batch, &run);
                            }
                        }
                        continue;
                    };
                    let owner = (pid, run.pid_start_time.unwrap().to_bits());
                    if let Some(pending_batch) = owners.get_mut(&owner) {
                        if orphaned {
                            *pending_batch = batch.clone();
                        }
                        continue;
                    }
                    owners.insert(owner, batch.clone().filter(|_| orphaned));
                    let target = resolve_kill_target(&run, pid, &groups);
                    let grace = if trigger == Trigger::RunStale { thresholds.grace }
                        else { thresholds.run_kill_grace };
                    let (paths, log, ledger) = (paths.clone(), log.clone(), ledger.clone());
                    pending.push(async move {
                        enforce_run(&paths, &run, target, trigger, grace, &log, ledger.as_ref()).await;
                        owner
                    });
                }
            }
            Some(owner) = pending.next(), if !pending.is_empty() => {
                finish_owner(&mut owners, owner);
            }
            _ = shutdown_rx.recv() => break,
        }
    }
    // A shutdown must finish escalations that already sent SIGTERM.
    while let Some(owner) = pending.next().await {
        finish_owner(&mut owners, owner);
    }
    audits.abort();
}

/// Integrity reads run in the blocking pool, outside the deadline polling task.
async fn integrity_loop(
    paths: WorkspacePaths,
    interval: Duration,
    ledger: Option<Arc<AuditLedger>>,
    diff: DiffContainmentConfig,
    promotion_gate: PromotionGateConfig,
    divergence: DivergenceConfig,
    mut shutdown: tokio::sync::broadcast::Receiver<()>,
) {
    let mut state = (
        crate::ledger::TransitionObserver::new(),
        HashSet::new(),
        HashSet::new(),
        HashSet::new(),
    );
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = ticker.tick() => {}
            _ = shutdown.recv() => return,
        }
        let (paths, ledger, diff, promotion_gate, divergence) = (
            paths.clone(),
            ledger.clone(),
            diff.clone(),
            promotion_gate.clone(),
            divergence.clone(),
        );
        let task = tokio::task::spawn_blocking(move || {
            if let Some(ledger) = ledger.as_ref() {
                observe_transitions(&paths, ledger, &mut state.0);
            }
            if diff.enabled {
                run_diff_containment_scan(&paths, &diff, ledger.as_ref(), &mut state.1);
            }
            if promotion_gate.enabled {
                run_promotion_gate_scan(&paths, &promotion_gate, ledger.as_ref(), &mut state.2);
            }
            if divergence.enabled {
                run_divergence_audit(&paths, &divergence, ledger.as_ref(), &mut state.3);
            }
            state
        });
        tokio::select! {
            result = task => match result {
                Ok(result) => state = result,
                Err(error) => { warn!(%error, "integrity scan stopped"); return; }
            },
            _ = shutdown.recv() => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{ActiveRun, Heartbeat};
    use chrono::Duration as ChDuration;

    type RemovalObserver = (std::path::PathBuf, Box<dyn FnOnce()>);
    thread_local! {
        static REMOVAL_OBSERVER: std::cell::RefCell<Option<RemovalObserver>> =
            const { std::cell::RefCell::new(None) };
    }

    pub(super) fn observe_orphan_removal(path: &std::path::Path) {
        let callback = REMOVAL_OBSERVER.with(|observer| {
            let mut observer = observer.borrow_mut();
            if observer
                .as_ref()
                .is_some_and(|(expected, _)| expected == path)
            {
                observer.take().map(|(_, callback)| callback)
            } else {
                None
            }
        });
        if let Some(callback) = callback {
            callback();
        }
    }

    struct ObserveRemoval;
    impl Drop for ObserveRemoval {
        fn drop(&mut self) {
            REMOVAL_OBSERVER.with(|observer| observer.borrow_mut().take());
        }
    }

    fn writer_guard(paths: &WorkspacePaths) -> Option<File> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(paths.lock_guard())
            .unwrap();
        // SAFETY: the returned File owns the descriptor and releases the lease.
        (unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0)
            .then_some(file)
    }

    fn publish_run(paths: &WorkspacePaths, run: &ActiveRun) {
        std::fs::create_dir_all(paths.active_runs_dir()).unwrap();
        std::fs::write(
            paths.active_runs_dir().join(format!("{}.json", run.run_id)),
            serde_json::to_vec(run).unwrap(),
        )
        .unwrap();
    }

    fn publish_heartbeat(paths: &WorkspacePaths, pid: i32) {
        std::fs::write(
            paths.heartbeat(),
            serde_json::to_vec(&Heartbeat {
                pid: Some(pid),
                ..Default::default()
            })
            .unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn orphan_guard_shares_python_flock_and_preserves_the_guard_inode() {
        use std::os::unix::fs::MetadataExt;
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        publish_run(
            &paths,
            &ActiveRun {
                run_id: "orphan".into(),
                producer_pid: Some(99_999_999),
                producer_start_time: Some(1.0),
                ..Default::default()
            },
        );
        let batch = OrphanBatch::acquire(&paths).unwrap();
        let inode = std::fs::metadata(paths.lock_guard()).unwrap().ino();
        let attempt = || {
            let output = std::process::Command::new("python3").args([
                "-c",
                "import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)\ntry:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n print('acquired')\nexcept BlockingIOError:\n print('blocked')\nfinally:\n os.close(fd)",
            ]).arg(paths.lock_guard()).output().unwrap();
            assert!(output.status.success());
            String::from_utf8(output.stdout).unwrap()
        };
        assert_eq!(attempt().trim(), "blocked");
        drop(batch);
        assert_eq!(attempt().trim(), "acquired");
        assert_eq!(std::fs::metadata(paths.lock_guard()).unwrap().ino(), inode);
    }

    #[tokio::test]
    async fn writer_and_live_or_unknown_producer_refuse_stale_heartbeat_reaping() {
        let mut failures = Vec::new();
        for case in [
            "writer",
            "metadata-writer",
            "live-producer",
            "missing-producer",
            "missing-token",
        ] {
            let root = tempfile::tempdir().unwrap();
            let paths = WorkspacePaths::new(root.path().into());
            let mut worker = Sleeper::spawn();
            let run = ActiveRun {
                run_id: "retained-worker".into(),
                pid: Some(worker.pid()),
                pid_start_time: signal::pid_start_time(worker.pid()),
                producer_pid: match case {
                    "writer" | "metadata-writer" => Some(99_999_999),
                    "missing-producer" => None,
                    _ => Some(std::process::id() as i32),
                },
                producer_start_time: match case {
                    "writer" | "metadata-writer" => Some(1.0),
                    "live-producer" => signal::pid_start_time(std::process::id() as i32),
                    _ => None,
                },
                ..Default::default()
            };
            publish_run(&paths, &run);
            publish_heartbeat(&paths, 99_999_999);
            let guard = (case == "writer").then(|| writer_guard(&paths).unwrap());
            if case == "metadata-writer" {
                std::fs::write(
                    paths.lock(),
                    serde_json::to_vec(&serde_json::json!({
                        "pid": std::process::id(),
                        "start_time": signal::pid_start_time(std::process::id() as i32)
                    }))
                    .unwrap(),
                )
                .unwrap();
            }
            let log = Arc::new(WatchdogLog::new());
            let (shutdown, _) = tokio::sync::broadcast::channel(1);
            let task = start_run_watchdog(
                paths.clone(),
                Thresholds::default(),
                log.clone(),
                None,
                shutdown.clone(),
            );
            tokio::time::sleep(Duration::from_millis(70)).await;
            let alive = worker.0.try_wait().unwrap().is_none();
            publish_heartbeat(&paths, std::process::id() as i32);
            let _ = shutdown.send(());
            task.await.unwrap();
            if !alive || !log.is_empty() || reader::read_active_runs(&paths).len() != 1 {
                failures.push(case);
            }
            drop(guard);
        }
        assert!(
            failures.is_empty(),
            "valid or unproven ownership was reaped: {failures:?}"
        );
    }

    #[test]
    fn orphan_cleanup_releases_lock_while_a_duplicate_descriptor_remains_open() {
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        publish_run(
            &paths,
            &ActiveRun {
                run_id: "orphan".into(),
                pid: Some(99_999_998),
                pid_start_time: Some(1.0),
                producer_pid: Some(99_999_999),
                producer_start_time: Some(1.0),
                ..Default::default()
            },
        );
        let batch = OrphanBatch::acquire(&paths).expect("orphan cleanup acquires the lock");
        // A descriptor inherited during process creation refers to this same
        // open file description until the child closes it or executes.
        let duplicate = batch._guard.0.try_clone().unwrap();
        assert!(writer_guard(&paths).is_none());
        drop(batch);
        assert!(
            writer_guard(&paths).is_some(),
            "completed cleanup must release its lock even while a duplicate stays open"
        );
        drop(duplicate);
    }

    #[tokio::test]
    async fn orphan_record_comparison_excludes_a_replacement_writer_until_unlink() {
        use std::cell::Cell;
        use std::rc::Rc;
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        let prior = ActiveRun {
            run_id: "reused-run".into(),
            pid: Some(99_999_998),
            pid_start_time: Some(1.0),
            producer_pid: Some(99_999_999),
            producer_start_time: Some(1.0),
            ..Default::default()
        };
        publish_run(&paths, &prior);
        publish_heartbeat(&paths, 99_999_999);
        let worker = Sleeper::spawn();
        let replacement = ActiveRun {
            pid: Some(worker.pid()),
            pid_start_time: signal::pid_start_time(worker.pid()),
            producer_pid: Some(std::process::id() as i32),
            producer_start_time: signal::pid_start_time(std::process::id() as i32),
            ..prior
        };
        let seen = Rc::new(Cell::new(false));
        let replaced = Rc::new(Cell::new(false));
        let (observed, published) = (seen.clone(), replaced.clone());
        let (observer_paths, record) = (paths.clone(), replacement.clone());
        REMOVAL_OBSERVER.with(|observer| {
            *observer.borrow_mut() = Some((
                paths.active_runs_dir().join("reused-run.json"),
                Box::new(move || {
                    observed.set(true);
                    if let Some(_guard) = writer_guard(&observer_paths) {
                        publish_heartbeat(&observer_paths, std::process::id() as i32);
                        publish_run(&observer_paths, &record);
                        published.set(true);
                    }
                }),
            ));
        });
        let _observer = ObserveRemoval;
        let log = Arc::new(WatchdogLog::new());
        let (shutdown, _) = tokio::sync::broadcast::channel(1);
        let task = start_run_watchdog(
            paths.clone(),
            Thresholds::default(),
            log,
            None,
            shutdown.clone(),
        );
        tokio::time::timeout(Duration::from_secs(1), async {
            while !seen.get() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let _ = shutdown.send(());
        task.await.unwrap();
        if !replaced.get() {
            let _guard = writer_guard(&paths).expect("orphan batch retained its completed lease");
            publish_heartbeat(&paths, std::process::id() as i32);
            publish_run(&paths, &replacement);
        }
        let records = reader::read_active_runs(&paths);
        assert_eq!(
            records.len(),
            1,
            "orphan finalization deleted the replacement owner's record"
        );
        assert_eq!(records[0].pid, replacement.pid);
        assert!(
            !replaced.get(),
            "replacement writer entered during compare/delete"
        );
    }

    #[tokio::test]
    async fn orphan_batch_retains_writer_guard_while_independent_deadlines_advance() {
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        let group = crate::test_process_group::OwnedGroup::spawn(true);
        let orphan = ActiveRun {
            run_id: "orphan".into(),
            pid: Some(group.leader),
            pgid: Some(group.leader),
            pid_start_time: Some(group.start_time),
            producer_pid: Some(99_999_999),
            producer_start_time: Some(1.0),
            ..Default::default()
        };
        publish_run(&paths, &orphan);
        // A live unrelated heartbeat cannot conceal a positively orphaned producer.
        publish_heartbeat(&paths, std::process::id() as i32);
        let log = Arc::new(WatchdogLog::new());
        let (shutdown, _) = tokio::sync::broadcast::channel(1);
        let task = start_run_watchdog(
            paths.clone(),
            Thresholds {
                run_kill_grace: Duration::from_millis(300),
                ..Thresholds::default()
            },
            log.clone(),
            None,
            shutdown.clone(),
        );
        tokio::time::timeout(Duration::from_secs(1), async {
            while writer_guard(&paths).is_some() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let worker = Sleeper::spawn();
        let deadline = ActiveRun {
            run_id: "independent-deadline".into(),
            pid: Some(worker.pid()),
            pid_start_time: signal::pid_start_time(worker.pid()),
            producer_pid: Some(std::process::id() as i32),
            producer_start_time: signal::pid_start_time(std::process::id() as i32),
            deadline: Some(Utc::now() - ChDuration::seconds(1)),
            ..Default::default()
        };
        publish_run(&paths, &deadline);
        let independent = tokio::time::timeout(Duration::from_millis(200), async {
            while signal::is_alive(worker.pid()) {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .is_ok();
        let orphan_survived = signal::is_alive(group.descendant);
        let guard_held = writer_guard(&paths).is_none();
        let _ = shutdown.send(());
        task.await.unwrap();
        assert!(
            independent && orphan_survived,
            "an orphan grace delayed an independent deadline"
        );
        assert!(
            guard_held,
            "orphan escalation released writer ownership before group exit"
        );
        assert!(writer_guard(&paths).is_some());
        assert!(!paths.active_runs_dir().join("orphan.json").exists());
        assert!(log
            .snapshot()
            .iter()
            .any(|action| action.trigger == Trigger::RunDeadline));
    }

    #[tokio::test]
    async fn producer_death_attaches_writer_guard_to_an_existing_deadline_escalation() {
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        let group = crate::test_process_group::OwnedGroup::spawn(false);
        let mut producer = Sleeper::spawn();
        let run = ActiveRun {
            run_id: "pending-deadline".into(),
            pid: Some(group.leader),
            pgid: Some(group.leader),
            pid_start_time: Some(group.start_time),
            producer_pid: Some(producer.pid()),
            producer_start_time: signal::pid_start_time(producer.pid()),
            deadline: Some(Utc::now() - ChDuration::seconds(1)),
            ..Default::default()
        };
        publish_run(&paths, &run);
        let log = Arc::new(WatchdogLog::new());
        let (shutdown, _) = tokio::sync::broadcast::channel(1);
        let task = start_run_watchdog(
            paths.clone(),
            Thresholds {
                run_kill_grace: Duration::from_millis(300),
                ..Thresholds::default()
            },
            log.clone(),
            None,
            shutdown.clone(),
        );
        tokio::time::timeout(Duration::from_secs(1), async {
            while signal::is_alive(group.leader) {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert!(signal::is_alive(group.descendant));
        assert!(writer_guard(&paths).is_some());
        producer.0.kill().unwrap();
        producer.reap();
        let adopted = tokio::time::timeout(Duration::from_millis(200), async {
            while writer_guard(&paths).is_some() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .is_ok();
        let descendant_retained = signal::is_alive(group.descendant);
        let _ = shutdown.send(());
        task.await.unwrap();
        assert!(adopted && descendant_retained);
        assert!(writer_guard(&paths).is_some());
        assert!(!paths
            .active_runs_dir()
            .join("pending-deadline.json")
            .exists());
        let actions = log.snapshot();
        assert_eq!(
            actions.len(),
            1,
            "producer death duplicated an admitted escalation"
        );
        assert_eq!(actions[0].trigger, Trigger::RunDeadline);
    }

    #[test]
    fn orphan_finalization_retains_records_with_replaced_producer_or_snapshot() {
        for replacement in ["producer", "snapshot"] {
            let root = tempfile::tempdir().unwrap();
            let paths = WorkspacePaths::new(root.path().into());
            let (snapshot, snapshot_path) = make_ephemeral_snapshot();
            let run = ActiveRun {
                run_id: "replaced-owner".into(),
                pid: Some(99_999_998),
                pid_start_time: Some(1.0),
                producer_pid: Some(99_999_999),
                producer_start_time: Some(1.0),
                snapshot_path: Some(snapshot_path),
                ..Default::default()
            };
            publish_run(&paths, &run);
            let batch = OrphanBatch::acquire(&paths).unwrap();
            let mut changed = run.clone();
            match replacement {
                "producer" => {
                    changed.producer_pid = Some(std::process::id() as i32);
                    changed.producer_start_time = signal::pid_start_time(std::process::id() as i32);
                }
                _ => changed.snapshot_path = None,
            }
            publish_run(&paths, &changed);
            finalize_orphan(&batch, &run);
            assert!(paths.active_runs_dir().join("replaced-owner.json").exists());
            assert!(
                snapshot.path().exists(),
                "changed ownership released the prior snapshot"
            );
        }
    }

    fn start_run_watchdog(
        paths: WorkspacePaths,
        thresholds: Thresholds,
        log: Arc<WatchdogLog>,
        ledger: Option<Arc<AuditLedger>>,
        shutdown: Sender<()>,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(runs_loop(
            paths,
            thresholds,
            Duration::from_millis(10),
            log,
            ledger,
            DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(crate::diff_containment::DiffContainmentFindings::new()),
            },
            PromotionGateConfig {
                enabled: false,
                findings: Arc::new(crate::promotion_gate::PromotionGateFindings::new()),
            },
            DivergenceConfig {
                enabled: false,
                findings: Arc::new(crate::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 60,
            },
            shutdown,
        ))
    }

    async fn await_orphan_cleanup(
        paths: &WorkspacePaths,
        thresholds: &Thresholds,
        log: &Arc<WatchdogLog>,
        ledger: Option<&Arc<AuditLedger>>,
    ) {
        let heartbeat = Heartbeat {
            pid: Some(99_999_999),
            ..Default::default()
        };
        std::fs::write(paths.heartbeat(), serde_json::to_vec(&heartbeat).unwrap()).unwrap();
        for mut run in reader::read_active_runs(paths) {
            run.producer_pid = heartbeat.pid;
            run.producer_start_time = Some(1.0);
            publish_run(paths, &run);
        }
        let (shutdown, _) = tokio::sync::broadcast::channel(1);
        let task = start_run_watchdog(
            paths.clone(),
            *thresholds,
            log.clone(),
            ledger.cloned(),
            shutdown.clone(),
        );
        let result = tokio::time::timeout(Duration::from_secs(2), async {
            while !reader::read_active_runs(paths).is_empty() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await;
        let _ = shutdown.send(());
        task.await.unwrap();
        result.expect("orphan ownership was not finalized");
    }

    #[tokio::test]
    async fn every_trigger_finishes_descendants_when_leader_exited_before_polling() {
        let mut failures = Vec::new();
        for trigger in [
            Trigger::KillRequest,
            Trigger::RunDeadline,
            Trigger::RunStale,
            Trigger::OrchestratorReap,
        ] {
            let group = crate::test_process_group::OwnedGroup::spawn(true);
            let root = tempfile::tempdir().unwrap();
            let paths = WorkspacePaths::new(root.path().into());
            std::fs::create_dir_all(paths.active_runs_dir()).unwrap();
            std::fs::create_dir_all(paths.kill_requests_dir()).unwrap();
            let run = ActiveRun {
                run_id: "owned-worker".into(),
                producer_pid: (trigger == Trigger::OrchestratorReap).then_some(99_999_999),
                producer_start_time: (trigger == Trigger::OrchestratorReap).then_some(1.0),
                pid: Some(group.leader),
                pgid: Some(group.leader),
                pid_start_time: Some(group.start_time),
                last_progress: (trigger == Trigger::RunStale)
                    .then(|| Utc::now() - ChDuration::hours(1)),
                deadline: (trigger == Trigger::RunDeadline)
                    .then(|| Utc::now() - ChDuration::seconds(1)),
                ..Default::default()
            };
            let record = paths.active_runs_dir().join("owned-worker.json");
            std::fs::write(&record, serde_json::to_vec(&run).unwrap()).unwrap();
            let heartbeat = Heartbeat {
                pid: Some(if trigger == Trigger::OrchestratorReap {
                    99_999_999
                } else {
                    std::process::id() as i32
                }),
                ..Default::default()
            };
            std::fs::write(paths.heartbeat(), serde_json::to_vec(&heartbeat).unwrap()).unwrap();
            if trigger == Trigger::KillRequest {
                std::fs::write(paths.kill_requests_dir().join("owned-worker"), b"{}").unwrap();
            }
            let log = Arc::new(WatchdogLog::new());
            let (shutdown, _) = tokio::sync::broadcast::channel(1);
            let task = start_run_watchdog(
                paths,
                Thresholds {
                    run_deadline_kill_disabled: trigger != Trigger::RunDeadline,
                    run_kill_grace: Duration::from_millis(20),
                    grace: Duration::from_millis(20),
                    ..Thresholds::default()
                },
                log.clone(),
                None,
                shutdown.clone(),
            );
            let stopped = tokio::time::timeout(Duration::from_millis(500), async {
                while signal::is_alive(group.descendant) {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .is_ok();
            let _ = shutdown.send(());
            task.await.unwrap();
            if !stopped
                || !log
                    .snapshot()
                    .iter()
                    .any(|action| action.trigger == trigger)
            {
                failures.push(trigger);
            }
        }
        assert!(failures.is_empty(), "descendants survived: {failures:?}");
    }

    #[tokio::test]
    async fn every_trigger_rejects_missing_or_mismatched_process_identity() {
        let mut failures = Vec::new();
        for trigger in [
            Trigger::KillRequest,
            Trigger::RunDeadline,
            Trigger::RunStale,
            Trigger::OrchestratorReap,
        ] {
            for missing in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let paths = WorkspacePaths::new(root.path().into());
                std::fs::create_dir_all(paths.active_runs_dir()).unwrap();
                std::fs::create_dir_all(paths.kill_requests_dir()).unwrap();
                let mut sleeper = Sleeper::spawn();
                let pid = sleeper.pid();
                let run = ActiveRun {
                    run_id: "owned-worker".into(),
                    producer_pid: (trigger == Trigger::OrchestratorReap).then_some(99_999_999),
                    producer_start_time: (trigger == Trigger::OrchestratorReap).then_some(1.0),
                    pid: Some(pid),
                    pid_start_time: if missing {
                        None
                    } else {
                        signal::pid_start_time(pid).map(|value| value + 1.0)
                    },
                    last_progress: (trigger == Trigger::RunStale)
                        .then(|| Utc::now() - ChDuration::hours(1)),
                    deadline: (trigger == Trigger::RunDeadline)
                        .then(|| Utc::now() - ChDuration::seconds(1)),
                    ..Default::default()
                };
                let record = paths.active_runs_dir().join("owned-worker.json");
                std::fs::write(&record, serde_json::to_vec(&run).unwrap()).unwrap();
                let heartbeat = Heartbeat {
                    pid: Some(if trigger == Trigger::OrchestratorReap {
                        99_999_999
                    } else {
                        std::process::id() as i32
                    }),
                    ..Default::default()
                };
                std::fs::write(paths.heartbeat(), serde_json::to_vec(&heartbeat).unwrap()).unwrap();
                if trigger == Trigger::KillRequest {
                    std::fs::write(paths.kill_requests_dir().join("owned-worker"), b"{}").unwrap();
                }
                let log = Arc::new(WatchdogLog::new());
                let (shutdown, _) = tokio::sync::broadcast::channel(1);
                let task = start_run_watchdog(
                    paths,
                    Thresholds {
                        run_deadline_kill_disabled: trigger != Trigger::RunDeadline,
                        run_kill_grace: Duration::from_millis(20),
                        grace: Duration::from_millis(20),
                        ..Thresholds::default()
                    },
                    log.clone(),
                    None,
                    shutdown.clone(),
                );
                tokio::time::sleep(Duration::from_millis(60)).await;
                let _ = shutdown.send(());
                task.await.unwrap();
                let alive = sleeper.0.try_wait().unwrap().is_none();
                if !alive || !record.exists() || !log.is_empty() {
                    failures.push(format!("{trigger:?}, missing token={missing}: alive={alive}, record retained={}, actions={}", record.exists(), log.snapshot().len()));
                }
            }
        }
        assert!(
            failures.is_empty(),
            "unverified owners were affected: {failures:?}"
        );
    }

    #[tokio::test]
    async fn simultaneous_deadlines_share_one_grace_period() {
        use std::io::BufRead;
        let root = tempfile::tempdir().unwrap();
        let paths = WorkspacePaths::new(root.path().into());
        std::fs::create_dir_all(paths.active_runs_dir()).unwrap();
        let mut sleepers = Vec::new();
        for index in 0..3 {
            let mut child = std::process::Command::new("sh")
                .args(["-c", "trap '' TERM; printf 'ready\\n'; exec sleep 60"])
                .stdout(std::process::Stdio::piped())
                .spawn()
                .unwrap();
            let mut ready = String::new();
            std::io::BufReader::new(child.stdout.take().unwrap())
                .read_line(&mut ready)
                .unwrap();
            assert_eq!(ready.trim(), "ready");
            let sleeper = Sleeper(child);
            let run = ActiveRun {
                run_id: format!("worker-{index}"),
                pid: Some(sleeper.pid()),
                pid_start_time: signal::pid_start_time(sleeper.pid()),
                deadline: Some(Utc::now() - ChDuration::seconds(1)),
                ..Default::default()
            };
            std::fs::write(
                paths.active_runs_dir().join(format!("worker-{index}.json")),
                serde_json::to_vec(&run).unwrap(),
            )
            .unwrap();
            if index == 0 {
                let alias = ActiveRun {
                    run_id: "same-owner".into(),
                    ..run.clone()
                };
                std::fs::write(
                    paths.active_runs_dir().join("same-owner.json"),
                    serde_json::to_vec(&alias).unwrap(),
                )
                .unwrap();
            }
            sleepers.push(sleeper);
        }
        let log = Arc::new(WatchdogLog::new());
        let (shutdown, _) = tokio::sync::broadcast::channel(1);
        let task = start_run_watchdog(
            paths,
            Thresholds {
                run_kill_grace: Duration::from_millis(300),
                ..Thresholds::default()
            },
            log.clone(),
            None,
            shutdown.clone(),
        );
        let started = std::time::Instant::now();
        tokio::time::timeout(Duration::from_secs(3), async {
            while log.snapshot().len() < 3 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let elapsed = started.elapsed();
        let _ = shutdown.send(());
        task.await.unwrap();
        assert!(
            elapsed < Duration::from_millis(750),
            "deadlines serialized: {elapsed:?}"
        );
        assert_eq!(
            log.snapshot().len(),
            3,
            "one escalation per process identity"
        );
        assert!(sleepers
            .iter()
            .all(|sleeper| !signal::is_alive(sleeper.pid())));
    }

    fn thresholds() -> Thresholds {
        Thresholds::default()
    }

    /// Tight thresholds for tests that need to exercise warn/kill paths
    /// without waiting hundreds of seconds.
    fn tight_run_thresholds() -> Thresholds {
        Thresholds {
            run_stale_warn: Duration::from_secs(30),
            run_stale_kill: Duration::from_secs(120),
            ..Thresholds::default()
        }
    }

    fn no_protected() -> HashSet<i32> {
        HashSet::new()
    }

    /// A spawned sleeper child: alive for the test, signalable (not our
    /// own pid, not 0/1), and reaped on drop.
    struct Sleeper(std::process::Child);

    impl Sleeper {
        fn spawn() -> Self {
            let child = std::process::Command::new("sleep")
                .arg("600")
                .spawn()
                .expect("spawn sleeper");
            Self(child)
        }
        fn pid(&self) -> i32 {
            self.0.id() as i32
        }
        /// Reap the (killed) child so it is not left a zombie. A zombie pid
        /// still answers `kill(pid, 0)`, so a liveness assertion in a test
        /// must clear the zombie first — the real watchdog never parents
        /// these workers (the dead orchestrator's children are reparented to
        /// init, which reaps them), so this is a test-only concern.
        fn reap(&mut self) {
            let _ = self.0.wait();
        }
    }

    impl Drop for Sleeper {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[test]
    fn fresh_heartbeat_is_nothing() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(123),
            last_heartbeat: Some(now - ChDuration::seconds(1)),
            ..Default::default()
        };
        assert_eq!(
            decide_heartbeat(Some(&hb), now, &thresholds()),
            HeartbeatAction::Nothing
        );
    }

    #[test]
    fn warn_threshold_for_heartbeat() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(123),
            last_heartbeat: Some(now - ChDuration::seconds(45)),
            ..Default::default()
        };
        assert_eq!(
            decide_heartbeat(Some(&hb), now, &thresholds()),
            HeartbeatAction::Warn
        );
    }

    #[test]
    fn deep_stale_heartbeat_warns_not_kills() {
        // Past the former kill threshold (default 90s) the watchdog must
        // NOT kill the orchestrator — it escalates the warning to `Stale`.
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(999),
            last_heartbeat: Some(now - ChDuration::seconds(100)),
            ..Default::default()
        };
        assert_eq!(
            decide_heartbeat(Some(&hb), now, &thresholds()),
            HeartbeatAction::Stale
        );
    }

    /// Core invariant of the §0 fix: `decide_heartbeat` must NEVER return a
    /// kill for the orchestrator (heartbeat) pid — at *any* staleness. The
    /// `HeartbeatAction` enum has no `Kill` variant by construction, but
    /// this test pins the behavioral guarantee against future regressions:
    /// we sweep ages from "fresh" through "absurdly stale" and assert every
    /// outcome is warn-only (`Nothing`/`Warn`/`Stale`/`MissingHeartbeat`),
    /// for several pids including init/sentinel values.
    #[test]
    fn decide_heartbeat_never_kills_the_orchestrator() {
        let now = Utc::now();
        let t = thresholds();
        // A wide sweep of staleness in seconds, well past every threshold
        // (kill default = 90s; we go to a full day).
        let ages = [
            0i64, 1, 5, 29, 30, 31, 60, 89, 90, 91, 120, 300, 600, 3_600, 86_400, 1_000_000,
        ];
        // Including the orchestrator pid, init, and sentinel pids.
        let pids = [Some(424_242), Some(1), Some(0), Some(-1), None];
        for &pid in &pids {
            for &age in &ages {
                let hb = Heartbeat {
                    pid,
                    last_heartbeat: Some(now - ChDuration::seconds(age)),
                    ..Default::default()
                };
                let action = decide_heartbeat(Some(&hb), now, &t);
                assert!(
                    matches!(
                        action,
                        HeartbeatAction::Nothing
                            | HeartbeatAction::Warn
                            | HeartbeatAction::Stale
                            | HeartbeatAction::MissingHeartbeat
                    ),
                    "decide_heartbeat must never kill the orchestrator: \
                     pid={pid:?} age={age}s yielded {action:?}",
                );
            }
        }
    }

    #[test]
    fn missing_heartbeat_file() {
        let now = Utc::now();
        assert_eq!(
            decide_heartbeat(None, now, &thresholds()),
            HeartbeatAction::MissingHeartbeat
        );
    }

    #[test]
    fn missing_last_heartbeat_field() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(1),
            ..Default::default()
        };
        assert_eq!(
            decide_heartbeat(Some(&hb), now, &thresholds()),
            HeartbeatAction::MissingHeartbeat
        );
    }

    #[test]
    fn fresh_run_is_nothing() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            last_progress: Some(now - ChDuration::seconds(5)),
            ..Default::default()
        };
        assert_eq!(decide_run(&run, now, &thresholds()), RunAction::Nothing);
    }

    #[test]
    fn stale_run_warns_then_kills() {
        // Uses tight thresholds (warn=30s, kill=120s, no budget) to exercise
        // the staleness path without needing 600s of elapsed time.
        let t = tight_run_thresholds();
        let now = Utc::now();
        let mut run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            last_progress: Some(now - ChDuration::seconds(45)),
            ..Default::default()
        };
        assert_eq!(decide_run(&run, now, &t), RunAction::Warn);

        run.last_progress = Some(now - ChDuration::seconds(125));
        assert_eq!(decide_run(&run, now, &t), RunAction::Kill { pid: 42 });
    }

    #[test]
    fn stale_run_kill_uses_2x_budget_when_known() {
        // When wall_clock_budget_seconds is set, the kill threshold is 2x
        // the budget — not the static run_stale_kill backstop.
        let now = Utc::now();
        // budget = 60s => effective kill = 120s.
        let mut run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            wall_clock_budget_seconds: Some(60.0),
            last_progress: Some(now - ChDuration::seconds(115)),
            ..Default::default()
        };
        // 115s < 120s (2x budget) — not yet kill, may warn if past run_stale_warn.
        let action = decide_run(&run, now, &thresholds());
        assert_ne!(
            action,
            RunAction::Kill { pid: 42 },
            "115s should not kill with 2x budget=120s"
        );

        // 121s >= 120s (2x budget) — should kill.
        run.last_progress = Some(now - ChDuration::seconds(121));
        assert_eq!(
            decide_run(&run, now, &thresholds()),
            RunAction::Kill { pid: 42 },
            "121s should kill when 2x budget = 120s",
        );
    }

    #[test]
    fn stale_run_kill_falls_back_to_backstop_without_budget() {
        // No wall_clock_budget_seconds: the static run_stale_kill (600s default)
        // is the kill threshold.
        let now = Utc::now();
        let mut run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            wall_clock_budget_seconds: None,
            last_progress: Some(now - ChDuration::seconds(200)),
            ..Default::default()
        };
        // 200s < 600s backstop — not killed.
        assert_ne!(
            decide_run(&run, now, &thresholds()),
            RunAction::Kill { pid: 42 },
            "200s should not kill without budget (backstop is 600s)",
        );

        // 601s >= 600s backstop — killed.
        run.last_progress = Some(now - ChDuration::seconds(601));
        assert_eq!(
            decide_run(&run, now, &thresholds()),
            RunAction::Kill { pid: 42 },
            "601s should kill at the 600s backstop",
        );
    }

    #[test]
    fn run_without_pid_only_warns() {
        // Uses tight thresholds so the test exercises the warn-only path
        // within a normal age range.
        let t = tight_run_thresholds();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: None,
            last_progress: Some(now - ChDuration::seconds(200)),
            ..Default::default()
        };
        // 200s > tight kill (120s) but no pid, so only Warn.
        assert_eq!(decide_run(&run, now, &t), RunAction::Warn);
    }

    #[test]
    fn staleness_is_independent_of_deadline() {
        // A run with fresh progress but a blown deadline is NOT flagged by
        // the staleness check — that is the deadline trigger's job.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(999_999),
            last_progress: Some(now),
            deadline: Some(now - ChDuration::seconds(60)),
            ..Default::default()
        };
        assert_eq!(decide_run(&run, now, &thresholds()), RunAction::Nothing);
    }

    // ---- decide_run_deadline ---------------------------------------

    #[test]
    fn deadline_before_is_none() {
        let sleeper = Sleeper::spawn();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(sleeper.pid()),
            deadline: Some(now + ChDuration::seconds(30)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None
        );
    }

    #[test]
    fn deadline_just_past_is_sigterm() {
        // A real, alive, signalable worker pid (not our own, not 0/1).
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            deadline: Some(now - ChDuration::seconds(1)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::Sigterm { pid }
        );
    }

    #[test]
    fn deadline_past_grace_with_live_pid_is_sigkill() {
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            // 10s past deadline, grace is 5s -> escalate to SIGKILL.
            deadline: Some(now - ChDuration::seconds(10)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::Sigkill { pid }
        );
    }

    #[test]
    fn deadline_overrun_with_no_pid_is_none() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: None,
            deadline: Some(now - ChDuration::seconds(30)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None
        );
    }

    #[test]
    fn deadline_overrun_with_dead_pid_is_none() {
        // pid 0 is never alive; the deadline check declines to signal it.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(0),
            deadline: Some(now - ChDuration::seconds(30)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None
        );
    }

    #[test]
    fn deadline_overrun_never_signals_pid_zero_or_one() {
        let now = Utc::now();
        for bad in [0, 1, -1, i32::MIN] {
            let run = ActiveRun {
                run_id: "r1".into(),
                pid: Some(bad),
                deadline: Some(now - ChDuration::seconds(30)),
                ..Default::default()
            };
            assert_eq!(
                decide_run_deadline(
                    &run,
                    now,
                    Duration::from_secs(5),
                    Duration::from_secs(6 * 3600),
                    &no_protected()
                ),
                RunDeadlineAction::None,
                "must never signal pid {bad}",
            );
        }
    }

    #[test]
    fn deadline_overrun_never_signals_protected_pid() {
        // The orchestrator pid (heartbeat pid) is protected: even a blown
        // deadline must not target it. Use a real alive pid so the only
        // thing stopping the signal is the protected-set guard.
        let sleeper = Sleeper::spawn();
        let orchestrator = sleeper.pid();
        let now = Utc::now();
        let mut protected = HashSet::new();
        protected.insert(orchestrator);
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(orchestrator),
            deadline: Some(now - ChDuration::seconds(30)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &protected
            ),
            RunDeadlineAction::None
        );
    }

    #[test]
    fn deadline_overrun_with_recycled_pid_is_none() {
        // A live, signalable worker pid whose recorded start time does NOT
        // match the live process simulates pid reuse: the original worker
        // died and the kernel reissued its number to an unrelated process.
        // The deadline check must decline to signal it.
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let now = Utc::now();
        let real = signal::pid_start_time(pid);
        // Only meaningful when we can read a real start time (Linux).
        if let Some(real) = real {
            let run = ActiveRun {
                run_id: "r1".into(),
                pid: Some(pid),
                pid_start_time: Some(real + 999_999.0),
                deadline: Some(now - ChDuration::seconds(30)),
                ..Default::default()
            };
            assert_eq!(
                decide_run_deadline(
                    &run,
                    now,
                    Duration::from_secs(5),
                    Duration::from_secs(6 * 3600),
                    &no_protected()
                ),
                RunDeadlineAction::None,
                "must not signal a recycled pid (start-time mismatch)",
            );
        }
    }

    #[test]
    fn deadline_overrun_with_matching_start_time_signals() {
        // Counterpart: a live worker whose recorded start time matches the
        // live process IS the genuine worker — signal it on deadline.
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            deadline: Some(now - ChDuration::seconds(1)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::Sigterm { pid },
        );
    }

    #[test]
    fn deadline_overrun_never_signals_supervisor_own_pid() {
        // Even with an empty protected set, the supervisor's own pid is
        // refused by the safety guard.
        let now = Utc::now();
        let me = std::process::id() as i32;
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(me),
            deadline: Some(now - ChDuration::seconds(30)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None
        );
    }

    #[test]
    fn no_deadline_field_is_none() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(999_999),
            deadline: None,
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None
        );
    }

    // ---- effective_deadline (untrusted-deadline clamp) -------------

    #[test]
    fn effective_deadline_clamps_a_far_future_deadline() {
        // started_at = now - 10s, max_run = 60s → ceiling = now + 50s.
        // A written deadline a year out must be clamped to the ceiling.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            started_at: Some(now - ChDuration::seconds(10)),
            deadline: Some(now + ChDuration::days(365)),
            ..Default::default()
        };
        let eff = effective_deadline(&run, Duration::from_secs(60)).unwrap();
        let ceiling = (now - ChDuration::seconds(10)) + ChDuration::seconds(60);
        assert_eq!(eff, ceiling);
        assert!(eff < now + ChDuration::days(1), "must be clamped near now");
    }

    #[test]
    fn effective_deadline_keeps_a_within_ceiling_deadline() {
        // A reasonable deadline below the ceiling is returned unchanged.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            started_at: Some(now - ChDuration::seconds(10)),
            deadline: Some(now + ChDuration::seconds(20)),
            ..Default::default()
        };
        let eff = effective_deadline(&run, Duration::from_secs(600)).unwrap();
        assert_eq!(eff, now + ChDuration::seconds(20));
    }

    #[test]
    fn effective_deadline_without_started_at_uses_written() {
        // No anchor to clamp against → the written deadline stands.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            started_at: None,
            deadline: Some(now + ChDuration::days(365)),
            ..Default::default()
        };
        let eff = effective_deadline(&run, Duration::from_secs(60)).unwrap();
        assert_eq!(eff, now + ChDuration::days(365));
    }

    #[test]
    fn far_future_deadline_is_still_killed_via_the_clamp() {
        // The end-to-end intent: an orchestrator that writes a far-future
        // deadline cannot disable its own watchdog. started_at well in the
        // past + a small max_run_seconds puts the clamped cutoff behind now,
        // so a live worker IS signalled despite the year-out written deadline.
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            started_at: Some(now - ChDuration::seconds(120)),
            deadline: Some(now + ChDuration::days(365)), // untrusted, far future
            ..Default::default()
        };
        // max_run = 60s → ceiling = now - 60s. The clamped cutoff is 60s in
        // the past, well beyond the 5s grace, so the run is force-killed —
        // proving the untrusted far-future deadline did NOT disable the kill.
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(60),
                &no_protected()
            ),
            RunDeadlineAction::Sigkill { pid },
        );
    }

    #[test]
    fn within_ceiling_far_future_run_is_not_signalled() {
        // Counterpart: when the clamped cutoff is still in the future the run
        // is NOT killed — the clamp only bounds, it does not kill early.
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(999_999),
            started_at: Some(now), // just started
            deadline: Some(now + ChDuration::days(365)),
            ..Default::default()
        };
        // ceiling = now + 6h, still ahead of now → None.
        assert_eq!(
            decide_run_deadline(
                &run,
                now,
                Duration::from_secs(5),
                Duration::from_secs(6 * 3600),
                &no_protected()
            ),
            RunDeadlineAction::None,
        );
    }

    #[test]
    fn signalable_pid_guard() {
        let protected: HashSet<i32> = [4242].into_iter().collect();
        // Bad sentinels.
        assert!(!is_signalable_run_pid(0, &protected));
        assert!(!is_signalable_run_pid(1, &protected));
        assert!(!is_signalable_run_pid(-1, &protected));
        // Supervisor's own pid.
        assert!(!is_signalable_run_pid(
            std::process::id() as i32,
            &protected
        ));
        // Protected (orchestrator) pid.
        assert!(!is_signalable_run_pid(4242, &protected));
        // A plausible worker pid.
        assert!(is_signalable_run_pid(999_999, &protected));
    }

    // ---- resolve_kill_target (group-vs-leader selection) -----------

    #[test]
    fn resolve_target_falls_back_to_leader_without_pgid() {
        // A legacy record (no pgid) always resolves to a single-pid kill.
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(4242),
            pgid: None,
            ..Default::default()
        };
        assert_eq!(
            resolve_kill_target(&run, 4242, &no_protected()),
            KillTarget::Leader { pid: 4242 },
        );
    }

    #[test]
    fn resolve_target_group_kills_the_workers_own_group() {
        use std::os::unix::process::CommandExt;
        let sleeper = Sleeper(
            std::process::Command::new("sleep")
                .arg("60")
                .process_group(0)
                .spawn()
                .unwrap(),
        );
        let pid = sleeper.pid();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pgid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            ..Default::default()
        };
        assert_eq!(
            resolve_kill_target(&run, pid, &no_protected()),
            KillTarget::Group {
                pgid: pid,
                leader_pid: pid
            }
        );
    }

    #[test]
    fn resolve_target_refuses_a_foreign_group() {
        // A pgid that is NOT the vetted leader's own group (pgid != pid) is a
        // group we have not identity-matched; fall back to the single-pid
        // kill rather than negate a foreign group.
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(4242),
            pgid: Some(9999), // not the worker's own group
            ..Default::default()
        };
        assert_eq!(
            resolve_kill_target(&run, 4242, &no_protected()),
            KillTarget::Leader { pid: 4242 },
        );
    }

    #[test]
    fn resolve_target_refuses_init_and_sentinel_pgids() {
        // pgid <= 1 is never negatable, even when it equals the (degenerate)
        // leader pid — fall back to the leader kill (which its own pid guard
        // will then refuse downstream).
        for bad in [0, 1] {
            let run = ActiveRun {
                run_id: "r1".into(),
                pid: Some(bad),
                pgid: Some(bad),
                ..Default::default()
            };
            assert_eq!(
                resolve_kill_target(&run, bad, &no_protected()),
                KillTarget::Leader { pid: bad },
                "pgid {bad} must never be negated",
            );
        }
    }

    #[test]
    fn resolve_target_refuses_a_protected_group() {
        // Even the worker's own group is refused when that pgid is protected
        // (the supervisor's or orchestrator's group): fall back to the leader.
        let protected_pgids: HashSet<i32> = [4242].into_iter().collect();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(4242),
            pgid: Some(4242),
            ..Default::default()
        };
        assert_eq!(
            resolve_kill_target(&run, 4242, &protected_pgids),
            KillTarget::Leader { pid: 4242 },
        );
    }

    #[test]
    fn protected_pgids_always_contains_the_supervisors_own_group() {
        // The supervisor's own pgid is always fenced off; with no heartbeat
        // pid that is the only protected group.
        let set = protected_pgids(None);
        assert!(set.contains(&signal::own_pgid()));
    }

    #[test]
    fn protected_pgids_includes_the_orchestrator_group_when_resolvable() {
        // Given an alive pid (our own), its pgid is added to the protected
        // set alongside the supervisor's own group.
        let me = std::process::id() as i32;
        let set = protected_pgids(Some(me));
        assert!(set.contains(&signal::own_pgid()));
        if let Some(pgid) = signal::pgid_of(me) {
            assert!(set.contains(&pgid));
        }
    }

    // ---- decide_run_kill_request -----------------------------------

    #[test]
    fn kill_request_signals_a_live_worker_pid() {
        // A real, alive, signalable worker pid is the one returned.
        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            ..Default::default()
        };
        assert_eq!(decide_run_kill_request(&run, &no_protected()), Some(pid));
    }

    #[test]
    fn kill_request_with_no_pid_is_none() {
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: None,
            ..Default::default()
        };
        assert_eq!(decide_run_kill_request(&run, &no_protected()), None);
    }

    #[test]
    fn kill_request_never_signals_protected_or_unsafe_pid() {
        // The orchestrator (protected) pid is refused even on explicit
        // request — the supervisor kills run workers only.
        let sleeper = Sleeper::spawn();
        let orchestrator = sleeper.pid();
        let mut protected = HashSet::new();
        protected.insert(orchestrator);
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(orchestrator),
            ..Default::default()
        };
        assert_eq!(decide_run_kill_request(&run, &protected), None);

        // pid 0/1 and the supervisor's own pid are refused too.
        for bad in [0, 1, std::process::id() as i32] {
            let run = ActiveRun {
                run_id: "r1".into(),
                pid: Some(bad),
                ..Default::default()
            };
            assert_eq!(
                decide_run_kill_request(&run, &no_protected()),
                None,
                "must never signal pid {bad} on request",
            );
        }
    }

    #[test]
    fn kill_request_with_dead_pid_is_none() {
        // pid 0 is never alive; an already-dead worker has nothing to kill.
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(0),
            ..Default::default()
        };
        assert_eq!(decide_run_kill_request(&run, &no_protected()), None);
    }

    // ---- SeqLiveness (seq-advance liveness) ------------------------

    /// A heartbeat carrying a `seq` that never advances goes stale on the
    /// SEQ-CHANGE age even though every observation refreshes the timestamp
    /// to `now`. This is the wedged-loop case the seq cursor exists to catch.
    #[test]
    fn frozen_seq_goes_stale_despite_fresh_timestamps() {
        let t = thresholds(); // warn 30s, deep-stale 90s
        let mut tracker = SeqLiveness::new();
        let start = Utc::now();

        // First observation: seq=5, fresh. Anchors the change at `start`.
        let hb = Heartbeat {
            pid: Some(1),
            last_heartbeat: Some(start),
            seq: Some(5),
            ..Default::default()
        };
        let obs = tracker.observe(Some(&hb), start, &t);
        assert_eq!(obs.action, HeartbeatAction::Nothing);
        assert_eq!(obs.seq_age_seconds, Some(0));

        // 45s later, seq STILL 5, timestamp refreshed to the new now.
        let now = start + ChDuration::seconds(45);
        let hb = Heartbeat {
            pid: Some(1),
            last_heartbeat: Some(now), // fresh timestamp
            seq: Some(5),              // but seq frozen
            ..Default::default()
        };
        let obs = tracker.observe(Some(&hb), now, &t);
        // Timestamp age is ~0, yet seq age is 45s → Warn.
        assert_eq!(obs.action, HeartbeatAction::Warn);
        assert_eq!(obs.seq_age_seconds, Some(45));
        assert_eq!(obs.timestamp_age_seconds, Some(0));

        // 100s later still frozen → deep-stale (Stale), but never a kill.
        let now = start + ChDuration::seconds(100);
        let hb = Heartbeat {
            pid: Some(1),
            last_heartbeat: Some(now),
            seq: Some(5),
            ..Default::default()
        };
        let obs = tracker.observe(Some(&hb), now, &t);
        assert_eq!(obs.action, HeartbeatAction::Stale);
        assert_eq!(obs.seq_age_seconds, Some(100));
    }

    /// When seq advances, the change anchor resets and the seq age drops
    /// back to zero — the loop is making progress.
    #[test]
    fn advancing_seq_resets_the_age() {
        let t = thresholds();
        let mut tracker = SeqLiveness::new();
        let start = Utc::now();

        let hb = Heartbeat {
            seq: Some(1),
            last_heartbeat: Some(start),
            ..Default::default()
        };
        tracker.observe(Some(&hb), start, &t);

        // 45s later seq advanced to 2 → age resets to 0, no warning.
        let now = start + ChDuration::seconds(45);
        let hb = Heartbeat {
            seq: Some(2),
            last_heartbeat: Some(now),
            ..Default::default()
        };
        let obs = tracker.observe(Some(&hb), now, &t);
        assert_eq!(obs.action, HeartbeatAction::Nothing);
        assert_eq!(obs.seq_age_seconds, Some(0));
        assert_eq!(tracker.last_seq(), Some(2));
    }

    /// Back-compat: a heartbeat with NO seq (old writer) falls back to the
    /// timestamp age for staleness, exactly as before Phase 4.
    #[test]
    fn absent_seq_falls_back_to_timestamp_age() {
        let t = thresholds();
        let mut tracker = SeqLiveness::new();
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(1),
            last_heartbeat: Some(now - ChDuration::seconds(45)),
            seq: None, // legacy heartbeat
            ..Default::default()
        };
        let obs = tracker.observe(Some(&hb), now, &t);
        assert_eq!(obs.action, HeartbeatAction::Warn); // from the 45s timestamp
        assert_eq!(obs.seq_age_seconds, None);
        assert_eq!(obs.timestamp_age_seconds, Some(45));
    }

    /// The seq path, like every heartbeat path, NEVER kills — sweep a wide
    /// range of frozen-seq ages and assert the outcome stays warn-only.
    #[test]
    fn seq_liveness_never_kills() {
        let t = thresholds();
        for age in [0i64, 30, 90, 600, 86_400, 1_000_000] {
            let mut tracker = SeqLiveness::new();
            let start = Utc::now();
            let hb0 = Heartbeat {
                seq: Some(9),
                last_heartbeat: Some(start),
                ..Default::default()
            };
            tracker.observe(Some(&hb0), start, &t);
            let now = start + ChDuration::seconds(age);
            let hb = Heartbeat {
                seq: Some(9),
                last_heartbeat: Some(now),
                ..Default::default()
            };
            let obs = tracker.observe(Some(&hb), now, &t);
            assert!(
                matches!(
                    obs.action,
                    HeartbeatAction::Nothing
                        | HeartbeatAction::Warn
                        | HeartbeatAction::Stale
                        | HeartbeatAction::MissingHeartbeat
                ),
                "seq liveness must never kill: age={age}s yielded {:?}",
                obs.action
            );
        }
    }

    /// The read-only `snapshot` reports ages without advancing the anchor,
    /// so repeated calls are stable and the watchdog loop keeps sole
    /// ownership of advancement.
    #[test]
    fn snapshot_is_read_only() {
        let t = thresholds();
        let mut tracker = SeqLiveness::new();
        let start = Utc::now();
        let hb0 = Heartbeat {
            seq: Some(3),
            last_heartbeat: Some(start),
            ..Default::default()
        };
        tracker.observe(Some(&hb0), start, &t);

        let now = start + ChDuration::seconds(40);
        let hb = Heartbeat {
            seq: Some(3),
            last_heartbeat: Some(now),
            ..Default::default()
        };
        // Two snapshots in a row must not move the anchor.
        let a = tracker.snapshot(Some(&hb), now, &t);
        let b = tracker.snapshot(Some(&hb), now, &t);
        assert_eq!(a.seq_age_seconds, Some(40));
        assert_eq!(b.seq_age_seconds, Some(40));
        // The tracker still holds the original change time.
        assert_eq!(tracker.last_seq_change_at(), Some(start));
    }

    // ---- Orphan enforcement through the running watchdog ----

    /// Build a `ztw-snap-*` ephemeral-snapshot tree under the SYSTEM temp dir
    /// (what the reap path's prefix guard checks against) and return
    /// `(snapshot_root, recorded_working_copy_path)`.
    fn make_ephemeral_snapshot() -> (tempfile::TempDir, String) {
        let parent = tempfile::Builder::new()
            .prefix(reap::SNAPSHOT_PREFIX)
            .tempdir()
            .unwrap();
        let working_copy = parent.path().join("snapshot");
        std::fs::create_dir_all(working_copy.join("src")).unwrap();
        std::fs::write(working_copy.join("src/a.py"), b"x = 1\n").unwrap();
        let recorded = working_copy.to_str().unwrap().to_string();
        (parent, recorded)
    }

    #[tokio::test]
    async fn dead_orchestrator_reap_group_kills_reaps_snapshot_and_finalizes_state() {
        // End-to-end: a confirmed-dead orchestrator triggers the full reap —
        // group-kill the live worker, GC its ztw-snap-* snapshot, remove the
        // state file.
        let tmp = tempfile::TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        let paths = WorkspacePaths::new(ws);

        let mut sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let (snap_guard, snapshot_path) = make_ephemeral_snapshot();
        let snap_root = snap_guard.path().to_path_buf();

        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            snapshot_path: Some(snapshot_path),
            ..Default::default()
        };
        // Write the state file so finalization has something to remove.
        let run_file = paths.active_runs_dir().join("r1.json");
        std::fs::write(&run_file, serde_json::to_vec(&run).unwrap()).unwrap();

        let log = Arc::new(WatchdogLog::new());
        await_orphan_cleanup(
            &paths,
            &Thresholds {
                run_kill_grace: Duration::from_millis(200),
                ..Thresholds::default()
            },
            &log,
            None,
        )
        .await;

        // Worker killed. Reap the zombie first: a killed-but-unwaited child
        // still answers kill(pid, 0) (the real watchdog never parents these
        // workers, so this is a test artifact only).
        sleeper.reap();
        assert!(!signal::is_alive(pid), "the orphaned worker must be killed",);
        // Snapshot tree GC'd.
        assert!(!snap_root.exists(), "the ztw-snap-* root must be reaped");
        // State file finalized.
        assert!(!run_file.exists(), "the run state file must be removed");
        // An action was recorded under the reap trigger.
        let recorded = log.snapshot();
        assert_eq!(recorded.len(), 1);
        assert_eq!(recorded[0].trigger, Trigger::OrchestratorReap);
        // The TempDir guard's directory is already gone; defuse its drop.
        std::mem::forget(snap_guard);
    }

    #[tokio::test]
    async fn dead_orchestrator_reap_refuses_a_snapshot_outside_the_temp_dir() {
        // The prefix guard protects against a malformed/hostile snapshot_path:
        // a ztw-snap-* directory NOT under the system temp dir is left intact,
        // while the worker is still killed and the state file finalized.
        let tmp = tempfile::TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        let paths = WorkspacePaths::new(ws);

        // A ztw-snap-* tree genuinely OUTSIDE the system temp dir (the
        // workspace TempDir lives UNDER the system temp dir, so anchor this
        // under the current working directory instead).
        let outside = tempfile::TempDir::new_in(std::env::current_dir().unwrap()).unwrap();
        let outside_root = outside.path().join("ztw-snap-evil");
        std::fs::create_dir_all(outside_root.join("snapshot")).unwrap();
        let bogus = outside_root.join("snapshot").to_str().unwrap().to_string();

        let mut sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            snapshot_path: Some(bogus),
            ..Default::default()
        };
        let run_file = paths.active_runs_dir().join("r1.json");
        std::fs::write(&run_file, serde_json::to_vec(&run).unwrap()).unwrap();

        let log = Arc::new(WatchdogLog::new());
        await_orphan_cleanup(
            &paths,
            &Thresholds {
                run_kill_grace: Duration::from_millis(200),
                ..Thresholds::default()
            },
            &log,
            None,
        )
        .await;

        sleeper.reap();
        assert!(!signal::is_alive(pid), "the worker is still killed");
        assert!(!run_file.exists(), "the state file is still finalized");
        // The guard refused the out-of-temp tree: it must remain intact.
        assert!(
            outside_root.exists(),
            "a ztw-snap-* tree outside the temp dir must NOT be removed",
        );
    }

    #[tokio::test]
    async fn dead_orchestrator_reap_tolerates_an_absent_worker_and_no_snapshot() {
        // A run whose worker is already gone and that recorded no snapshot:
        // the reap is a clean no-op on the kill + GC, but STILL finalizes the
        // state file (the dead orchestrator can no longer do it).
        let tmp = tempfile::TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        let paths = WorkspacePaths::new(ws);

        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(0), // never alive
            snapshot_path: None,
            ..Default::default()
        };
        let run_file = paths.active_runs_dir().join("r1.json");
        std::fs::write(&run_file, serde_json::to_vec(&run).unwrap()).unwrap();

        let log = Arc::new(WatchdogLog::new());
        await_orphan_cleanup(&paths, &thresholds(), &log, None).await;

        assert!(
            !run_file.exists(),
            "state finalized even with no worker/snapshot"
        );
        // No escalation was recorded (no live, signalable pid).
        assert!(log.is_empty());
    }

    #[tokio::test]
    async fn reap_records_the_escalation_into_the_ledger_when_configured() {
        // A live, signalable worker orphaned by a dead orchestrator: the reap
        // escalation must be mirrored into the tamper-evident ledger (when one
        // is configured) in addition to the in-memory ring.
        use crate::ledger::AuditLedger;
        let tmp = tempfile::TempDir::new().unwrap();
        let ws = tmp.path().join("ws");
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        let paths = WorkspacePaths::new(ws);

        let sleeper = Sleeper::spawn();
        let pid = sleeper.pid();
        let run = ActiveRun {
            run_id: "r-reap".into(),
            pid: Some(pid),
            pid_start_time: signal::pid_start_time(pid),
            ..Default::default()
        };
        let run_file = paths.active_runs_dir().join("r-reap.json");
        std::fs::write(&run_file, serde_json::to_vec(&run).unwrap()).unwrap();

        let log = Arc::new(WatchdogLog::new());
        let ledger = Arc::new(AuditLedger::open(&tmp.path().join("super-runtime")));
        await_orphan_cleanup(
            &paths,
            &Thresholds {
                run_kill_grace: Duration::from_millis(200),
                ..Thresholds::default()
            },
            &log,
            Some(&ledger),
        )
        .await;
        // Reap the zombie so the assertion below is about state, not liveness.
        drop(sleeper);

        // The ring recorded one OrchestratorReap action...
        let snap = log.snapshot();
        assert_eq!(snap.len(), 1);
        assert_eq!(snap[0].trigger, Trigger::OrchestratorReap);
        // ...and the ledger recorded it too, with an intact chain.
        let report = ledger.verify();
        assert!(report.intact, "ledger chain must verify: {report:?}");
        assert_eq!(report.records, 1);
    }
}
