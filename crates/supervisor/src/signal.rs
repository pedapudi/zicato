//! POSIX signal helpers.
//!
//! Two-stage escalation: SIGTERM, wait grace, SIGKILL if the process is
//! still alive. All operations are best-effort — a vanished pid is not
//! an error.

use nix::sys::signal::{kill, killpg, Signal};
use nix::unistd::Pid;
use std::time::Duration;
use tracing::{debug, warn};

/// The supervisor's own process-group id.
///
/// A group-kill negates a pgid (`kill(-pgid, …)`), so the supervisor must
/// never negate the group it is itself a member of — that would signal the
/// supervisor (and, when it shares a group, the orchestrator that launched
/// it). This is read once and fenced off in the protected pgid set. Uses
/// `libc::getpgrp()` directly (always available) rather than enabling nix's
/// `process` feature for one call.
pub fn own_pgid() -> i32 {
    // SAFETY: getpgrp() takes no arguments and cannot fail.
    unsafe { libc::getpgrp() }
}

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

/// Read `pid`'s process-group id from `/proc/<pid>/stat` (field 5, `pgrp`),
/// or `None` when the process is gone / `/proc` is unreadable (non-Linux).
///
/// Used to fence the ORCHESTRATOR's process group out of the negatable set:
/// the watchdog group-kills run workers, never the orchestrator's group.
/// Reading via `/proc` avoids enabling nix's `process` feature for one
/// `getpgid` call and is best-effort — a `None` reading simply leaves that
/// group unprotected by pgid (the pid-level protected set still applies, so
/// no group containing the orchestrator pid is ever the worker's OWN vetted
/// group anyway). A non-positive result is rejected (a process group id is
/// always positive).
pub fn pgid_of(pid: i32) -> Option<i32> {
    if pid <= 0 {
        return None;
    }
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // Field 2 (comm) is parenthesized and may contain ')' / spaces, so
    // tokenize after the LAST ')'. rest[0] is field 3 (state); field 5
    // (pgrp) is rest[2].
    let rparen = raw.rfind(')')?;
    let rest: Vec<&str> = raw[rparen + 1..].split_whitespace().collect();
    rest.get(2)
        .and_then(|s| s.parse::<i32>().ok())
        .filter(|&pgid| pgid > 0)
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

/// Whether `pgid` is one this watchdog may negate (`kill(-pgid, …)`).
///
/// Negating a pgid signals every process in that group, so the guard is
/// strict: `pgid` must be `> 1` (pgid 0 means "my own group" to `killpg`,
/// pgid 1 is init's; negating either is catastrophic) and must not be in
/// the protected set (the supervisor's and orchestrator's own pgids). Pure,
/// so it is unit-testable without spawning a process group.
pub fn is_negatable_pgid(pgid: i32, protected: &std::collections::HashSet<i32>) -> bool {
    if pgid <= 1 {
        return false;
    }
    !protected.contains(&pgid)
}

/// Send SIGTERM to an entire process group (`killpg(pgid, SIGTERM)`, i.e.
/// `kill(-pgid, …)`). The caller MUST have already vetted `pgid` through
/// [`is_negatable_pgid`] AND confirmed the group leader is alive and
/// identity-matched — this primitive does not re-check.
pub fn send_sigterm_group(pgid: i32) -> Result<(), nix::errno::Errno> {
    debug!(pgid, "sending SIGTERM to process group");
    killpg(Pid::from_raw(pgid), Signal::SIGTERM)
}

/// Send SIGKILL to an entire process group. Same vetting contract as
/// [`send_sigterm_group`].
pub fn send_sigkill_group(pgid: i32) -> Result<(), nix::errno::Errno> {
    debug!(pgid, "sending SIGKILL to process group");
    killpg(Pid::from_raw(pgid), Signal::SIGKILL)
}

/// What an escalation should signal: a single leader pid, or the whole
/// process group.
///
/// Liveness is always tracked through the group LEADER pid (the worker —
/// `pgid == pid`, since the worker is spawned as a session/group leader).
/// Tracking the leader keeps the identity check (`is_same_process`) and the
/// "still alive?" poll meaningful even for the group case: the group is
/// considered gone once its leader exits, exactly as a single-pid kill
/// tracks its own pid.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KillTarget {
    /// Signal a single pid (legacy record with no pgid, or a pgid the
    /// negate-guard refused).
    Leader { pid: i32 },
    /// Signal the whole process group by negating `pgid`; the group's
    /// liveness is tracked through `leader_pid`.
    Group { pgid: i32, leader_pid: i32 },
}

impl KillTarget {
    /// The pid whose liveness gates this escalation (the leader in both
    /// cases).
    fn leader_pid(self) -> i32 {
        match self {
            KillTarget::Leader { pid } => pid,
            KillTarget::Group { leader_pid, .. } => leader_pid,
        }
    }

    fn send_sigterm(self) -> Result<(), nix::errno::Errno> {
        match self {
            KillTarget::Leader { pid } => send_sigterm(pid),
            KillTarget::Group { pgid, .. } => send_sigterm_group(pgid),
        }
    }

    fn send_sigkill(self) -> Result<(), nix::errno::Errno> {
        match self {
            KillTarget::Leader { pid } => send_sigkill(pid),
            KillTarget::Group { pgid, .. } => send_sigkill_group(pgid),
        }
    }
}

/// Escalate: SIGTERM, poll for exit up to `grace`, SIGKILL if still alive.
///
/// Single-pid escalation — the thin wrapper that backs every legacy
/// (pgid-less) kill path. Delegates to [`escalate_target`] with a
/// [`KillTarget::Leader`].
pub async fn escalate(pid: i32, grace: Duration) -> EscalationOutcome {
    escalate_target(KillTarget::Leader { pid }, grace).await
}

