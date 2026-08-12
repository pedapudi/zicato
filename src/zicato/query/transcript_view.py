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
from zicato.query.run_log import clamp_run_log_limit

#: What the follow pane actually renders: the events.jsonl reconstruction.
#: A distinct token beside the reflection ladder's ``verbatim`` / ``result``
#: / ``preview`` tiers (:mod:`zicato.reflection.corpus`) — the pane captions
#: the tier of the bytes it drew, and this tier is neither of those.
FIDELITY_EVENTS = "events"


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


# ---------------------------------------------------------------------------
# The live-follow delta — cursor-append over a growing events.jsonl
# ---------------------------------------------------------------------------


def empty_run_transcript_delta(
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The same-shaped not-found delta (DQ3), ``found`` false.

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
    """Does a valid ``result.json`` sit beside this run's events file?

    The fidelity caption's second half: the pane always renders the events
    reconstruction, but the operator deserves to know when the higher-tier
    verbatim capture was retained on disk. Uses the tournament's own
    tolerant reader, so a missing / truncated / wrong-version file reads
    ``False`` rather than raising.
    """
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
    reconstruct: Any = None,
) -> dict[str, Any]:
    """The APPEND-ONLY slice of one run's transcript past ``after``.

    The live conversation pane's read. Resolution, coordinate stamping and
    the same-shape degrade are :func:`build_run_transcript`'s; what differs
    is that the caller carries a cursor and gets back only what changed,
    so following a running unit never re-sends a settled conversation.

    THE CURSOR is the count of PARSED events consumed — the value returned
    as ``cursor``, fed back as the next ``after``. It is deliberately not
    the goldfive ``sequence`` the run-log's cursor prefers: a
    ``multi_turn_emulated`` entry writes several goldfive runs into one
    events file and each restarts ``sequence`` at 0, so sequence is not
    monotone over the file while the parsed-event count always is. The
    run-log's other conventions carry over unchanged — a clamped
    ``limit``, and ``after=None`` (or negative) meaning "from the top".

    Because the cursor is a COUNT it sits one past the last index it
    covers, so the filter is inclusive: a turn is IN the delta when its
    ``source_index`` reaches ``after`` — which covers the two ways a
    transcript changes as a run streams: a
    brand-new turn lands, or the open final turn absorbs another event and
    grows (goldfive's ``llmCallStart`` → ``llmCallEnd`` merge). Each
    returned turn carries the ``turn_index`` it occupies in the full
    reconstruction, so the client splices rather than guesses. Replaying
    the same cursor therefore yields an EMPTY delta — the file has not
    grown, no ``source_index`` can exceed it.

    TORN LINES are tolerated exactly as the reconstructor tolerates them:
    a half-written final line takes no position in the parsed list, so the
    cursor never advances past it and the completed line arrives whole on
    a later poll.

    A delta longer than ``limit`` turns answers its TAIL with ``truncated``
    set — a follower that far behind has a gap it cannot splice, and the
    honest instruction is to re-read the full transcript.
    """
    if reconstruct is None:
        return empty_run_transcript_delta(
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
        return empty_run_transcript_delta(epoch_id, generation_id, entry_id, run_id=run_id)
    resolved_run_id = run_id or entry_id
    try:
        full: dict[str, Any] = reconstruct(events_path, partial_ok=True).to_dict()
    except Exception as exc:  # noqa: BLE001 — best-effort, never raises (DQ3)
        return empty_run_transcript_delta(
            epoch_id,
            generation_id,
            entry_id,
            run_id=resolved_run_id,
            error=f"transcript failed: {exc}",
        )

    # The cursor COUNTS events consumed, so it is one past the last index
    # it covers: a turn is new when it absorbed an event AT or past it. A
    # negative / absent cursor means "from the top", mirroring the
    # run-log's treatment of a missing ``after`` as the initial paint.
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
        "turn_total": len(all_turns),
        "event_count": int(full.get("event_count") or 0),
        "complete": bool(full.get("complete")),
        "truncated": truncated,
        "fidelity": FIDELITY_EVENTS,
        "verbatim_available": _verbatim_capture_exists(events_path),
        "events_path": str(events_path),
    }
