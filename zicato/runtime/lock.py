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

import datetime as _dt
import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.runtime._storage import backend_for, lock_key
from zicato.runtime.paths import ensure_runtime_dirs


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with seconds precision."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    """

    pid: int
    instance_id: str
    acquired_at: str
    workspace_root: Path

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON encoding."""
        return {
            "pid": self.pid,
            "instance_id": self.instance_id,
            "acquired_at": self.acquired_at,
            "workspace_root": str(self.workspace_root),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkspaceLock:
        """Construct from a JSON-decoded dict."""
        return cls(
            pid=int(d["pid"]),
            instance_id=str(d["instance_id"]),
            acquired_at=str(d["acquired_at"]),
            workspace_root=Path(d["workspace_root"]),
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
        operators running a deliberately conservative deployment can set
        it to ``False`` to require manual cleanup of stale locks.
    """
    ensure_runtime_dirs(workspace_root)
    backend = backend_for(workspace_root)
    my_pid = os.getpid()

    existing = backend.read_json(lock_key())
    if existing is not None:
        prior = WorkspaceLock.from_dict(existing)
        if prior.pid == my_pid:
            # Idempotent re-acquisition: keep the original acquired_at
            # so observers can see when the lock first appeared.
            return prior
        # Different pid. Is it alive?
        if is_pid_alive(prior.pid):
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
        # Fall through to overwrite.

    lock = WorkspaceLock(
        pid=my_pid,
        instance_id=instance_id,
        acquired_at=_utc_now_iso(),
        workspace_root=workspace_root,
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
    "is_pid_alive",
    "acquire_workspace_lock",
    "release_workspace_lock",
]
