//! Watchdog tasks: heartbeat staleness + run staleness/deadline checks.
//!
//! Each tick reads state files (cheap, small files) and decides whether
//! to escalate. The decisions are pure functions of `(state, now,
//! thresholds)` and are unit-tested below; the async wrapper just plumbs
//! them into `tokio::time::interval`.
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
use crate::reader::{self, WorkspacePaths};
use crate::signal::{self, escalate};
use chrono::{DateTime, Utc};
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast::Sender;
use tracing::{info, warn};

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

/// Decide whether an active run has blown its per-board wall-clock budget.
///
/// Pure function of `(active_run, now, grace)` so it is unit-testable
/// independent of the tokio loop, mirroring [`decide_heartbeat`] /
/// [`decide_run`].
///
/// * before `deadline` → [`RunDeadlineAction::None`]
/// * past `deadline` (within `grace`) → [`RunDeadlineAction::Sigterm`]
/// * past `deadline + grace`, worker still alive → [`RunDeadlineAction::Sigkill`]
///
/// The grace window is measured from the deadline itself: once `deadline`
/// passes the worker is asked to stop, and `grace` later — if it has not
/// honoured SIGTERM — it is force-killed. A worker that exits during the
/// grace window is no longer alive, so the result collapses back to
/// `None`. Pid safety is enforced via [`is_signalable_run_pid`]; an unsafe
/// or absent pid yields `None`.
pub fn decide_run_deadline(
    run: &crate::state::ActiveRun,
    now: DateTime<Utc>,
    grace: Duration,
    protected: &HashSet<i32>,
) -> RunDeadlineAction {
    let Some(deadline) = run.deadline else {
        return RunDeadlineAction::None;
    };
    if now <= deadline {
        return RunDeadlineAction::None;
    }
    let Some(pid) = run.pid else {
        // Past deadline but no pid to signal — nothing the watchdog can do.
        return RunDeadlineAction::None;
    };
    if !is_signalable_run_pid(pid, protected) {
        return RunDeadlineAction::None;
    }
    // Sanity-check the worker is actually alive AND is the same process we
    // recorded — never signal a recycled pid. When the worker recorded its
    // start time we verify it; absent a recorded start time this degrades
    // to a bare liveness check (legacy writers).
    if !signal::is_same_process(pid, run.pid_start_time) {
        return RunDeadlineAction::None;
    }

    let overrun = now.signed_duration_since(deadline);
    let grace = chrono::Duration::from_std(grace).unwrap_or_else(|_| chrono::Duration::zero());
    if overrun > grace {
        RunDeadlineAction::Sigkill { pid }
    } else {
        RunDeadlineAction::Sigterm { pid }
    }
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
    shutdown: Sender<()>,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut shutdown_rx = shutdown.subscribe();
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                let hb = reader::read_heartbeat(&paths);
                let action = decide_heartbeat(hb.as_ref(), Utc::now(), &thresholds);
                match action {
                    HeartbeatAction::Nothing => {}
                    HeartbeatAction::Warn => {
                        warn!(?hb, "heartbeat is stale (warn threshold)");
                    }
                    HeartbeatAction::Stale => {
                        // Deep-stale past the former kill threshold. We do
                        // NOT signal the orchestrator: restart is an
                        // operator / process-supervisor decision. Surface
                        // it loudly and move on.
                        warn!(
                            ?hb,
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

/// Long-running active-runs watchdog task.
///
/// Each tick evaluates two independent triggers per `active_runs/*.json`:
///
/// 1. **Deadline overrun** ([`decide_run_deadline`]) — default-on; sends
///    SIGTERM, then SIGKILL after `--run-kill-grace`. The watchdog never
///    deletes the state file: the orchestrator/worker owns that lifecycle
///    (the Python parent detects the dead worker, cleans up, and records
///    the run aborted).
/// 2. **Run staleness** ([`decide_run`]) — `last_progress` not advancing.
pub async fn runs_loop(
    paths: WorkspacePaths,
    thresholds: Thresholds,
    interval: Duration,
    log: Arc<WatchdogLog>,
    shutdown: Sender<()>,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut shutdown_rx = shutdown.subscribe();
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                // Protect the orchestrator pid (carried by the heartbeat)
                // from ever being treated as a run worker. The watchdog
                // kills run pids only.
                let mut protected: HashSet<i32> = HashSet::new();
                if let Some(hb) = reader::read_heartbeat(&paths) {
                    if let Some(pid) = hb.pid {
                        protected.insert(pid);
                    }
                }

                let runs = reader::read_active_runs(&paths);
                for run in &runs {
                    let now = Utc::now();

                    // Trigger 1: per-board wall-clock deadline (default-on).
                    if !thresholds.run_deadline_kill_disabled {
                        match decide_run_deadline(
                            run,
                            now,
                            thresholds.run_kill_grace,
                            &protected,
                        ) {
                            RunDeadlineAction::None => {}
                            RunDeadlineAction::Sigterm { pid }
                            | RunDeadlineAction::Sigkill { pid } => {
                                let budget = run
                                    .wall_clock_budget_seconds
                                    .map(|b| format!("{b:.0}"))
                                    .unwrap_or_else(|| "?".to_string());
                                warn!(
                                    run_id = %run.run_id,
                                    pid,
                                    budget_seconds = %budget,
                                    "run {} exceeded its {}s wall-clock budget; SIGTERM",
                                    run.run_id,
                                    budget,
                                );
                                // escalate() does SIGTERM, waits the grace
                                // window, then SIGKILLs if still alive.
                                let out = escalate(pid, thresholds.run_kill_grace).await;
                                if out == crate::signal::EscalationOutcome::KilledForcefully {
                                    warn!(
                                        run_id = %run.run_id,
                                        pid,
                                        "run ignored SIGTERM after {}s grace; SIGKILL",
                                        thresholds.run_kill_grace.as_secs(),
                                    );
                                }
                                info!(
                                    ?out,
                                    run_id = %run.run_id,
                                    "run deadline escalation complete; \
                                     leaving state file for orchestrator cleanup",
                                );
                                log.record(Action {
                                    ts: Utc::now(),
                                    trigger: Trigger::RunDeadline,
                                    pid,
                                    run_id: Some(run.run_id.clone()),
                                    outcome: out.into(),
                                });
                                // Deliberately do NOT remove the state
                                // file: the orchestrator/worker owns that
                                // lifecycle.
                                continue;
                            }
                        }
                    }

                    // Trigger 2: run staleness (complementary).
                    match decide_run(run, now, &thresholds) {
                        RunAction::Nothing => {}
                        RunAction::Warn => {
                            warn!(run_id=%run.run_id, "active run is stalled (warn)");
                        }
                        RunAction::Kill { pid } => {
                            if !is_signalable_run_pid(pid, &protected) {
                                warn!(
                                    run_id=%run.run_id,
                                    pid,
                                    "stalled run pid is not a signalable worker; skipping",
                                );
                                continue;
                            }
                            warn!(run_id=%run.run_id, pid, "active run past kill threshold; escalating");
                            let out = escalate(pid, thresholds.grace).await;
                            info!(?out, run_id=%run.run_id, "run escalation complete");
                            log.record(Action {
                                ts: Utc::now(),
                                trigger: Trigger::RunStale,
                                pid,
                                run_id: Some(run.run_id.clone()),
                                outcome: out.into(),
                            });
                            // Remove the state file so we don't re-escalate.
                            let run_file = paths.active_runs_dir().join(format!("{}.json", run.run_id));
                            if let Err(e) = std::fs::remove_file(&run_file) {
                                if e.kind() != std::io::ErrorKind::NotFound {
                                    warn!(?run_file, error=%e, "failed to remove stale run file");
                                }
                            }
                        }
                    }
                }
            }
            _ = shutdown_rx.recv() => break,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{ActiveRun, Heartbeat};
    use chrono::Duration as ChDuration;

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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            deadline: Some(now - ChDuration::seconds(1)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            // 10s past deadline, grace is 5s -> escalate to SIGKILL.
            deadline: Some(now - ChDuration::seconds(10)),
            ..Default::default()
        };
        assert_eq!(
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
                decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &protected),
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
                decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
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
            decide_run_deadline(&run, now, Duration::from_secs(5), &no_protected()),
            RunDeadlineAction::None
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
}
