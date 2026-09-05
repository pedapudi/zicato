"""One candidate's dossier: the per-candidate reads the console's candidate page joins.

The candidate page shows one generation: its per-board results against the
champion's, the gate that decided its round and the gates it defended, the
proposer's prediction scorecard, the proposal episode, an optional board
drill-down, and, on a racing epoch, the settled racing field. Each of those
is served by a reader of its own, and the page used to fetch ten to fifteen
routes and join them in the browser. :func:`build_candidate_dossier` performs
that join here, calling the same readers the granular routes call, so every
verdict on the page (the gate's ``decision`` and ``deciding_rule``, the
grid's ``verdict`` / ``won_by`` / ``decided_by``, the round-level
``decision`` on a match-up) is the one those readers serve and is never
recomputed from the payload.

The reader is best-effort like the rest of the layer: an unknown generation
answers a same-shaped payload with ``found: False`` rather than raising.
"""

from __future__ import annotations

from typing import Any

from zicato.query.epoch_view import (
    _current_champion,
    _read_epoch_experiments,
    _tournament_block_from_scoring,
)
from zicato.query.gate_view import build_gate_breakdown
from zicato.query.hypothesis_view import build_hypothesis_accuracy
from zicato.query.judge_view import (
    build_expectation_outcomes_for_run,
    build_per_entry_for_generation,
    build_per_judge_comparison,
    build_per_judge_for_entry,
    build_run_header,
)
from zicato.query.paths import WorkspacePaths, _read_json_value, coerce_float, layout_of
from zicato.query.promoted_head import read_recorded_heads, recorded_head_ids
from zicato.query.racing_view import build_racing_field
from zicato.query.runtime_view import read_active_tournament_dict
from zicato.query.tournament_view import build_bracket, build_matchup_grid
from zicato.query.transcript_view import build_proposal_episode_export

#: The two parts a candidate can play in a gate: the challenger of the round
#: that decided it, or the champion of a round it defended.
ROLE_CHALLENGER = "challenger"
ROLE_CHAMPION = "champion"


def _empty_dossier(epoch_id: str, generation_id: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "found": False,
        "champion": None,
        "parent": None,
        "structure": "gauntlet",
        "per_entry": None,
        "hypothesis_accuracy": None,
        "episode_export": None,
        "matchup_grid": None,
        "comparison": None,
        "gates": [],
        "drilldown": None,
        "racing_field": None,
    }


def _comparison(grid: dict[str, Any] | None, per_entry: dict[str, Any]) -> dict[str, Any]:
    """The per-board champion comparison the lifecycle figure paints.

    One row per entry of the matchup grid, projected to the fields the figure
    reads, plus the drift sums over the entries both sides ran (so the two
    sums cover the same boards) and whether the drift channel carries
    information for this pair. A candidate with no grid (the seed, or the
    reigning champion) sums its own drift losses and answers the drift
    question from its own per-entry read.
    """
    entries: dict[str, dict[str, Any]] = {}
    champion_sigma: float | None = None
    candidate_sigma: float | None = None
    drift_present: bool | None = None
    rows = grid.get("entry_grid") if isinstance(grid, dict) else None
    if isinstance(grid, dict):
        drift_present = grid.get("drift_present") is not False
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("entry_id") is None:
            continue
        parent_drift = coerce_float(row.get("parent_drift_loss"))
        child_drift = coerce_float(row.get("child_drift_loss"))
        replicates = row.get("score_replicates")
        entries[str(row["entry_id"])] = {
            "delta_score": coerce_float(row.get("delta_score")),
            "champion_score": coerce_float(row.get("parent_score")),
            "candidate_score": coerce_float(row.get("child_score")),
            "score_se": coerce_float(row.get("score_se")),
            "score_replicates": replicates if isinstance(replicates, int) else 0,
            "champion_drift_loss": parent_drift,
            "decided_by": row.get("decided_by"),
        }
        if parent_drift is not None and child_drift is not None:
            champion_sigma = (champion_sigma or 0.0) + parent_drift
            candidate_sigma = (candidate_sigma or 0.0) + child_drift
    if candidate_sigma is None:
        for entry in per_entry.get("entries", []):
            drift = coerce_float(entry.get("drift_loss")) if isinstance(entry, dict) else None
            if drift is not None:
                candidate_sigma = (candidate_sigma or 0.0) + drift
    if drift_present is None:
        drift_present = per_entry.get("drift_present") is not False
    delta_sigma = (
        candidate_sigma - champion_sigma
        if candidate_sigma is not None and champion_sigma is not None
        else None
    )
    return {
        "entries": entries,
        "champion_sigma": champion_sigma,
        "candidate_sigma": candidate_sigma,
        "delta_sigma": delta_sigma,
        "drift_present": drift_present,
    }


def _live_pairs(paths: WorkspacePaths, epoch_id: str) -> list[tuple[str, str]]:
    """The (champion, challenger) pairs of the running tournament, if this epoch's.

    A match names its champion first and its challenger last, the reading the
    live match-up list gives the console; a round a candidate is running
    right now therefore reaches its gate list before any record settles.
    """
    active = read_active_tournament_dict(paths)
    if not isinstance(active, dict) or active.get("epoch_id") not in (None, epoch_id):
        return []
    pairs: list[tuple[str, str]] = []
    for round_ in active.get("rounds") or []:
        for match in (round_.get("matches") or []) if isinstance(round_, dict) else []:
            competitors = match.get("competitors") if isinstance(match, dict) else None
            ids = (
                [str(c) for c in competitors if c is not None]
                if isinstance(competitors, list)
                else []
            )
            if len(ids) >= 2:
                pairs.append((ids[0], ids[-1]))
    return pairs


