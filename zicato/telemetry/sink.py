"""Per-run sink wiring on top of goldfive's :class:`JSONLPersistenceSink`.

Zicato does not define its own EventSink primitive — goldfive's
JSONL-backed sink does the right thing (proto-canonical serialisation,
asyncio-safe writes, one line per event). What zicato adds is the
*routing*: every run writes to a stable per-(epoch, generation, entry)
path under the workspace root, and the sink is constructed in ``"write"``
mode so reruns cannot corrupt earlier event boundaries by appending.

The factory is the only place that imports ``goldfive.sinks.persistence``,
and it does so lazily. That keeps :mod:`zicato.telemetry` importable in
environments where goldfive is not (yet) installed — useful for unit
tests over pure-dataclass surface, for ``zicato --help``, and for the
CLI's path-introspection commands.

Path layout is delegated to :mod:`zicato.core.workspace`: there is
exactly one canonical path math definition for the workspace, and it
lives there. This module composes ``events_jsonl_path`` with a parent-
directory ``mkdir`` so the goldfive sink can lazily open the file
without the caller pre-creating the directory tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.core.workspace import events_jsonl_path


def make_run_sink_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Return the per-run ``events.jsonl`` path and ensure its parent exists.

    The path itself is computed by
    :func:`zicato.core.workspace.events_jsonl_path` so the layout stays
    pinned to the workspace contract. We additionally ``mkdir(parents=
    True, exist_ok=True)`` on the run directory so callers that build
    the sink lazily — goldfive's sink opens the file handle on first
    emit — do not need to remember to pre-create the tree.

    Returning the path (not the sink) is deliberate: tests and CLI
    introspection commands need to know where the JSONL will land
    without constructing a sink, and the reducer needs to read the same
    path the sink wrote to. Both call this helper.
    """
    path = events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_run_sink(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Any:
    """Construct a goldfive :class:`JSONLPersistenceSink` for one run.

    The sink is configured in ``mode="write"`` so a rerun overwrites the
    prior events file rather than corrupting it with appended events
    from a fresh attempt. Run boundaries are file-level by design (see
    the telemetry-path note); the post-run reducer assumes one events
    file = one run.

    Goldfive is imported lazily here so this module is import-safe even
    when goldfive is not installed. The return type is annotated as
    :class:`Any` for the same reason — typing it as
    ``JSONLPersistenceSink`` would force a top-level goldfive import
    that the module would never recover from in a no-goldfive
    environment.

    Raises
    ------
    ModuleNotFoundError
        If goldfive is not importable. The original error is preserved
        as the cause so the caller can distinguish "telemetry needs
        goldfive but it's not installed" from any other import failure.
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink
    except ModuleNotFoundError as exc:  # pragma: no cover — exercised in tests
        raise ModuleNotFoundError(
            "zicato.telemetry.sink.make_run_sink requires the goldfive "
            "package to be installed; install it (or the appropriate "
            "extra) and retry."
        ) from exc

    path = make_run_sink_path(workspace_root, epoch_id, generation_id, entry_id)
    return JSONLPersistenceSink(path=path, mode="write")


__all__ = ["make_run_sink_path", "make_run_sink"]
