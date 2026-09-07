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

from zicato.epoch._storage import RecordError
from zicato.epoch.lineage import Lineage, load_lineage
from zicato.epoch.settlement_receipt import read_settlement_receipt
from zicato.query.epoch_view import (
    _current_champion,
    _read_epoch_experiments,
    _tournament_block_from_scoring,
)
from zicato.query.gate_view import build_gate_breakdown
from zicato.query.hypothesis_view import build_hypothesis_accuracy
from zicato.query.inputs import EpochInputs
from zicato.query.judge_view import (
    build_expectation_outcomes_for_run,
    build_per_entry_for_generation,
    build_per_judge_comparison,
    build_per_judge_for_entry,
    build_per_judge_for_generation,
    build_run_header,
)
from zicato.query.lineage_view import build_lineage_view
from zicato.query.paths import WorkspacePaths, coerce_float, layout_of
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
        "generation": None,
        "experiment": None,
        "relatives": [],
        "lineage_note": None,
        "per_judge": None,
        "champion": None,
        "parent": None,
        "parent_epoch_id": None,
        "parent_inconsistency": None,
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
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    parent: str | None,
    inputs: EpochInputs,
) -> list[dict[str, Any]]:
    """Every gate the candidate stood at: its own round, then the rounds it defended.

    Each carries the gate breakdown and the per-judge comparison for the pair,
    read from the readers that serve the granular routes. Defended rounds
    come from the settled match-ups and from the running tournament.
    """
    specs: list[tuple[str, str, str]] = []
    if parent is not None:
        specs.append((parent, generation_id, ROLE_CHALLENGER))
    settled = build_bracket(paths, epoch_id, inputs=inputs).get("matchups")
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
                "gate": build_gate_breakdown(paths, epoch_id, champion, challenger, inputs=inputs),
                "judge_comparison": build_per_judge_comparison(
                    paths, epoch_id, champion, challenger
                ),
            }
        )
    return gates


def _relatives(
    nodes: dict[str, dict[str, Any]], epoch_id: str, generation_id: str
) -> list[dict[str, Any]]:
    """The recorded parent and immediate children, with explicit epoch identity."""
    relatives = []
    parent = nodes.get(generation_id, {}).get("parent_generation_id")
    if isinstance(parent, str) and parent:
        parent_epoch, parent_id = parent.split(":", 1) if ":" in parent else (epoch_id, parent)
        parent_record = nodes.get(parent_id, {}) if parent_epoch == epoch_id else {}
        relatives.append(
            {
                **parent_record,
                "relationship": "parent",
                "epoch_id": parent_epoch,
                "generation_id": parent_id,
            }
        )
    relatives.extend(
        {**node, "relationship": "child"}
        for node in nodes.values()
        if node.get("parent_generation_id") in {generation_id, f"{epoch_id}:{generation_id}"}
    )
    return relatives


def _parent_coordinate(value: Any, epoch_id: str, source: str) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise RecordError(f"{source}: invalid parent coordinate {value!r}")
    parts = value.split(":")
    if len(parts) == 1 and value not in {".", ".."}:
        return epoch_id, value
    if len(parts) == 2 and all(part and part not in {".", ".."} for part in parts):
        return parts[0], parts[1]
    raise RecordError(f"{source}: invalid parent coordinate {value!r}")


def _accepted_parent(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, inputs: EpochInputs, lineage: Lineage
) -> tuple[str, str] | None:
    """Compare recorded parent claims using complete epoch/generation coordinates."""
    claims: dict[str, tuple[str, str] | None] = {}
    epoch = lineage.epoch(epoch_id)
    generation = epoch.generation(generation_id) if epoch is not None else None
    if generation is not None and "parent_id" in generation.to_dict():
        claims["lineage"] = _parent_coordinate(generation.parent_id, epoch_id, "lineage")
    if generation_id == "v0" and epoch is not None and epoch.v0_parent and ":" in epoch.v0_parent:
        claims["lineage seed"] = _parent_coordinate(epoch.v0_parent, epoch_id, "lineage seed")
    captured = inputs.generations.get(generation_id)
    if captured is not None and captured.unreadable:
        raise RecordError(captured.unreadable)
    body = inputs.experiment(generation_id)
    if body is not None and "parent_generation_id" in body:
        claims["experiment"] = _parent_coordinate(
            body["parent_generation_id"], epoch_id, "experiment"
        )
    # A baseline experiment records no parent within its own contract. Its
    # lineage may independently retain the external source of the seed.
    if generation_id == "v0" and claims.get("experiment") is None:
        if any(parent is not None and parent[0] != epoch_id for parent in claims.values()):
            claims.pop("experiment", None)
    raw_round = body.get("round_index") if body is not None else None
    if raw_round is None and generation is not None:
        raw_round = generation.round_index
    if generation_id != "v0" and raw_round is not None:
        if isinstance(raw_round, bool) or not isinstance(raw_round, int) or raw_round < 0:
            raise RecordError("experiment: invalid round_index prevents reading its settlement")
        receipt = read_settlement_receipt(paths.root, epoch_id, raw_round)
        if receipt is not None:
            if not any(
                candidate.generation_id == generation_id for candidate in receipt.candidates
            ):
                raise RecordError(
                    f"settlement round {raw_round} does not include {epoch_id}:{generation_id}"
                )
            for candidate in receipt.candidates:
                sibling = inputs.generations.get(candidate.generation_id)
                if sibling is None or sibling.unreadable:
                    raise RecordError(
                        f"settlement experiment {candidate.generation_id} is unavailable"
                    )
                experiment = sibling.body.copy()
                if (
                    not isinstance(experiment, dict)
                    or experiment.get("id") != candidate.experiment_id
                ):
                    raise RecordError(
                        f"settlement experiment {candidate.generation_id} has conflicting identity"
                    )
                source = f"settlement experiment {candidate.generation_id}"
                if "parent_generation_id" not in experiment:
                    raise RecordError(f"{source}: parent is missing")
                claims[source] = _parent_coordinate(
                    experiment["parent_generation_id"], epoch_id, source
                )
            field = receipt.field_record
            if field is not None:
                claims["settlement incumbent"] = _parent_coordinate(
                    field["champion_generation_id"], receipt.epoch_id, "settlement incumbent"
                )
    if len(set(claims.values())) > 1:
        details = "; ".join(
            f"{source} declares {parent[0] + ':' + parent[1] if parent else 'no parent'}"
            for source, parent in claims.items()
        )
        raise RecordError(f"parent identity conflicts for {epoch_id}:{generation_id}: {details}")
    return next(iter(claims.values()), None)


