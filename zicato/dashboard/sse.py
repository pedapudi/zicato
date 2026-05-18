"""Server-sent-events broker for the dashboard service.

A new client receives a ``snapshot`` event built from the current state
files, then a live ``state_change`` event whenever a file under
``.zicato/runtime/`` mutates, plus a ``run_log`` event whenever a run's
``events.jsonl`` grows so the live conversation view can stream new
turns.

``state_change`` notifications are *coalesced*: a burst of file writes
(the orchestrator can touch the runtime tree many times a second) is
debounced into a single ``state_change`` frame carrying the set of
changed ``kind`` regions. The dashboard reacts with ONE coalesced
``/api/environment`` fetch — this is what stops the old flashing /
self-DoS where every file write fanned out into a fresh wave of
per-endpoint polls.

The watch layer prefers the :mod:`watchdog` library when it is
importable and falls back to a periodic poll loop otherwise. Either way
the broker exposes the same async iterator of change notifications, so
the rest of the server is agnostic to which backend is in play.

The wire protocol is ``event: snapshot`` then ``event: state_change``
lines, each ``data:`` carrying a JSON object — the front-end reads
``payload.kind`` (single region) and ``payload.kinds`` (the coalesced
set).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from zicato.dashboard.state_reader import WorkspacePaths, build_snapshot

try:  # watchdog is the preferred backend; the poll loop is the fallback.
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _HAVE_WATCHDOG = True
except Exception:  # pragma: no cover - exercised only without watchdog
    _HAVE_WATCHDOG = False


# How often the poll-loop fallback re-scans the runtime tree.
_POLL_INTERVAL_S = 1.0

# SSE keepalive ping cadence.
_KEEPALIVE_S = 15.0

# Debounce window for ``state_change`` coalescing. A burst of file
# writes inside this window is collapsed into a single ``state_change``
# frame, so the dashboard refreshes once per burst rather than once per
# write. 250ms keeps the UI feeling live while absorbing the
# orchestrator's write storms.
_COALESCE_WINDOW_S = 0.25


def _classify(path: Path, paths: WorkspacePaths) -> str:
    """Map a changed path to a ``state_change`` ``kind``.

    The vocabulary matches the Rust ``watcher::ChangeKind`` snake_case
    serialization; the dashboard JS reads ``payload.kind``.
    """
    try:
        if path == paths.heartbeat:
            return "heartbeat"
        if path == paths.lock:
            return "lock"
        if path == paths.active_tournament:
            return "active_tournament"
        if path == paths.lineage:
            return "lineage"
        if path == paths.current_epoch_marker:
            return "epoch"
        if paths.active_runs_dir in path.parents:
            return "active_runs"
        if paths.control_dir in path.parents:
            return "control"
        if paths.epochs == path or paths.epochs in path.parents:
            return "epoch"
    except Exception:
        pass
    return "unknown"


class ChangeBroker:
    """Fan-out of file-system change notifications to many SSE clients.

    A single watch backend feeds an internal queue; each subscriber gets
    its own bounded queue so a slow client never blocks the watcher or a
    sibling client.
    """

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Any | None = None
        self._poll_task: asyncio.Task[None] | None = None
        # Tracks events.jsonl sizes so a grow can be reported as a
        # `run_log` event for the live conversation stream.
        self._events_sizes: dict[str, int] = {}
        # Coalescing state: pending changed `kind`s and the scheduled
        # flush handle. A burst of file writes accumulates kinds here;
        # the flush, _COALESCE_WINDOW_S later, emits one state_change.
        self._pending_kinds: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Begin watching. Idempotent."""
        self._loop = asyncio.get_running_loop()
        if _HAVE_WATCHDOG:
            self._start_watchdog()
        else:  # pragma: no cover - exercised only without watchdog
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop watching and release resources. Idempotent."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending_kinds.clear()

    # -- subscription -------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    # -- fan-out ------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        """Push one payload to every subscriber, dropping on a full queue."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow client: drop rather than block the watcher.
                pass

    def _emit_threadsafe(self, payload: dict[str, Any]) -> None:
        """Thread-safe ``_emit`` for the watchdog observer thread."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._emit, payload)

    def _on_path_changed(self, raw_path: str) -> None:
        """Handle one changed path (called from the watch backend).

        The ``state_change`` for this path is *not* emitted immediately;
        its ``kind`` is folded into the pending set and a debounced
        flush is (re)scheduled. A burst of writes therefore produces a
        single coalesced ``state_change`` frame. The ``run_log`` event
        for an ``events.jsonl`` growth is still emitted promptly so the
        live conversation stream stays responsive.
        """
        path = Path(raw_path)
        # Atomic-write intermediates are pure noise.
        if path.suffix == ".tmp":
            return
        kind = _classify(path, self.paths)
        self._schedule_state_change(kind)
        # An events.jsonl write also drives the live conversation stream.
        if path.name == "events.jsonl":
            self._report_events_growth(path)

    def _schedule_state_change(self, kind: str) -> None:
        """Fold ``kind`` into the pending set and (re)arm the flush.

        Called from the watch backend thread; hops onto the event loop
        so the timer and the pending set are only ever touched there.
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._accumulate_kind, kind)

    def _accumulate_kind(self, kind: str) -> None:
        """Event-loop-side: record a changed kind, arm the flush timer."""
        self._pending_kinds.add(kind)
        if self._flush_handle is None and self._loop is not None:
            self._flush_handle = self._loop.call_later(_COALESCE_WINDOW_S, self._flush_state_change)

    def _flush_state_change(self) -> None:
        """Emit one ``state_change`` for every kind seen in the window."""
        self._flush_handle = None
        kinds = sorted(self._pending_kinds)
        self._pending_kinds.clear()
        if not kinds:
            return
        # `kind` carries a single region for back-compat with a client
        # that reads only one; `kinds` carries the whole coalesced set.
        self._emit(
            {
                "event": "state_change",
                "data": {
                    "type": "state_change",
                    "kind": kinds[0] if len(kinds) == 1 else "multiple",
                    "kinds": kinds,
                    "ts": _now_iso(),
                },
            }
        )

    def _report_events_growth(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        key = str(path)
        prev = self._events_sizes.get(key)
        self._events_sizes[key] = size
        if prev is not None and size <= prev:
            return
        self._emit_threadsafe(
            {
                "event": "run_log",
                "data": {
                    "type": "run_log",
                    "events_path": key,
                    "size": size,
                    "ts": _now_iso(),
                },
            }
        )

    # -- watchdog backend --------------------------------------------

    def _start_watchdog(self) -> None:
        broker = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                src = getattr(event, "src_path", None)
                if src:
                    broker._on_path_changed(str(src))
                dest = getattr(event, "dest_path", None)
                if dest:
                    broker._on_path_changed(str(dest))

        observer = Observer()
        handler = _Handler()
        # Ensure watched roots exist so the observer does not error.
        for d in (self.paths.runtime, self.paths.epochs):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        for d in (self.paths.runtime, self.paths.epochs):
            if d.is_dir():
                observer.schedule(handler, str(d), recursive=True)
        # The workspace root non-recursively for current_epoch / lineage.json.
        if self.paths.root.is_dir():
            observer.schedule(handler, str(self.paths.root), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    # -- poll-loop fallback ------------------------------------------

    async def _poll_loop(self) -> None:  # pragma: no cover - no-watchdog path
        seen: dict[str, float] = {}
        roots = [self.paths.runtime, self.paths.epochs]
        single = [self.paths.current_epoch_marker, self.paths.lineage]
        while True:
            try:
                current: dict[str, float] = {}
                for root in roots:
                    if not root.is_dir():
                        continue
                    for p in root.rglob("*"):
                        if p.is_file():
                            try:
                                current[str(p)] = p.stat().st_mtime
                            except OSError:
                                pass
                for p in single:
                    if p.exists():
                        try:
                            current[str(p)] = p.stat().st_mtime
                        except OSError:
                            pass
                for key, mtime in current.items():
                    if seen.get(key) != mtime:
                        self._on_path_changed(key)
                seen = current
            except Exception:
                pass
            await asyncio.sleep(_POLL_INTERVAL_S)


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _format_sse(event: str, data: Any) -> str:
    """Encode one SSE frame: an ``event:`` line plus a ``data:`` line."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def sse_event_stream(broker: ChangeBroker, paths: WorkspacePaths) -> AsyncIterator[str]:
    """Async generator backing ``GET /events``.

    Yields a ``snapshot`` frame first, then ``state_change`` / ``run_log``
    frames as files mutate, with a periodic ``ping`` keepalive comment.
    """
    queue = broker.subscribe()
    try:
        snapshot = build_snapshot(paths)
        yield _format_sse("snapshot", {"type": "snapshot", "data": snapshot})
        last_ping = time.monotonic()
        while True:
            timeout = max(0.0, _KEEPALIVE_S - (time.monotonic() - last_ping))
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                # Keepalive comment line — the Rust server sends `: ping`.
                yield ": ping\n\n"
                last_ping = time.monotonic()
                continue
            yield _format_sse(payload["event"], payload["data"])
    finally:
        broker.unsubscribe(queue)
