"""Workspace lock — pid-based with stale-pid detection.

Only one orchestrator may write under ``.zicato/runtime/`` at a time;
running two against the same workspace would corrupt the lineage and
the in-flight tournament state. The lock file at
``.zicato/runtime/lock.json`` records the owning pid, instance id, and
acquisition timestamp.

The lock protocol is intentionally PID-based rather than OS-level
(``fcntl.flock`` and friends) because:

* The supervisor binary is a separate process and may be a different
  language; a JSON file works without negotiating a protocol.
* PID-based locks survive non-clean orchestrator exits in a recoverable
  way — the next invocation sees the stale pid, confirms it is gone
  via ``os.kill(pid, 0)``, and steals.

Re-acquisition by the same pid is idempotent (returns a fresh
:class:`WorkspaceLock` describing the existing lock). Different-pid
acquisitions raise :class:`WorkspaceLockHeld` unless the prior owner
is dead AND ``steal_stale=True`` (the default).
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.runtime._storage import backend_for, lock_key
from zicato.runtime.paths import ensure_runtime_dirs
from zicato.util.iso_time import now_iso as _utc_now_iso


class WorkspaceLockHeld(RuntimeError):
    """Raised when the workspace is locked by a live, different process."""


@dataclass(frozen=True, slots=True)
class WorkspaceLock:
    """Handle to an acquired workspace lock.

    Carries everything :func:`release_workspace_lock` needs to confirm
    it is releasing its own lock (and not stomping on a successor that
    stole it after a crash).

    Fields
    ------
    pid:
        OS process id that owns the lock.
    instance_id:
        Logical instance id stamped at acquisition time. Mirrors
        :class:`zicato.core.types.RuntimeConfig.instance_id`.
    acquired_at:
        ISO-8601 UTC timestamp of acquisition.
    workspace_root:
        Workspace the lock applies to.
    start_time:
        The owning process's start time (see :func:`pid_start_time`),
        paired with ``pid`` to defeat pid reuse: a recycled pid cannot pass
        as the original owner. ``None`` for a lock that carries no start
        time, or when the host could not read one; callers degrade
        gracefully via :func:`is_same_process`.
    """

    pid: int
    instance_id: str
    acquired_at: str
    workspace_root: Path
    start_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON encoding."""
        return {
            "pid": self.pid,
            "instance_id": self.instance_id,
            "acquired_at": self.acquired_at,
            "workspace_root": str(self.workspace_root),
            "start_time": self.start_time,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkspaceLock:
        """Construct from a JSON-decoded dict."""
        raw_start = d.get("start_time")
        return cls(
            pid=int(d["pid"]),
            instance_id=str(d["instance_id"]),
            acquired_at=str(d["acquired_at"]),
            workspace_root=Path(d["workspace_root"]),
            start_time=float(raw_start) if raw_start is not None else None,
        )


def is_pid_alive(pid: int) -> bool:
    """Return ``True`` iff ``pid`` is a live process on this host.

    Implementation is the classic POSIX ``os.kill(pid, 0)`` trick:
    signal 0 does not actually send a signal but performs the usual
    permission and existence checks. Three cases:

    * No error → process exists and we have permission to signal it.
      Treat as alive.
    * :class:`PermissionError` (``EPERM``) → process exists, we lack
      permission. Treat as alive (the conservative answer — refuse to
      steal the lock).
    * :class:`ProcessLookupError` (``ESRCH``) → no such pid. Dead.

    Pid 0 and negative pids are treated as not alive — the OS may
    interpret them as broadcast targets on some platforms and we don't
    want to accidentally signal anything else.

    .. note::
        This is a *bare* liveness check. It cannot tell apart the
        original process from a **recycled pid** (the kernel reissued the
        same pid number to an unrelated process after the owner exited).
        For lock ownership use :func:`is_same_process`, which also
        verifies the process *start time* — the identity check that
        defends against pid reuse.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # On Windows, the errno landscape is messier. ESRCH from a
        # non-existent pid is the standard answer on POSIX; treat any
        # other OSError as "alive" to stay on the conservative side.
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def pid_start_time(pid: int) -> float | None:
    """Return ``pid``'s start time as a float, or ``None`` if unavailable.

    The start time, paired with the pid, is what distinguishes the
    *original* process from a later one that the kernel reissued the same
    pid number to (pid reuse / recycling). A pid number alone is not an
    identity; ``(pid, start_time)`` is.

    The value is an opaque comparison token, **not** a wall-clock
    timestamp: its units differ per source. It is only ever compared for
    equality against another reading taken on the *same host* by the same
    code path, so absolute meaning is irrelevant — only stability is.

    Sources, in order of preference:

    1. **Linux ``/proc/<pid>/stat`` field 22** (``starttime``, clock ticks
       since boot). This is the portable-on-Linux primary and needs no
       third-party dependency. The 2nd field (``comm``) can contain spaces
       and parentheses, so we split on the *last* ``)`` before tokenizing.
    2. **``psutil.Process(pid).create_time()``** when psutil is installed
       (covers macOS / other platforms). Imported lazily so the core
       package keeps no hard psutil dependency.

    Returns ``None`` when the pid is non-positive, the process is gone, or
    no source could read a start time (e.g. a platform with neither
    ``/proc`` nor psutil). A ``None`` reading means "cannot prove
    identity" and callers treat it conservatively — see
    :func:`is_same_process`.
    """
    if pid <= 0:
        return None
    # Source 1: Linux /proc/<pid>/stat field 22 (starttime).
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        raw = ""
    if raw:
        # comm (field 2) is wrapped in parens and may itself contain ')'
        # and spaces; everything after the LAST ')' is space-separated.
        rparen = raw.rfind(")")
        if rparen != -1:
            rest = raw[rparen + 1 :].split()
            # rest[0] is field 3 (state); field 22 (starttime) is rest[19].
            if len(rest) >= 20:
                try:
                    return float(rest[19])
                except ValueError:
                    pass
    # Source 2: psutil fallback (non-Linux hosts).
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def is_same_process(pid: int, expected_start_time: float | None) -> bool:
    """Return ``True`` iff ``pid`` is alive **and** is the same process.

    "Same process" means the live process now holding ``pid`` is the one
    that recorded ``expected_start_time`` — defeating pid reuse, where a
    dead worker's pid is reissued to an unrelated process (which would
    make the dead worker look alive and decline a steal) or a stale lock's
    pid number now belongs to an innocent bystander (which would make us
    refuse to start, or mis-target it).

    Decision matrix:

    * Pid not alive → ``False`` (a dead process is never "the same").
    * Pid alive, ``expected_start_time`` is ``None`` → fall back to bare
      liveness (``True``). We have no recorded identity to check against
      (a lock carrying no start-time token, or a release where the writer
      could not read its own start time), so the conservative answer stands
      rather than inventing a mismatch.
    * Pid alive, current start time unreadable (``None``) → ``True``. We
      cannot *disprove* identity on this host/platform, so we stay
      conservative (do not declare a mismatch that would let us steal).
    * Pid alive, both start times known → ``True`` iff they match.
    """
    if not is_pid_alive(pid):
        return False
    if expected_start_time is None:
        return True
    current = pid_start_time(pid)
    if current is None:
        return True
    return current == expected_start_time


def read_workspace_lock(workspace_root: Path) -> WorkspaceLock | None:
    """Return the workspace lock IF a live process still holds it.

    A pure read — it never writes, steals, or clears anything. ``None`` means
    no lock file, an unreadable one, or one whose recorded owner is gone
    (a stale lock left by a crashed evolve, which
    :func:`acquire_workspace_lock` would steal).

    Lets a non-orchestrator process ask "is an evolve running here?" before
    doing work the single-writer rule reserves for the lock holder — the
    dashboard's index build defers on exactly this signal (see
    ``docs/design/ANALYTICAL-INDEX.md`` §5.3).
    """
    backend = backend_for(workspace_root)
    try:
        existing = backend.read_json(lock_key())
    except OSError:
        return None
    if existing is None:
        return None
    prior = WorkspaceLock.from_dict(existing)
    if not is_same_process(prior.pid, prior.start_time):
        return None
    return prior


def acquire_workspace_lock(
    workspace_root: Path,
    instance_id: str,
    *,
    steal_stale: bool = True,
) -> WorkspaceLock:
    """Acquire the workspace lock for the current process.

    Behavior matrix (assume my pid = ``self``):

    * No lock file → write a fresh lock, return :class:`WorkspaceLock`.
    * Lock file with pid == ``self`` → idempotent re-acquisition. The
      existing acquisition timestamp is preserved (so the caller can
      log "first acquired at..." across retries).
    * Lock file with pid != ``self`` and that pid is alive → raise
      :class:`WorkspaceLockHeld`.
    * Lock file with pid != ``self`` and that pid is dead → if
      ``steal_stale`` is true, overwrite with a fresh lock and return.
      If false, raise :class:`WorkspaceLockHeld`.

    Parameters
    ----------
    workspace_root:
        The workspace to lock.
    instance_id:
        Stamped on the lock for audit purposes.
    steal_stale:
        Whether to steal a lock whose owner is dead. Default ``True``;
        operators running a conservative deployment can set
        it to ``False`` to require manual cleanup of stale locks.
    """
    ensure_runtime_dirs(workspace_root)
    backend = backend_for(workspace_root)
    my_pid = os.getpid()

    existing = backend.read_json(lock_key())
    if existing is not None:
        prior = WorkspaceLock.from_dict(existing)
        if prior.pid == my_pid and is_same_process(prior.pid, prior.start_time):
            # Idempotent re-acquisition by the *same* process: keep the
            # original acquired_at so observers can see when the lock first
            # appeared. The start-time check guards the pathological case
            # where this process's pid equals a prior owner's pid that has
            # since been recycled to us — without it we'd inherit a foreign
            # lock as if it were our own re-acquisition.
            return prior
        # Different pid, OR same pid number but a different process
        # (recycled). Is the recorded owner still the same live process?
        if is_same_process(prior.pid, prior.start_time):
            raise WorkspaceLockHeld(
                f"workspace {workspace_root} locked by live pid {prior.pid} "
                f"(instance {prior.instance_id!r}, acquired {prior.acquired_at})"
            )
        if not steal_stale:
            raise WorkspaceLockHeld(
                f"workspace {workspace_root} locked by stale pid {prior.pid} "
                f"(instance {prior.instance_id!r}, acquired {prior.acquired_at}); "
                "refusing to steal with steal_stale=False"
            )
        # Fall through to overwrite: the prior owner is gone (or its pid
        # was reused by an unrelated process — the start-time mismatch
        # proves it is not the lock owner, so stealing is correct).

    lock = WorkspaceLock(
        pid=my_pid,
        instance_id=instance_id,
        acquired_at=_utc_now_iso(),
        workspace_root=workspace_root,
        start_time=pid_start_time(my_pid),
    )
    backend.write_json(lock_key(), lock.to_dict())
    return lock


def release_workspace_lock(lock: WorkspaceLock) -> None:
    """Release a previously-acquired workspace lock.

    Reads the on-disk lock; if it still belongs to the same pid +
    instance_id this caller acquired with, deletes it. Otherwise the
    call is a no-op — the lock has been stolen by another process or
    has already been released, and overwriting it would corrupt the
    successor's state.

    Idempotent.
    """
    backend = backend_for(lock.workspace_root)
    existing = backend.read_json(lock_key())
    if existing is None:
        return
    prior = WorkspaceLock.from_dict(existing)
    if prior.pid == lock.pid and prior.instance_id == lock.instance_id:
        backend.delete(lock_key())


__all__ = [
    "WorkspaceLockHeld",
    "WorkspaceLock",
    "read_workspace_lock",
    "is_pid_alive",
    "pid_start_time",
    "is_same_process",
    "acquire_workspace_lock",
    "release_workspace_lock",
]
