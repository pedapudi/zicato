"""The champion and challenger conversations for one board entry.

This module owns the join that pairs the two sides of a matchup and
reconstructs both transcripts, so the dashboard endpoint renders what it
is handed. The transcript reconstructor lives in
``zicato.dashboard.transcript`` and the query layer must stay
dashboard-free (the import-linter contract), so
:func:`build_matchup_conversations` takes it as an INJECTED callable.
Passing ``None`` degrades each side to a record without a transcript; no
reader here raises.
"""

from __future__ import annotations

from typing import Any

from zicato.query.events_index import find_generation_run, read_run_result
from zicato.query.paths import WorkspacePaths
from zicato.query.runtime_view import read_active_tournament_dict


def build_matchup_conversations(
    paths: WorkspacePaths, entry_id: str, *, reconstruct: Any = None
) -> dict[str, Any]:
    """Locate and reconstruct the champion + challenger conversations.

    For a board entry, the active tournament names a champion-side
    (``parent``) generation and a challenger-side (``child``) generation;
    each ran the entry once. This finds both runs' ``events.jsonl`` files
    and reconstructs both transcripts so the UI can render them side by
    side.

    Fast-mode caveat: in a fast-mode round the champion side is NOT
    actually executed — its ``status_raw`` is ``"cached"`` and the per-
    entry scalar is reused from the cached aggregate. The matching
    transcript on disk is the one this generation produced when it was
    the live challenger in its *original* tournament, persisted under
    its own generation directory. The active-tournament's per-entry
    ``generation_id`` is the correct lookup key;
    ``_normalize_tournament_statuses`` stamps it from the
    tournament-level parent and child fields. One code path then serves
    both sides: it routes a cached side through the cached generation's
    own runs directory and a live side through the in-progress round's
    runs directory.
    """
    result: dict[str, Any] = {"champion": None, "challenger": None}
    tournament = read_active_tournament_dict(paths)
    if not isinstance(tournament, dict):
        return result

    # Index per-(entry, side) so the side resolver can read both the
    # generation_id and the producer's status spelling. The normalizer
    # stamps a generation_id on every entry; the tournament-level fields
    # stay a fallback for a payload that carries only them.
    entries_index: dict[tuple[str, str], dict[str, Any]] = {}
    raw_entries = tournament.get("entries")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("entry_id")
            side = entry.get("side")
            if isinstance(eid, str) and isinstance(side, str):
                entries_index[(eid, side)] = entry

    tournament_parent_gen = tournament.get("parent_generation_id")
    tournament_child_gen = tournament.get("child_generation_id")

    def _resolve_generation_id(side: str, fallback: Any) -> Any:
        # Prefer the per-entry generation_id (stamped explicitly so a
        # cached row can carry a generation distinct from the current
        # round's champion-of-this-round id, if those ever differ). Fall
        # back to the tournament-level field for a payload that carries
        # only it.
        entry = entries_index.get((entry_id, side))
        if entry is not None:
            gen_id = entry.get("generation_id")
            if isinstance(gen_id, str) and gen_id:
                return gen_id
        return fallback

    def _side(side: str, generation_id: Any) -> dict[str, Any] | None:
        if not isinstance(generation_id, str) or not generation_id:
            return None
        located = find_generation_run(paths, generation_id, entry_id)
        if located is None:
            return {
                "run_id": None,
                "generation_id": generation_id,
                "transcript": None,
                "result": None,
            }
        run_id, events_path = located
        transcript: Any = None
        if reconstruct is not None:
            try:
                transcript = reconstruct(events_path, partial_ok=True).to_dict()
            except Exception as exc:  # noqa: BLE001 — best-effort, never raises
                transcript = {"error": f"transcript failed: {exc}"}
        # Surface a small projection of the sibling ``loss.json`` so the
        # frontend can render an honest "timed out" panel for a run that
        # produced no transcript turns. Without this the dashboard's
        # zero-turn complete-run path falls back to "This run produced
        # no transcript turns" — accurate but useless to the operator.
        run_result = read_run_result(events_path.parent)
        return {
            "run_id": run_id,
            "generation_id": generation_id,
            "transcript": transcript,
            "result": run_result,
        }

    champion_gen = _resolve_generation_id("parent", tournament_parent_gen)
    challenger_gen = _resolve_generation_id("child", tournament_child_gen)
    result["champion"] = _side("parent", champion_gen)
    result["challenger"] = _side("child", challenger_gen)
    return result
