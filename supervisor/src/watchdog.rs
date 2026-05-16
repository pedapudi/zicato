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
    pub run_stale_warn: Duration,
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
            run_stale_warn: Duration::from_secs(30),
            run_stale_kill: Duration::from_secs(120),
            grace: Duration::from_secs(5),
            run_kill_grace: Duration::from_secs(5),
            run_deadline_kill_disabled: false,
        }
    }
}

/// What the heartbeat watchdog wants to do this tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeartbeatAction {
    Nothing,
    Warn,
    Kill { pid: i32 },
    MissingHeartbeat,
}

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

    if age_secs >= t.heartbeat_stale_kill.as_secs() {
        if let Some(pid) = hb.pid {
            return HeartbeatAction::Kill { pid };
        }
        return HeartbeatAction::Warn;
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
pub fn decide_run(run: &crate::state::ActiveRun, now: DateTime<Utc>, t: &Thresholds) -> RunAction {
    let reference = run.last_progress.or(run.started_at);
    let Some(reference) = reference else {
        return RunAction::Nothing;
    };
    let age_secs = now.signed_duration_since(reference).num_seconds().max(0) as u64;

    if age_secs >= t.run_stale_kill.as_secs() {
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
    // Sanity-check the worker is actually alive before deciding to signal.
    if !signal::is_alive(pid) {
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
pub async fn heartbeat_loop(
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
                let hb = reader::read_heartbeat(&paths);
                let action = decide_heartbeat(hb.as_ref(), Utc::now(), &thresholds);
                match action {
                    HeartbeatAction::Nothing => {}
                    HeartbeatAction::Warn => {
                        warn!(?hb, "heartbeat is stale (warn threshold)");
                    }
                    HeartbeatAction::MissingHeartbeat => {
                        // Don't spam: just debug-level after the initial warn.
                        tracing::debug!("no heartbeat file present");
                    }
                    HeartbeatAction::Kill { pid } => {
                        warn!(pid, "heartbeat stale past kill threshold; escalating");
                        let out = escalate(pid, thresholds.grace).await;
                        info!(?out, pid, "escalation complete");
                        log.record(Action {
                            ts: Utc::now(),
                            trigger: Trigger::HeartbeatStale,
                            pid,
                            run_id: None,
                            outcome: out.into(),
                        });
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
        Thresholds {
            heartbeat_stale_warn: Duration::from_secs(30),
            heartbeat_stale_kill: Duration::from_secs(90),
            run_stale_warn: Duration::from_secs(30),
            run_stale_kill: Duration::from_secs(120),
            grace: Duration::from_secs(5),
            run_kill_grace: Duration::from_secs(5),
            run_deadline_kill_disabled: false,
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
    fn kill_threshold_for_heartbeat() {
        let now = Utc::now();
        let hb = Heartbeat {
            pid: Some(999),
            last_heartbeat: Some(now - ChDuration::seconds(100)),
            ..Default::default()
        };
        assert_eq!(
            decide_heartbeat(Some(&hb), now, &thresholds()),
            HeartbeatAction::Kill { pid: 999 }
        );
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
        let now = Utc::now();
        let mut run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            last_progress: Some(now - ChDuration::seconds(45)),
            ..Default::default()
        };
        assert_eq!(decide_run(&run, now, &thresholds()), RunAction::Warn);

        run.last_progress = Some(now - ChDuration::seconds(125));
        assert_eq!(
            decide_run(&run, now, &thresholds()),
            RunAction::Kill { pid: 42 }
        );
    }

    #[test]
    fn run_without_pid_only_warns() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: None,
            last_progress: Some(now - ChDuration::seconds(200)),
            ..Default::default()
        };
        assert_eq!(decide_run(&run, now, &thresholds()), RunAction::Warn);
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
