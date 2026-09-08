"""Exclusive workspace writers with readable process-identity metadata.

A kernel lock on a stable guard file serializes acquisition and remains held
until the writer releases ownership or exits. The sibling JSON record lets
supervisors and readers inspect ownership without acquiring it. Independent
invocations in one process must acquire separate handles and therefore compete.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4
from weakref import WeakSet

from zicato.runtime._storage import active_tournament_log_key, lock_key, progress_log_key
from zicato.runtime.channel import EventLog
from zicato.runtime.paths import ensure_runtime_dirs, lock_guard_path
from zicato.storage import workspace_backend
from zicato.util.iso_time import now_iso as _utc_now_iso


class WorkspaceLockHeld(RuntimeError):
    """Raised when another invocation owns the workspace."""


@dataclass(eq=False, slots=True, weakref_slot=True)
class _WriterLease:
    fd: int
    progress_log: EventLog
    tournament_log: EventLog

    def __post_init__(self) -> None:
        _writer_leases.add(self)

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            _writer_leases.discard(self)
            os.close(fd)


_writer_leases: WeakSet[_WriterLease] = WeakSet()


def _close_inherited_leases() -> None:
    # Closing the child's descriptor preserves the parent's shared lock.
    # LOCK_UN would also unlock the parent's open file description.
    for lease in tuple(_writer_leases):
        try:
            lease.close()
        except OSError:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_leases)


@dataclass(frozen=True, slots=True)
class WorkspaceLock:
    """An owned writer handle, or a read-only description of its metadata.

    Only acquisition attaches a live lease. Deserializing the JSON cannot
    authorize mutation or release. The owner identifier distinguishes successive
    invocations even when their process and configured instance are identical.
    """

    pid: int
    instance_id: str
    acquired_at: str
    workspace_root: Path
    start_time: float | None = None
    owner_id: str | None = None
    _lease: _WriterLease | None = field(default=None, repr=False, compare=False)

    @property
    def progress_log(self) -> EventLog:
        """Return the progress writer while this process holds its lease."""
        return self._owned_lease().progress_log

    @property
    def tournament_log(self) -> EventLog:
        """Return the shared tournament writer while this process holds its lease."""
        return self._owned_lease().tournament_log

    def _owned_lease(self) -> _WriterLease:
        # The kernel lease stays exclusive until close; publication needs no
        # repeated metadata read. Fork cleanup closes the inherited descriptor.
        if self._lease is None or self._lease.fd < 0 or self.pid != os.getpid():
            raise WorkspaceLockHeld(
                f"workspace {self.workspace_root} requires its acquired writer handle"
            )
        return self._lease

    def to_dict(self) -> dict[str, Any]:
        """Serialize inspection metadata without granting ownership."""
        result = {
            "pid": self.pid,
            "instance_id": self.instance_id,
            "acquired_at": self.acquired_at,
            "workspace_root": str(self.workspace_root),
            "start_time": self.start_time,
        }
        if self.owner_id is not None:
            result["owner_id"] = self.owner_id
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkspaceLock:
        """Read metadata, including records written without an owner identifier."""
        raw_start = d.get("start_time")
        return cls(
            pid=int(d["pid"]),
            instance_id=str(d["instance_id"]),
            acquired_at=str(d["acquired_at"]),
            workspace_root=Path(d["workspace_root"]),
            start_time=float(raw_start) if raw_start is not None else None,
            owner_id=d.get("owner_id"),
        )

    def __enter__(self) -> WorkspaceLock:
        try:
            validate_workspace_lock(self, self.workspace_root)
        except BaseException:
            # A failed scope entry has no matching __exit__ call.
            try:
                release_workspace_lock(self)
            except BaseException:
                pass
            raise
        return self

    def __exit__(self, *_: object) -> None:
        release_workspace_lock(self)


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


def signal_owned_process(
    pid: int,
    expected_start_time: float | None,
    pgid: int | None,
    sig: int,
    *,
    leader_exited: bool,
) -> bool:
    """Signal a verified leader or the group retained after its confirmed exit.

    An unreadable live identity refuses signalling. The caller must establish
    leader exit independently before a missing start token can retain a group.
    """
    if pid <= 1 or expected_start_time is None:
        return False
    current = pid_start_time(pid)
    if current != expected_start_time and (current is not None or not leader_exited):
        return False
    try:
        if pgid is None:
            if not leader_exited:
                os.kill(pid, sig)
        else:
            if pgid != pid or pgid <= 1 or pgid == os.getpgrp():
                return False
            if not leader_exited and os.getpgid(pid) != pgid:
                return False
            os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    return True


def group_has_live_members(pgid: int) -> bool:
    """Treat unreadable membership as live; exited zombies cannot execute code."""
    if pgid <= 1 or pgid == os.getpgrp():
        return True
    try:
        processes = tuple(Path("/proc").iterdir())
    except OSError:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            raw = (process / "stat").read_text()
            fields = raw[raw.rindex(")") + 1 :].split()
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            return True
    return False


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
    that recorded ``expected_start_time``. That defeats pid reuse, which
    otherwise misleads in two directions: a dead worker's pid reissued to
    an unrelated process makes the dead worker look alive and declines a
    steal, and a stale lock's pid number now belonging to an innocent
    bystander makes this process refuse to start, or mis-target it.

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
    backend = workspace_backend(workspace_root, start=False)
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
    """Acquire an exclusive writer and publish its inspection metadata.

    Acquiring twice in one process is refused. Private operations reuse the
    existing handle explicitly through :func:`validate_workspace_lock`.
    Exclusive acquisition proves that a recorded kernel lease has ended.
    Records without an owner identifier can represent a writer using process
    metadata alone; their live process still blocks takeover.
    """
    import fcntl  # noqa: PLC0415

    workspace_root = workspace_root.resolve()
    ensure_runtime_dirs(workspace_root)
    # Never unlink this guard: every contender must lock the same inode.
    backend = workspace_backend(workspace_root, start=False)
    lease = _WriterLease(
        os.open(lock_guard_path(workspace_root), os.O_CREAT | os.O_RDWR, 0o600),
        EventLog(backend, progress_log_key()),
        EventLog(backend, active_tournament_log_key()),
    )
    try:
        try:
            fcntl.flock(lease.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceLockHeld(f"workspace {workspace_root} has an active writer") from exc
        existing = backend.read_json(lock_key())
        if existing is not None:
            prior = WorkspaceLock.from_dict(existing)
            recorded_lease = isinstance(prior.owner_id, str) and bool(prior.owner_id)
            if not recorded_lease and is_same_process(prior.pid, prior.start_time):
                raise WorkspaceLockHeld(
                    f"workspace {workspace_root} locked by live pid {prior.pid} "
                    f"(instance {prior.instance_id!r}, acquired {prior.acquired_at})"
                )
            if not steal_stale:
                raise WorkspaceLockHeld(
                    f"workspace {workspace_root} has stale writer metadata for pid {prior.pid} "
                    f"(instance {prior.instance_id!r}, acquired {prior.acquired_at}); "
                    "refusing to steal with steal_stale=False"
                )
        lock = WorkspaceLock(
            pid=os.getpid(),
            instance_id=instance_id,
            acquired_at=_utc_now_iso(),
            workspace_root=workspace_root,
            start_time=pid_start_time(os.getpid()),
            owner_id=uuid4().hex,
            _lease=lease,
        )
        backend.write_json(lock_key(), lock.to_dict())
        return lock
    except BaseException:
        lease.close()
        raise


def validate_workspace_lock(writer: WorkspaceLock, workspace_root: Path) -> None:
    """Require the live writer handle acquired for this workspace and process."""
    if (
        writer._lease is None
        or writer._lease.fd < 0
        or writer.pid != os.getpid()
        or writer.workspace_root != workspace_root.resolve()
        or not writer.owner_id
    ):
        raise WorkspaceLockHeld(f"workspace {workspace_root} requires its acquired writer handle")
    existing = workspace_backend(writer.workspace_root, start=False).read_json(lock_key())
    if existing != writer.to_dict():
        raise WorkspaceLockHeld(
            f"workspace {workspace_root} writer metadata changed during ownership"
        )


def release_workspace_lock(lock: WorkspaceLock) -> None:
    """Remove owned metadata and close the lease; repeated release is harmless."""
    if lock._lease is None or lock._lease.fd < 0:
        return
    try:
        if lock.pid == os.getpid():
            backend = workspace_backend(lock.workspace_root, start=False)
            if backend.read_json(lock_key()) == lock.to_dict():
                backend.delete(lock_key())
    finally:
        lock._lease.close()


__all__ = [
    "WorkspaceLockHeld",
    "WorkspaceLock",
    "read_workspace_lock",
    "is_pid_alive",
    "pid_start_time",
    "signal_owned_process",
    "group_has_live_members",
    "is_same_process",
    "acquire_workspace_lock",
    "validate_workspace_lock",
    "release_workspace_lock",
]
