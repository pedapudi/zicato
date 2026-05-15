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

    #[tokio::test]
    async fn vanished_pid_yields_already_gone() {
        // pid 0 is reserved; cannot be alive.
        let out = escalate(0, Duration::from_millis(50)).await;
        assert_eq!(out, EscalationOutcome::AlreadyGone);
    }
}
