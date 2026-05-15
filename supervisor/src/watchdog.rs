//! Watchdog tasks: heartbeat staleness + run staleness/deadline checks.
//!
//! Each tick reads state files (cheap, small files) and decides whether
//! to escalate. The decisions are pure functions of `(state, now,
//! thresholds)` and are unit-tested below; the async wrapper just plumbs
//! them into `tokio::time::interval`.

use crate::reader::{self, WorkspacePaths};
use crate::signal::escalate;
use chrono::{DateTime, Utc};
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
    /// Grace between SIGTERM and SIGKILL.
    pub grace: Duration,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            heartbeat_stale_warn: Duration::from_secs(30),
            heartbeat_stale_kill: Duration::from_secs(90),
            run_stale_warn: Duration::from_secs(30),
            run_stale_kill: Duration::from_secs(120),
            grace: Duration::from_secs(5),
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunAction {
    Nothing,
    Warn,
    Kill { pid: i32 },
}

pub fn decide_run(run: &crate::state::ActiveRun, now: DateTime<Utc>, t: &Thresholds) -> RunAction {
    // Deadline overrun first.
    if let Some(deadline) = run.deadline {
        if now > deadline {
            if let Some(pid) = run.pid {
                return RunAction::Kill { pid };
            }
            return RunAction::Warn;
        }
    }

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

/// Long-running heartbeat watchdog task.
pub async fn heartbeat_loop(
    paths: WorkspacePaths,
    thresholds: Thresholds,
    interval: Duration,
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
                    }
                }
            }
            _ = shutdown_rx.recv() => break,
        }
    }
}

/// Long-running active-runs watchdog task.
pub async fn runs_loop(
    paths: WorkspacePaths,
    thresholds: Thresholds,
    interval: Duration,
    shutdown: Sender<()>,
) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut shutdown_rx = shutdown.subscribe();
    loop {
        tokio::select! {
            _ = ticker.tick() => {
                let runs = reader::read_active_runs(&paths);
                for run in &runs {
                    let action = decide_run(run, Utc::now(), &thresholds);
                    match action {
                        RunAction::Nothing => {}
                        RunAction::Warn => {
                            warn!(run_id=%run.run_id, "active run is stalled (warn)");
                        }
                        RunAction::Kill { pid } => {
                            warn!(run_id=%run.run_id, pid, "active run past kill threshold; escalating");
                            let out = escalate(pid, thresholds.grace).await;
                            info!(?out, run_id=%run.run_id, "run escalation complete");
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
    fn deadline_overrun_kills_immediately() {
        let now = Utc::now();
        let run = ActiveRun {
            run_id: "r1".into(),
            pid: Some(42),
            last_progress: Some(now), // fresh progress
            deadline: Some(now - ChDuration::seconds(1)),
            ..Default::default()
        };
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
}
