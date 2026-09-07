//! POSIX signal helpers.
//!
//! Two-stage escalation: SIGTERM, wait grace, SIGKILL if the process is
//! still alive. All operations are best-effort — a vanished pid is not
//! an error.

use nix::sys::signal::{kill, killpg, Signal};
use nix::unistd::Pid;
use std::time::Duration;
use tracing::debug;

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
    /// SIGKILL was sent and target termination was confirmed.
    KilledForcefully,
    /// Ownership, signal delivery, or final termination could not be confirmed.
    Failed,
}

pub fn is_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    if let Some(fields) = process_fields(pid) {
        if matches!(fields.first().map(String::as_str), Some("Z" | "X")) {
            return false;
        }
    }
    // Signal 0 = existence check.
    match kill(Pid::from_raw(pid), None) {
        Ok(_) => true,
        Err(nix::errno::Errno::ESRCH) => false,
        Err(nix::errno::Errno::EPERM) => true, // exists, just not ours to signal
        Err(_) => true,
    }
}

fn process_fields(pid: i32) -> Option<Vec<String>> {
    if pid <= 0 {
        return None;
    }
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rparen = raw.rfind(')')?;
    Some(
        raw[rparen + 1..]
            .split_whitespace()
            .map(String::from)
            .collect(),
    )
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
    process_fields(pid)?
        .get(19)?
        .parse::<u64>()
        .ok()
        .map(|value| value as f64)
}

/// A signal requires a recorded, readable start token matching a live process.
pub fn verified_process(pid: i32, expected_start_time: Option<f64>) -> bool {
    expected_start_time.is_some() && pid_start_time(pid) == expected_start_time && is_alive(pid)
}

/// Unknown membership remains live; zombies cannot execute or retain a checkout.
pub fn group_has_live_members(pgid: i32) -> bool {
    if pgid <= 1 || pgid == own_pgid() {
        return true;
    }
    let Ok(processes) = std::fs::read_dir("/proc") else {
        return killpg(Pid::from_raw(pgid), None) != Err(nix::errno::Errno::ESRCH);
    };
    for process in processes {
        let Ok(process) = process else {
            return true;
        };
        let Some(pid) = process
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<i32>().ok())
        else {
            continue;
        };
        if let Some(fields) = process_fields(pid) {
            if fields.get(2).and_then(|value| value.parse::<i32>().ok()) == Some(pgid)
                && !matches!(fields.first().map(String::as_str), Some("Z" | "X"))
            {
                return true;
            }
        } else if is_alive(pid) {
            return true;
        }
    }
    false
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
    process_fields(pid)?
        .get(2)?
        .parse::<i32>()
        .ok()
        .filter(|pgid| *pgid > 0)
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

/// Send SIGTERM to a process group whose ownership the caller already verified.
/// The caller must apply both `is_negatable_pgid` and `verified_target`; this
/// primitive does not recheck either guard.
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

/// An owned leader or its process group. Group termination includes descendants
/// after leader exit; a group id remains allocated while its members survive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KillTarget {
    /// Signal a single pid (legacy record with no pgid, or a pgid the
    /// negate-guard refused).
    Leader { pid: i32 },
    /// Signal the owned group, including members surviving `leader_pid`.
    Group { pgid: i32, leader_pid: i32 },
}

/// A recorded group remains owned after leader exit while descendants survive.
/// Unreadable identity of a possibly live leader never authorizes signalling.
pub fn verified_target(target: KillTarget, expected_start_time: Option<f64>) -> bool {
    let pid = target.leader_pid();
    target_identity_matches(
        target,
        expected_start_time,
        pid_start_time(pid),
        is_alive(pid),
        pgid_of(pid),
    )
}

fn target_identity_matches(
    target: KillTarget,
    expected: Option<f64>,
    observed: Option<f64>,
    leader_alive: bool,
    observed_group: Option<i32>,
) -> bool {
    let Some(expected) = expected.filter(|value| value.is_finite() && *value >= 0.0) else {
        return false;
    };
    if observed.is_some_and(|value| value != expected) {
        return false;
    }
    match target {
        KillTarget::Leader { pid } => pid > 1 && leader_alive && observed == Some(expected),
        KillTarget::Group { pgid, leader_pid } => {
            pgid > 1
                && pgid == leader_pid
                && pgid != own_pgid()
                && match observed {
                    Some(_) => observed_group == Some(pgid),
                    None => !leader_alive && observed_group.is_none(),
                }
        }
    }
}

