"""transcript_view — the run-transcript payload, served (not endpoint-assembled).

The L4 conversation surfaces used to assemble their payloads inside the
dashboard endpoints; this module is that logic moved into the query
layer per the reader recipe. Two seams matter:

* The query layer must stay dashboard-free (the import-linter contract),
  but the transcript reconstructor lives in ``zicato.dashboard.transcript``
  — so :func:`build_run_transcript` takes it as an INJECTED callable
  (``reconstruct``). A ``None`` reconstructor degrades to the honest
  "transcript reconstruction unavailable" shape rather than importing
  across the boundary.
* The reader STAMPS the resolved coordinates onto the reconstructed
  payload (``epoch_id`` / ``generation_id`` / ``entry_id`` and a
  fallback ``run_id``) — a documented reader step, so the frontend can
  label the transcript column without a second lookup and the payload
  spelling stays server-owned (DQ2).

Every function degrades to the same-shaped empty payload (DQ3), never
raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.query.events_index import (
    find_generation_entry_events,
    find_run_events_path,
    resolve_transcript_events,
)
from zicato.query.paths import WorkspacePaths


def resolve_conversation(
    paths: WorkspacePaths,
    run_id: str,
    *,
    gen: str | None = None,
    entry: str | None = None,
    epoch: str | None = None,
) -> Path | None:
    """Resolve a conversation's ``events.jsonl`` — gen×entry-FIRST.

    The deterministic triple is the primary key: when the caller supplies
    ``gen``/``entry`` (and optionally ``epoch``), resolve straight to
    ``generations/<gen>/runs/<entry>/events.jsonl`` — strict to that
    entry's own run dir, with the ``run_id`` only a disambiguator. This
    inverts the prior run_id-first order, which kept failing on reused /
    index-only run_ids. Falls back to the opaque run_id lookup only when
    the triple is absent or resolves to nothing (a pure-run_id caller
    with no coordinates). Returns ``None`` when nothing resolves.
    """
    events_path: Path | None = None
    if gen and entry:
        events_path = resolve_transcript_events(paths, epoch or "", gen, entry, run_id=run_id)
        if events_path is None:
            # Strict to the entry's own run dir — never a sibling's.
            events_path = find_generation_entry_events(paths, gen, entry)
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
    """The same-shaped empty transcript payload (DQ3).

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
    reconstruct: Any = None,
) -> dict[str, Any]:
    """The transcript for one ``(epoch, gen, entry)`` run, coordinates stamped.

    Powers the L4 conversation diff: the focused-run side fetches the
    transcript via this reader, and the compare side fetches it again
    with the picker's selected generation. Returns the reconstructor's
    ``Transcript.to_dict()`` shape plus the resolved coordinates.

    PRIMARY resolution is the deterministic triple —
    ``generations/<gen>/runs/<entry>/events.jsonl``, strict to this
    entry's OWN run directory (never a sibling's). An optional ``run_id``
    / ``match_id`` disambiguator selects a specific rung when a
    gen×entry has multiple runs (successive-halving re-races); without
    one we DEFAULT to the entry's own canonical events file.

    ``reconstruct`` is the injected transcript reconstructor (the query
    layer never imports the dashboard); ``None`` degrades to the
    unavailable shape. Every failure path answers the same-shaped empty
    payload (DQ3) — absent run, failed reconstruction, no reconstructor.
    """
    if reconstruct is None:
        return empty_run_transcript(
            epoch_id,
            generation_id,
            entry_id,
            error="transcript reconstruction unavailable",
        )
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
        payload: dict[str, Any] = reconstruct(events_path, partial_ok=True).to_dict()
    except Exception as exc:  # noqa: BLE001 — best-effort, never raises (DQ3)
        return empty_run_transcript(
            epoch_id,
            generation_id,
            entry_id,
            run_id=resolved_run_id,
            error=f"transcript failed: {exc}",
        )
    # The documented STAMPING step: the reconstructor sets its own run_id
    # from the events stream; surface the directory-name run_id explicitly
    # when the reducer produced no value (empty file), then stamp the
    # resolved coordinates so the frontend labels the column without a
    # second lookup.
    if not payload.get("run_id"):
        payload["run_id"] = resolved_run_id
    payload["epoch_id"] = epoch_id
    payload["generation_id"] = generation_id
    payload["entry_id"] = entry_id
    return payload