def _gates(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, parent: str | None
) -> list[dict[str, Any]]:
    """Every gate the candidate stood at: its own round, then the rounds it defended.

    Each carries the gate breakdown and the per-judge comparison for the pair,
    read from the readers that serve the granular routes. Defended rounds
    come from the settled match-ups and from the running tournament.
    """
    specs: list[tuple[str, str, str]] = []
    if parent is not None:
        specs.append((parent, generation_id, ROLE_CHALLENGER))
    settled = build_bracket(paths, epoch_id).get("matchups")
    for matchup in settled if isinstance(settled, list) else []:
        if isinstance(matchup, dict) and matchup.get("champion") == generation_id:
            challenger = matchup.get("challenger")
            if isinstance(challenger, str) and challenger:
                specs.append((generation_id, challenger, ROLE_CHAMPION))
    for champion, challenger in _live_pairs(paths, epoch_id):
        if champion == generation_id and challenger:
            specs.append((generation_id, challenger, ROLE_CHAMPION))
    seen: set[tuple[str, str]] = set()
    gates: list[dict[str, Any]] = []
    for champion, challenger, role in specs:
        if (champion, challenger) in seen:
            continue
        seen.add((champion, challenger))
        gates.append(
            {
                "champion": champion,
                "challenger": challenger,
                "role": role,
                "gate": build_gate_breakdown(paths, epoch_id, champion, challenger),
                "judge_comparison": build_per_judge_comparison(
                    paths, epoch_id, champion, challenger
                ),
            }
        )
    return gates


def build_candidate_dossier(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry: str = ""
) -> dict[str, Any]:
    """``GET /api/epoch/{epoch_id}/candidate/{generation_id}[?entry=<id>]``.

    Returns::

        {
          "epoch_id", "generation_id", "found",
          "champion",             # the reigning champion's id, or null
          "parent",               # the candidate's parent, null for the seed
          "structure",            # the epoch's tournament structure
          "per_entry",            # build_per_entry_for_generation
          "hypothesis_accuracy",  # build_hypothesis_accuracy; null for the seed
          "episode_export",       # build_proposal_episode_export; null for the seed
          "matchup_grid",         # build_matchup_grid against the champion; null
                                  # for the seed and for the champion itself
          "comparison",           # the per-board champion comparison (above)
          "gates": [{champion, challenger, role, gate, judge_comparison}],
          "drilldown",            # {entry_id, expectations, judges, header} for
                                  # ?entry=, else null
          "racing_field",         # build_racing_field on a racing epoch, else null
        }

    ``entry`` names the board entry whose run the page drills into; the empty
    string asks for no drill-down.
    """
    layout = layout_of(paths)
    if not layout.epoch_dir(epoch_id).is_dir():
        return _empty_dossier(epoch_id, generation_id)
    experiments = _read_epoch_experiments(layout, epoch_id)
    record = next(
        (e for e in experiments if e.get("generation_id") == generation_id),
        None,
    )
    if record is None and not layout.generation_dir(epoch_id, generation_id).is_dir():
        return _empty_dossier(epoch_id, generation_id)
    raw_parent = record.get("parent_generation_id") if record else None
    parent = raw_parent if isinstance(raw_parent, str) and raw_parent else None
    champion = _current_champion(
        experiments, recorded_head_ids(read_recorded_heads(paths, epoch_id))
    )
    block = _tournament_block_from_scoring(_read_json_value(layout.scoring(epoch_id)))
    structure = str(block.get("structure") or "gauntlet") if isinstance(block, dict) else "gauntlet"

    per_entry = build_per_entry_for_generation(paths, epoch_id, generation_id)
    seed = parent is None
    grid = (
        build_matchup_grid(paths, epoch_id, champion, generation_id)
        if champion is not None and champion != generation_id and not seed
        else None
    )
    drilldown = None
    if entry:
        drilldown = {
            "entry_id": entry,
            "expectations": build_expectation_outcomes_for_run(
                paths, epoch_id, generation_id, entry
            ),
            "judges": build_per_judge_for_entry(paths, epoch_id, generation_id, entry),
            "header": build_run_header(paths, epoch_id, generation_id, entry),
        }
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "found": True,
        "champion": champion,
        "parent": parent,
        "structure": structure,
        "per_entry": per_entry,
        "hypothesis_accuracy": (
            None if seed else build_hypothesis_accuracy(paths, epoch_id, generation_id)
        ),
        "episode_export": (
            None if seed else build_proposal_episode_export(paths, epoch_id, generation_id)
        ),
        "matchup_grid": grid,
        "comparison": _comparison(grid, per_entry),
        "gates": _gates(paths, epoch_id, generation_id, parent),
        "drilldown": drilldown,
        "racing_field": build_racing_field(paths, epoch_id) if structure == "racing" else None,
    }


__all__ = ["ROLE_CHALLENGER", "ROLE_CHAMPION", "build_candidate_dossier"]
