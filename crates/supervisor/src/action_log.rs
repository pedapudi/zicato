//! In-memory ring buffer of recent watchdog actions.
//!
//! The watchdog escalates (SIGTERM -> SIGKILL) against the orchestrator
//! and against over-deadline / stalled run workers, but until now it only
//! emitted those decisions to the tracing log — there was no structured,
//! queryable record. `/statusz` needs to surface "what has the watchdog
//! actually done", so this module keeps a small bounded history of recent
//! escalations entirely in memory.
//!
//! It is deliberately tiny: a fixed-capacity ring behind a `Mutex`, shared
//! by `Arc`. Nothing is persisted — a supervisor restart starts the
//! history empty, which is the honest thing to show (the watchdog cannot
//! claim actions it did not take in this process lifetime).

use chrono::{DateTime, Utc};
use serde::Serialize;
use std::collections::VecDeque;
use std::sync::Mutex;

/// How many recent actions to retain. Small: this is an operational
/// "recent activity" view, not an audit log.
pub const CAPACITY: usize = 64;

/// Which watchdog trigger fired.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Trigger {
    /// The orchestrator heartbeat went stale past the kill threshold.
    HeartbeatStale,
    /// A run blew its per-board wall-clock deadline.
    RunDeadline,
    /// A run stopped making progress (staleness trigger).
    RunStale,
    /// The Python parent requested a kill via a control marker (the
    /// supervisor is the single SIGTERM→grace→SIGKILL escalator).
    KillRequest,
    /// The orchestrator was confirmed dead and the supervisor reaped an
    /// orphaned worker (group-kill + ephemeral-snapshot GC + state
    /// finalization) in its stead.
    OrchestratorReap,
}

impl Trigger {
    pub fn as_str(self) -> &'static str {
        match self {
            Trigger::HeartbeatStale => "heartbeat_stale",
            Trigger::RunDeadline => "run_deadline",
            Trigger::RunStale => "run_stale",
            Trigger::KillRequest => "kill_request",
            Trigger::OrchestratorReap => "orchestrator_reap",
        }
    }
}

/// How far the escalation got. Mirrors `signal::EscalationOutcome` but is
/// a serializable, dependency-free copy so the action log stays decoupled.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// Pid was already gone; nothing was sent.
    AlreadyGone,
    /// SIGTERM was honoured within the grace window.
    TerminatedGracefully,
    /// SIGTERM was ignored; SIGKILL was sent.
    KilledForcefully,
    /// The signal could not be delivered.
    Failed,
}

impl From<crate::signal::EscalationOutcome> for Outcome {
    fn from(o: crate::signal::EscalationOutcome) -> Self {
        use crate::signal::EscalationOutcome as E;
        match o {
            E::AlreadyGone => Outcome::AlreadyGone,
            E::TerminatedGracefully => Outcome::TerminatedGracefully,
            E::KilledForcefully => Outcome::KilledForcefully,
            E::Failed => Outcome::Failed,
        }
    }
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::AlreadyGone => "already_gone",
            Outcome::TerminatedGracefully => "terminated_gracefully",
            Outcome::KilledForcefully => "killed_forcefully",
            Outcome::Failed => "failed",
        }
    }
}

/// One recorded watchdog escalation.
#[derive(Debug, Clone, Serialize)]
pub struct Action {
    /// When the escalation finished.
    pub ts: DateTime<Utc>,
    /// Which trigger fired.
    pub trigger: Trigger,
    /// The pid the watchdog signalled.
    pub pid: i32,
    /// The run id, when the trigger was run-scoped (`None` for heartbeat).
    pub run_id: Option<String>,
    /// How far the escalation got.
    pub outcome: Outcome,
}

/// A bounded, thread-safe ring buffer of recent watchdog actions.
#[derive(Debug, Default)]
pub struct WatchdogLog {
    inner: Mutex<VecDeque<Action>>,
}

impl WatchdogLog {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(VecDeque::with_capacity(CAPACITY)),
        }
    }

    /// Append an action, evicting the oldest once `CAPACITY` is reached.
    pub fn record(&self, action: Action) {
        let mut q = match self.inner.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        if q.len() == CAPACITY {
            q.pop_front();
        }
        q.push_back(action);
    }

    /// Snapshot of recorded actions, newest last.
    pub fn snapshot(&self) -> Vec<Action> {
        match self.inner.lock() {
            Ok(g) => g.iter().cloned().collect(),
            Err(p) => p.into_inner().iter().cloned().collect(),
        }
    }

    /// Number of actions currently retained.
    pub fn len(&self) -> usize {
        match self.inner.lock() {
            Ok(g) => g.len(),
            Err(p) => p.into_inner().len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn action(pid: i32) -> Action {
        Action {
            ts: Utc::now(),
            trigger: Trigger::RunDeadline,
            pid,
            run_id: Some(format!("run-{pid}")),
            outcome: Outcome::KilledForcefully,
        }
    }

    #[test]
    fn records_and_snapshots_in_order() {
        let log = WatchdogLog::new();
        assert!(log.is_empty());
        log.record(action(1));
        log.record(action(2));
        let snap = log.snapshot();
        assert_eq!(snap.len(), 2);
        assert_eq!(snap[0].pid, 1);
        assert_eq!(snap[1].pid, 2);
    }

    #[test]
    fn evicts_oldest_past_capacity() {
        let log = WatchdogLog::new();
        for i in 0..(CAPACITY as i32 + 10) {
            log.record(action(i));
        }
        let snap = log.snapshot();
        assert_eq!(snap.len(), CAPACITY);
        // Oldest 10 were evicted; the buffer now starts at pid 10.
        assert_eq!(snap.first().unwrap().pid, 10);
        assert_eq!(snap.last().unwrap().pid, CAPACITY as i32 + 9);
    }

    #[test]
    fn escalation_outcome_maps_cleanly() {
        use crate::signal::EscalationOutcome as E;
        assert_eq!(Outcome::from(E::AlreadyGone), Outcome::AlreadyGone);
        assert_eq!(
            Outcome::from(E::TerminatedGracefully),
            Outcome::TerminatedGracefully
        );
        assert_eq!(
            Outcome::from(E::KilledForcefully),
            Outcome::KilledForcefully
        );
        assert_eq!(Outcome::from(E::Failed), Outcome::Failed);
    }
}
