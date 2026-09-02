"""The run-transcript payload, assembled in the query layer.

The run-level conversation surfaces read their payloads from here rather
than assembling them in the dashboard endpoints. The reader resolves the
events file (:mod:`zicato.query.events_index`), reconstructs it
(:func:`zicato.query.transcript_reconstruction.reconstruct_transcript`) and
STAMPS the resolved coordinates onto the reconstructed payload (``epoch_id``
/ ``generation_id`` / ``entry_id`` and a fallback ``run_id``) — a documented
reader step, so the frontend can label the transcript column without a
second lookup, and each field keeps one server-owned spelling on the wire.

Every function degrades to the same-shaped empty payload and never raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.query.events_index import (
    find_generation_entry_events,
    find_proposal_episode_log,
    find_run_events_path,
    resolve_transcript_events,
)
from zicato.query.paths import WorkspacePaths
from zicato.query.run_log import clamp_run_log_limit
from zicato.query.transcript_reconstruction import reconstruct_transcript

#: The follow pane renders an events reconstruction rather than verbatim capture.
FIDELITY_EVENTS = "events"


def _empty_execution() -> dict[str, Any]:
    return {
        "fidelity": "unavailable",
        "nodes": [],
        "root_ids": [],
        "unresolved_ids": [],
    }


def _add_run_artifacts(payload: dict[str, Any], events_path: Path) -> None:
    """Add the durable artifact inventory at run scope without inferring producers."""
    try:
        manifest = json.loads((events_path.parent / "artifacts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    files = manifest.get("files") if isinstance(manifest, dict) else None
    execution = payload.setdefault("execution", _empty_execution())
    for item in files if isinstance(files, list) else []:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path:
            continue
        node_id = f"artifact:{path}"
        execution["nodes"].append(
            {
                "node_id": node_id,
                "kind": "artifact",
                "parent_id": None,
                "name": path,
                "status": "captured",
                "summary": f"{item.get('size', 0)} bytes",
                "fidelity": "run",
            }
        )
        execution["root_ids"].append(node_id)
        execution["fidelity"] = "partial"


def resolve_conversation(
    paths: WorkspacePaths,
    run_id: str,
    *,
    gen: str | None = None,
    entry: str | None = None,
    epoch: str | None = None,
    slot: int | None = None,
) -> Path | None:
    """Resolve by deterministic generation and entry before opaque run id.

    A generation named WITHOUT a board entry resolves to its proposal
    episode. A generation has two kinds of conversation: one per board entry
    it was evaluated on, and the single Foe episode that proposed it. The
    entry coordinate is what tells them apart, so a caller that omits it is
    asking for the proposal. ``slot`` names one best-of-N slate slot; without
    it the lowest-numbered slot answers.
    """
    events_path: Path | None = None
    if gen and entry:
        events_path = resolve_transcript_events(paths, epoch or "", gen, entry, run_id=run_id)
        if events_path is None:
            # Strict to the entry's own run dir — never a sibling's.
            events_path = find_generation_entry_events(paths, gen, entry)
    elif gen:
        # The episode log or nothing: a proposal transcript is served from a
        # Foe episode and from no other source, so an absent episode must not
        # fall through to some run's event stream.
        return find_proposal_episode_log(paths, epoch or "", gen, slot_index=slot)
    if events_path is None:
        events_path = find_run_events_path(paths, run_id)
    return events_path


def empty_run_transcript(
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The same-shaped empty transcript payload.

    ``error`` is attached only when given — the genuine-absence path
    carries no error key (the frontend renders the "could not be
    reconstructed" message off the zero turns alone).
    """
    payload: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "run_id": run_id,
        "turns": [],
        "annotations": [],
        "execution": _empty_execution(),
        "event_count": 0,
        "complete": False,
    }
    if error is not None:
        payload["error"] = error
    return payload


