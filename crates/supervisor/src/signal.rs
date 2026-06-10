//! POSIX signal helpers.
//!
//! Two-stage escalation: SIGTERM, wait grace, SIGKILL if the process is
//! still alive. All operations are best-effort — a vanished pid is not
//! an error.

use nix::sys::signal::{kill, Signal};
use nix::unistd::Pid;
use std::time::Duration;
use tracing::{debug, warn};

/// Result of one escalation cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EscalationOutcome {
    /// Pid was not alive at the start; nothing sent.
    AlreadyGone,
    /// SIGTERM was sent and the process exited within grace.
    TerminatedGracefully,
    /// SIGTERM did not stop it; SIGKILL was sent.
    KilledForcefully,
    /// SIGTERM not deliverable (permission, ESRCH, ...).
    Failed,
}

pub fn is_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    // Signal 0 = existence check.
    match kill(Pid::from_raw(pid), None) {
        Ok(_) => true,
        Err(nix::errno::Errno::ESRCH) => false,
        Err(nix::errno::Errno::EPERM) => true, // exists, just not ours to signal
        Err(_) => false,
    }
}

/// Read `pid`'s start time (an opaque identity token), or `None`.
///
/// A pid number alone is not a process identity: after the owner exits the
/// kernel can reissue the same number to an unrelated process (pid reuse).
/// Pairing the pid with its **start time** distinguishes the original
/// process from a recycled-pid impostor. The value is only ever compared
/// for equality against another reading on the same host; its units
/// (Linux: clock ticks since boot, from `/proc/<pid>/stat` field 22) carry
/// no portable meaning.
///
/// Returns `None` for a non-positive pid, a process that is gone, or an
/// unparseable/absent `/proc` entry (e.g. a non-Linux host). The
/// supervisor is Linux-only in practice; a `None` reading is handled
/// conservatively by [`is_same_process`].
///
/// The value is an `f64` (rather than the natural `u64`) so it compares
/// directly against the Python writer's reading, which serializes the
/// `/proc` tick count as a JSON float (`116371304.0`). The tick count is
/// integer-valued and well within `f64`'s exact-integer range, so equality
/// comparison is exact.
pub fn pid_start_time(pid: i32) -> Option<f64> {
    if pid <= 0 {
        return None;
    }
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // Field 2 (comm) is wrapped in parens and may itself contain ')' and
    // spaces, so tokenize everything after the LAST ')'.
    let rparen = raw.rfind(')')?;
    let rest: Vec<&str> = raw[rparen + 1..].split_whitespace().collect();
    // rest[0] is field 3 (state); field 22 (starttime) is rest[19].
    rest.get(19)
        .and_then(|s| s.parse::<u64>().ok().map(|t| t as f64))
}

/// Whether `pid` is alive **and** is the same process that recorded
/// `expected_start_time` — the pid-reuse-proof identity check.
///
/// * not alive → `false` (a dead process is never "the same")
/// * alive, `expected_start_time == None` → fall back to bare liveness
///   (`true`): no recorded identity to check against, so stay conservative
/// * alive, current start time unreadable → `true`: cannot *disprove*
///   identity, so don't manufacture a mismatch
/// * alive, both known → `true` iff equal
pub fn is_same_process(pid: i32, expected_start_time: Option<f64>) -> bool {
    if !is_alive(pid) {
        return false;
    }
    let Some(expected) = expected_start_time else {
        return true;
    };
    match pid_start_time(pid) {
        // Both readings are integer-valued tick counts, so exact float
        // equality is the right comparison (no tolerance needed).
        Some(current) => current == expected,
        None => true,
    }
}

pub fn send_sigterm(pid: i32) -> Result<(), nix::errno::Errno> {
    debug!(pid, "sending SIGTERM");
    kill(Pid::from_raw(pid), Signal::SIGTERM)
}

pub fn send_sigkill(pid: i32) -> Result<(), nix::errno::Errno> {
    debug!(pid, "sending SIGKILL");
    kill(Pid::from_raw(pid), Signal::SIGKILL)
}

/// Escalate: SIGTERM, poll for exit up to `grace`, SIGKILL if still alive.
pub async fn escalate(pid: i32, grace: Duration) -> EscalationOutcome {
    if !is_alive(pid) {
        return EscalationOutcome::AlreadyGone;
    }
    if let Err(e) = send_sigterm(pid) {
        warn!(pid, error=%e, "SIGTERM failed");
        return EscalationOutcome::Failed;
    }

    let poll_interval = Duration::from_millis(100);
    let mut elapsed = Duration::ZERO;
    while elapsed < grace {
        tokio::time::sleep(poll_interval).await;
        elapsed += poll_interval;
        if !is_alive(pid) {
            return EscalationOutcome::TerminatedGracefully;
        }
    }

    if !is_alive(pid) {
        return EscalationOutcome::TerminatedGracefully;
    }

    match send_sigkill(pid) {
        Ok(_) => EscalationOutcome::KilledForcefully,
        Err(e) => {
            warn!(pid, error=%e, "SIGKILL failed");
            EscalationOutcome::Failed
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_pid_is_not_alive() {
        assert!(!is_alive(0));
        assert!(!is_alive(-1));
    }

    #[test]
    fn self_pid_is_alive() {
        // We are running, so our own pid must show as alive.
        let me = std::process::id() as i32;
        assert!(is_alive(me));
    }

    #[test]
    fn start_time_for_self_is_stable() {
        let me = std::process::id() as i32;
        // On Linux (/proc) this is always readable.
        let st = pid_start_time(me);
        if let Some(v) = st {
            assert_eq!(pid_start_time(me), Some(v), "start time must be stable");
        }
    }

    #[test]
    fn start_time_none_for_dead_and_bad_pids() {
        assert_eq!(pid_start_time(0), None);
        assert_eq!(pid_start_time(-1), None);
        assert_eq!(pid_start_time(99_999_999), None);
    }

    #[test]
    fn is_same_process_true_for_self_matching_start_time() {
        let me = std::process::id() as i32;
        let st = pid_start_time(me);
        assert!(is_same_process(me, st));
    }

    #[test]
    fn is_same_process_false_on_start_time_mismatch() {
        // Same live pid (ours) with a non-matching start time simulates pid
        // reuse: the recorded owner is gone and we now hold the number. The
        // identity check must reject it as a different process.
        let me = std::process::id() as i32;
        if let Some(real) = pid_start_time(me) {
            assert!(!is_same_process(me, Some(real + 999_999.0)));
        }
    }

    #[test]
    fn is_same_process_false_for_dead_pid() {
        assert!(!is_same_process(99_999_999, Some(12345.0)));
        assert!(!is_same_process(0, Some(12345.0)));
    }

    #[test]
    fn is_same_process_falls_back_to_liveness_without_recorded_time() {
        let me = std::process::id() as i32;
        assert!(is_same_process(me, None));
        assert!(!is_same_process(99_999_999, None));
    }

    #[tokio::test]
    async fn vanished_pid_yields_already_gone() {
        // pid 0 is reserved; cannot be alive.
        let out = escalate(0, Duration::from_millis(50)).await;
        assert_eq!(out, EscalationOutcome::AlreadyGone);
    }
}