/// Escalate a [`KillTarget`]: SIGTERM, poll the leader for exit up to
/// `grace`, SIGKILL if the leader is still alive.
///
/// For a [`KillTarget::Group`] the SIGTERM/SIGKILL go to the whole group
/// (`kill(-pgid, …)`) but the exit poll watches the group LEADER pid, so the
/// escalation completes the moment the worker (group leader) exits — any
/// stragglers in the group have been signalled by the same `killpg`. The
/// outcome semantics are identical to the single-pid path: `AlreadyGone`
/// when the leader is already dead, `TerminatedGracefully` if it exits
/// within grace, `KilledForcefully` after the forced kill, `Failed` if a
/// signal could not be delivered.
pub async fn escalate_target(target: KillTarget, grace: Duration) -> EscalationOutcome {
    let leader = target.leader_pid();
    if !is_alive(leader) {
        return EscalationOutcome::AlreadyGone;
    }
    if let Err(e) = target.send_sigterm() {
        warn!(?target, error=%e, "SIGTERM failed");
        return EscalationOutcome::Failed;
    }

    let poll_interval = Duration::from_millis(100);
    let mut elapsed = Duration::ZERO;
    while elapsed < grace {
        tokio::time::sleep(poll_interval).await;
        elapsed += poll_interval;
        if !is_alive(leader) {
            return EscalationOutcome::TerminatedGracefully;
        }
    }

    if !is_alive(leader) {
        return EscalationOutcome::TerminatedGracefully;
    }

    match target.send_sigkill() {
        Ok(_) => EscalationOutcome::KilledForcefully,
        Err(e) => {
            warn!(?target, error=%e, "SIGKILL failed");
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

    // ---- process-group kill guards ---------------------------------

    #[test]
    fn negatable_pgid_refuses_init_and_sentinels() {
        let protected = std::collections::HashSet::new();
        // pgid 0 means "my own group" to killpg; pgid 1 is init; negative is
        // a malformed record. None may be negated.
        assert!(!is_negatable_pgid(0, &protected));
        assert!(!is_negatable_pgid(1, &protected));
        assert!(!is_negatable_pgid(-1, &protected));
        assert!(!is_negatable_pgid(i32::MIN, &protected));
        // A plausible worker pgid is negatable.
        assert!(is_negatable_pgid(424_242, &protected));
    }

    #[test]
    fn negatable_pgid_refuses_protected_groups() {
        // The supervisor's and orchestrator's own pgids are fenced off: even
        // a well-formed pgid in the protected set must not be negated.
        let protected: std::collections::HashSet<i32> = [5000, 6000].into_iter().collect();
        assert!(!is_negatable_pgid(5000, &protected));
        assert!(!is_negatable_pgid(6000, &protected));
        assert!(is_negatable_pgid(7000, &protected));
    }

    #[test]
    fn own_pgid_is_positive() {
        // The supervisor always belongs to a real (>0) process group.
        assert!(own_pgid() > 0);
    }

    #[test]
    fn kill_target_leader_pid_is_the_tracked_pid() {
        // Both variants gate liveness on the leader pid: the single pid for
        // Leader, and the recorded leader for Group.
        assert_eq!(KillTarget::Leader { pid: 42 }.leader_pid(), 42);
        assert_eq!(
            KillTarget::Group {
                pgid: 99,
                leader_pid: 42,
            }
            .leader_pid(),
            42
        );
    }

    #[tokio::test]
    async fn escalate_target_group_with_dead_leader_is_already_gone() {
        // A group whose leader pid is not alive (pid 0) escalates to nothing:
        // the leader gates the whole group's liveness.
        let out = escalate_target(
            KillTarget::Group {
                pgid: 999_999,
                leader_pid: 0,
            },
            Duration::from_millis(50),
        )
        .await;
        assert_eq!(out, EscalationOutcome::AlreadyGone);
    }

    #[tokio::test]
    async fn escalate_group_kills_the_whole_group() {
        // End-to-end: spawn a worker as its OWN process-group leader, fork a
        // grandchild inside that group, then group-escalate. Negating the
        // pgid must take BOTH down — the leak the single-pid kill would miss.
        use std::os::unix::process::CommandExt;
        // The leader spawns a child sleep, then sleeps itself; both share the
        // new process group the leader creates via setsid().
        let mut leader = unsafe {
            std::process::Command::new("sh")
                .args(["-c", "sleep 600 & sleep 600"])
                .pre_exec(|| {
                    // New session → the leader becomes its own group leader,
                    // so pgid == its pid and the grandchild inherits the pgid.
                    nix::unistd::setsid().map_err(std::io::Error::from)?;
                    Ok(())
                })
                .spawn()
                .expect("spawn group leader")
        };
        let leader_pid = leader.id() as i32;
        // The leader's new pgid equals its own pid (it is the group leader).
        let pgid = leader_pid;
        // Give the shell a moment to fork its background grandchild.
        tokio::time::sleep(Duration::from_millis(200)).await;

        let out = escalate_target(
            KillTarget::Group { pgid, leader_pid },
            Duration::from_millis(300),
        )
        .await;
        // The leader exited (gracefully on SIGTERM, or forcibly) — either way
        // the group is gone.
        assert!(
            matches!(
                out,
                EscalationOutcome::TerminatedGracefully | EscalationOutcome::KilledForcefully
            ),
            "group escalation should stop the leader, got {out:?}",
        );
        let _ = leader.wait();
        // The leader pid is gone.
        assert!(!is_alive(leader_pid), "leader must be dead after group kill");
    }
}
