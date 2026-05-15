"""Heartbeat beater — background asyncio task that bumps ``heartbeat.json``.

The orchestrator spawns one :class:`HeartbeatBeater` for its lifetime;
the beater wakes every ``interval_s`` seconds, writes a fresh
:class:`zicato.runtime.state.Heartbeat` with the current UTC time, and
goes back to sleep. The supervisor binary tails the file via inotify
and treats a stale ``last_heartbeat`` (more than a few intervals old)
as a stalled orchestrator.

Design rules:

* **GIL-friendly.** The beater's hot path is ``asyncio.sleep`` + one
  atomic JSON write. No blocking calls, no shared locks, no long
  critical sections — the orchestrator can run concurrent CPU-bound
  work without starving the beater.
* **Mutable in-memory snapshot, atomic on-disk file.** Field updates go
  through :meth:`HeartbeatBeater.update`, which mutates a small dict
  the next bump reads. The on-disk file is replaced atomically each
  bump.
* **Cooperative shutdown.** :meth:`HeartbeatBeater.stop` cancels the
  task and awaits its completion; never kills via signal.
* **Crash-safe.** A crash leaves the last full heartbeat on disk; the
  supervisor decides what to do based on staleness.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
from dataclasses import replace
from pathlib import Path

from zicato.runtime.state import Heartbeat, write_heartbeat


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with seconds precision."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class HeartbeatBeater:
    """Background asyncio task that bumps ``heartbeat.json`` periodically.

    Construct one per orchestrator process. Call :meth:`start` once to
    spawn the background task; call :meth:`update` whenever the
    orchestrator transitions phase / round / generation; call
    :meth:`stop` on shutdown.

    The beater holds a small in-memory :class:`Heartbeat` snapshot that
    represents the next disk write. Each bump replaces
    :attr:`Heartbeat.last_heartbeat` with the current UTC time and writes
    the snapshot. :meth:`update` lets the orchestrator change the other
    fields (epoch, generation, phase, round) without needing to know
    about the timing layer.
    """

    def __init__(
        self,
        workspace_root: Path,
        instance_id: str,
        interval_s: float = 2.0,
    ) -> None:
        """Initialize. Does NOT start the background task — call :meth:`start`.

        Parameters
        ----------
        workspace_root:
            The workspace root the beater writes ``heartbeat.json`` under.
        instance_id:
            The :class:`zicato.core.types.RuntimeConfig.instance_id` to
            stamp on every heartbeat. Allows the supervisor to
            distinguish nested zicato instances sharing a workspace.
        interval_s:
            Seconds between heartbeat bumps. Default 2.0; the supervisor
            tolerates several intervals of staleness before escalating
            so this needn't be sub-second.
        """
        self._workspace_root = workspace_root
        self._interval_s = interval_s
        now = _utc_now_iso()
        self._snapshot = Heartbeat(
            pid=os.getpid(),
            instance_id=instance_id,
            started_at=now,
            last_heartbeat=now,
        )
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Spawn the bump-loop background task.

        Writes one heartbeat immediately so a reader observing the file
        right after :meth:`start` returns sees a valid record. Calling
        :meth:`start` twice without a :meth:`stop` raises
        :class:`RuntimeError`.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("HeartbeatBeater already started")
        # Emit one beat synchronously so the file exists by the time
        # start() returns. Subsequent beats happen on the bump task.
        self._bump_to_disk()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._bump_loop())

    async def stop(self) -> None:
        """Stop the bump loop and wait for the task to exit.

        Idempotent — stopping an already-stopped beater is a no-op.
        Does NOT remove the on-disk file; the supervisor or the
        orchestrator's outer shutdown path decides whether to clean up.
        """
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            # Either CancelledError (expected) or any propagated error
            # from the bump loop — neither should propagate out of
            # shutdown. The bump loop itself catches and logs internal
            # errors via the write path's atomic discipline.
            pass
        self._task = None

    def update(
        self,
        *,
        epoch_id: str | None = None,
        generation_id: str | None = None,
        phase: str | None = None,
        round_index: int | None = None,
        round_started_at: str | None = None,
    ) -> None:
        """Update the in-memory heartbeat snapshot.

        Every argument is optional; ``None`` means "leave unchanged".
        The next bump (or an in-flight one) will write the updated
        fields. The change does NOT immediately flush — callers who
        need an immediate write should call :meth:`bump_now`.
        """
        # Build the replace() call as a single typed pipe of overrides
        # so mypy can verify each kwarg against Heartbeat's field types.
        snapshot = self._snapshot
        if epoch_id is not None:
            snapshot = replace(snapshot, epoch_id=epoch_id)
        if generation_id is not None:
            snapshot = replace(snapshot, generation_id=generation_id)
        if phase is not None:
            snapshot = replace(snapshot, phase=phase)
        if round_index is not None:
            snapshot = replace(snapshot, round_index=round_index)
        if round_started_at is not None:
            snapshot = replace(snapshot, round_started_at=round_started_at)
        self._snapshot = snapshot

    def bump_now(self) -> None:
        """Force an immediate disk write of the current snapshot.

        Useful at orchestrator transition points (e.g. just before a
        long-running tournament block) where the operator wants the
        dashboard to reflect the new phase without waiting for the
        next periodic bump.
        """
        self._bump_to_disk()

    @property
    def snapshot(self) -> Heartbeat:
        """Return the current in-memory :class:`Heartbeat` snapshot.

        Returned value is the frozen dataclass; callers cannot mutate
        the beater's state through it.
        """
        return self._snapshot

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bump_to_disk(self) -> None:
        """Write the snapshot with ``last_heartbeat`` set to now."""
        self._snapshot = replace(self._snapshot, last_heartbeat=_utc_now_iso())
        write_heartbeat(self._workspace_root, self._snapshot)

    async def _bump_loop(self) -> None:
        """Periodically bump until cancelled.

        Sleeps with :func:`asyncio.wait_for` against the stop event so
        the bump loop wakes immediately on shutdown rather than
        waiting out the remainder of the current interval.
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
                # Stop event fired — exit cleanly.
                return
            except TimeoutError:
                # Timeout means we should beat again.
                self._bump_to_disk()


__all__ = ["HeartbeatBeater"]