def build_run_transcript(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    match_id: str | None = None,
) -> dict[str, Any]:
    """Build one coordinate-stamped transcript with a same-shaped degrade."""
    events_path = resolve_transcript_events(
        paths,
        epoch_id,
        generation_id,
        entry_id,
        run_id=run_id,
        match_id=match_id,
    )
    if events_path is None:
        # Genuine absence: no events.jsonl exists for this gen×entry at
        # all. The honest empty shape (no error key) — the frontend
        # renders the "could not be reconstructed" message.
        return empty_run_transcript(epoch_id, generation_id, entry_id, run_id=run_id)
    resolved_run_id = run_id or entry_id
    try:
        payload: dict[str, Any] = reconstruct_transcript(events_path, partial_ok=True).to_dict()
    except Exception as exc:  # noqa: BLE001 — best-effort, never raises
        return empty_run_transcript(
            epoch_id,
            generation_id,
            entry_id,
            run_id=resolved_run_id,
            error=f"transcript failed: {exc}",
        )
    if not payload.get("run_id"):
        payload["run_id"] = resolved_run_id
    payload["epoch_id"] = epoch_id
    payload["generation_id"] = generation_id
    payload["entry_id"] = entry_id
    payload.setdefault("execution", _empty_execution())
    _add_run_artifacts(payload, events_path)
    return payload


def empty_run_transcript_delta(
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The same-shaped not-found delta, ``found`` false.

    A cursor of ``0`` is the honest "nothing consumed" answer: a follower
    that keeps polling with it receives the whole transcript the moment
    the run's events file appears.
    """
    payload: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "run_id": run_id,
        "found": False,
        "cursor": 0,
        "turns": [],
        "annotations": [],
        "execution": _empty_execution(),
        "turn_total": 0,
        "event_count": 0,
        "complete": False,
        "truncated": False,
        "fidelity": FIDELITY_EVENTS,
        "verbatim_available": False,
        "events_path": None,
    }
    if error is not None:
        payload["error"] = error
    return payload


def _verbatim_capture_exists(events_path: Path) -> bool:
    """Report whether a valid higher-fidelity ``result.json`` exists."""
    from zicato.tournament.unit_cache import read_run_result  # noqa: PLC0415

    return read_run_result(events_path.parent / "result.json") is not None


def build_run_transcript_delta(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    after: int | None = None,
    limit: int | None = None,
    run_id: str | None = None,
    match_id: str | None = None,
) -> dict[str, Any]:
    """Return the bounded transcript changes at or beyond a parsed-event cursor."""
    events_path = resolve_transcript_events(
        paths,
        epoch_id,
        generation_id,
        entry_id,
        run_id=run_id,
        match_id=match_id,
    )
    if events_path is None:
        return empty_run_transcript_delta(epoch_id, generation_id, entry_id, run_id=run_id)
    resolved_run_id = run_id or entry_id
    try:
        full: dict[str, Any] = reconstruct_transcript(events_path, partial_ok=True).to_dict()
    except Exception as exc:  # noqa: BLE001 — best-effort, never raises
        return empty_run_transcript_delta(
            epoch_id,
            generation_id,
            entry_id,
            run_id=resolved_run_id,
            error=f"transcript failed: {exc}",
        )

    _add_run_artifacts(full, events_path)

    floor = after if isinstance(after, int) and after > 0 else 0
    cap = clamp_run_log_limit(limit)

    all_turns = full.get("turns") or []
    delta_turns = [
        {**turn, "turn_index": i}
        for i, turn in enumerate(all_turns)
        if int(turn.get("source_index", -1)) >= floor
    ]
    truncated = len(delta_turns) > cap
    if truncated:
        delta_turns = delta_turns[-cap:]

    all_anns = full.get("annotations") or []
    delta_anns = [a for a in all_anns if int(a.get("source_index", -1)) >= floor]

    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "run_id": full.get("run_id") or resolved_run_id,
        "found": True,
        "cursor": int(full.get("event_count") or 0),
        "turns": delta_turns,
        "annotations": delta_anns,
        "execution": full.get("execution") or _empty_execution(),
        "turn_total": len(all_turns),
        "event_count": int(full.get("event_count") or 0),
        "complete": bool(full.get("complete")),
        "truncated": truncated,
        "fidelity": FIDELITY_EVENTS,
        "verbatim_available": _verbatim_capture_exists(events_path),
        "events_path": str(events_path),
    }