def build_candidate_dossier(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry: str = ""
) -> dict[str, Any]:
    """``GET /api/epoch/{epoch_id}/candidate/{generation_id}[?entry=<id>]``.

    Returns::

        {
          "epoch_id", "generation_id", "found",
          "generation",           # epoch-scoped identity, decision and rating
          "experiment",           # hypothesis, referenced patches and outcome
          "relatives",            # recorded parent and immediate children
          "lineage_note",         # an unreadable lineage explanation, or null
          "per_judge",            # the candidate's recorded judge losses
          "champion",             # the reigning champion's id, or null
          "parent",               # the accepted local or epoch:generation coordinate
          "parent_epoch_id",      # the parent's epoch, or null
          "parent_inconsistency", # conflicting/unreadable parent evidence, or null
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
    inputs = EpochInputs.capture(paths, epoch_id)
    lineage: dict[str, Any]
    try:
        lineage_record = load_lineage(paths.root)
    except RecordError as exc:
        lineage_record = None
        lineage = {"generations": [], "unreadable": str(exc)}
    else:
        lineage = build_lineage_view(paths, epoch_id, inputs=inputs, lineage_record=lineage_record)
    nodes = {node["generation_id"]: node for node in lineage.get("generations", [])}
    experiments = _read_epoch_experiments(layout, epoch_id, lineage=nodes, inputs=inputs)
    record = next(
        (e for e in experiments if e.get("generation_id") == generation_id),
        None,
    )
    if record is None and not layout.generation_dir(epoch_id, generation_id).is_dir():
        return _empty_dossier(epoch_id, generation_id)
    parent_inconsistency = None
    coordinate = None
    try:
        if lineage_record is None:
            raise RecordError(str(lineage["unreadable"]))
        coordinate = _accepted_parent(paths, epoch_id, generation_id, inputs, lineage_record)
    except RecordError as exc:
        parent_inconsistency = str(exc)
    parent_epoch_id = coordinate[0] if coordinate is not None else None
    local_parent = coordinate[1] if coordinate is not None and coordinate[0] == epoch_id else None
    parent = (
        local_parent
        if local_parent is not None
        else ":".join(coordinate)
        if coordinate is not None
        else None
    )
    champion = _current_champion(
        experiments, recorded_head_ids(read_recorded_heads(paths, epoch_id))
    )
    block = _tournament_block_from_scoring(inputs.scoring.copy())
    structure = str(block.get("structure") or "gauntlet") if isinstance(block, dict) else "gauntlet"

    per_entry = build_per_entry_for_generation(paths, epoch_id, generation_id, inputs=inputs)
    seed = local_parent is None
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
        "generation": nodes.get(generation_id),
        "experiment": record,
        "relatives": _relatives(nodes, epoch_id, generation_id),
        "lineage_note": lineage.get("unreadable"),
        "per_judge": build_per_judge_for_generation(paths, epoch_id, generation_id),
        "champion": champion,
        "parent": parent,
        "parent_epoch_id": parent_epoch_id,
        "parent_inconsistency": parent_inconsistency,
        "structure": structure,
        "per_entry": per_entry,
        "hypothesis_accuracy": (
            None
            if seed
            else build_hypothesis_accuracy(paths, epoch_id, generation_id, inputs=inputs)
        ),
        "episode_export": (
            None
            if generation_id == "v0"
            else build_proposal_episode_export(paths, epoch_id, generation_id)
        ),
        "matchup_grid": grid,
        "comparison": None if parent_inconsistency else _comparison(grid, per_entry),
        "gates": []
        if parent_inconsistency
        else _gates(paths, epoch_id, generation_id, local_parent, inputs),
        "drilldown": drilldown,
        "racing_field": build_racing_field(paths, epoch_id, inputs=inputs)
        if structure == "racing"
        else None,
    }


__all__ = ["ROLE_CHALLENGER", "ROLE_CHAMPION", "build_candidate_dossier"]