impl KillTarget {
    /// The leader whose start token establishes the target's ownership.
    pub fn leader_pid(self) -> i32 {
        match self {
            KillTarget::Leader { pid } => pid,
            KillTarget::Group { leader_pid, .. } => leader_pid,
        }
    }

    pub fn is_gone(self) -> bool {
        match self {
            KillTarget::Leader { pid } => !is_alive(pid),
            KillTarget::Group { pgid, .. } => !group_has_live_members(pgid),
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

/// Capture an owned target's identity before starting its escalation.
pub async fn escalate_target(target: KillTarget, grace: Duration) -> EscalationOutcome {
    escalate_owned_target(target, pid_start_time(target.leader_pid()), grace).await
}

/// Terminate an identity-verified target and confirm its process group stopped.
///
/// The start token is retained across waits. After leader exit, surviving group
/// members retain the group id. A replacement leader invalidates that ownership.
pub async fn escalate_owned_target(
    target: KillTarget,
    expected_start_time: Option<f64>,
    grace: Duration,
) -> EscalationOutcome {
    if target.is_gone() {
        return EscalationOutcome::AlreadyGone;
    }
    if !verified_target(target, expected_start_time) {
        return EscalationOutcome::Failed;
    }
    if target.send_sigterm().is_err() {
        return EscalationOutcome::Failed;
    }
    let deadline = tokio::time::Instant::now() + grace;
    while tokio::time::Instant::now() < deadline {
        if target.is_gone() {
            return EscalationOutcome::TerminatedGracefully;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    if target.is_gone() {
        return EscalationOutcome::TerminatedGracefully;
    }
    if !verified_target(target, expected_start_time) {
        return EscalationOutcome::Failed;
    }
    if target.send_sigkill().is_err() {
        return EscalationOutcome::Failed;
    }
    let deadline = tokio::time::Instant::now() + grace.max(Duration::from_millis(100));
    while tokio::time::Instant::now() < deadline {
        if target.is_gone() {
            return EscalationOutcome::KilledForcefully;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    EscalationOutcome::Failed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unreadable_live_identity_and_replacement_leaders_refuse_signals() {
        let group = KillTarget::Group {
            pgid: 999_999,
            leader_pid: 999_999,
        };
        for target in [group, KillTarget::Leader { pid: 999_999 }] {
            assert!(!target_identity_matches(
                target,
                Some(12.0),
                None,
                true,
                None
            ));
            assert!(!target_identity_matches(
                target,
                Some(12.0),
                Some(13.0),
                true,
                Some(999_999)
            ));
            assert!(!target_identity_matches(target, None, None, false, None));
        }
        assert!(target_identity_matches(
            group,
            Some(12.0),
            None,
            false,
            None
        ));
        assert!(!target_identity_matches(
            group,
            Some(12.0),
            None,
            true,
            Some(999_999)
        ));
        assert!(!target_identity_matches(
            group,
            Some(12.0),
            Some(12.0),
            true,
            None
        ));
    }

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
        // An absent leader and an empty group need no signals.
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
    async fn escalation_finishes_descendants_when_leader_exited_before_or_during_grace() {
        for leader_exited in [false, true] {
            let group = crate::test_process_group::OwnedGroup::spawn(leader_exited);
            let out = escalate_owned_target(
                KillTarget::Group {
                    pgid: group.leader,
                    leader_pid: group.leader,
                },
                Some(group.start_time),
                Duration::from_millis(50),
            )
            .await;
            assert_eq!(
                out,
                EscalationOutcome::KilledForcefully,
                "leader exited before escalation: {leader_exited}"
            );
            assert!(!is_alive(group.descendant), "resistant descendant survived");
            assert!(!is_alive(group.leader));
        }
    }
}
