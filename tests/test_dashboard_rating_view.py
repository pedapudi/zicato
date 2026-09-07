"""Recorded independent confirmation is the only source of confidence views.

Selection matchups cannot reconstruct uncertainty. Disabled requirements remain
absent, and authoritative independent confirmation records retain their history.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._workspace_support import experiment_record
from zicato.core import TournamentDecision
from zicato.epoch.journal import write_experiment
from zicato.query import WorkspacePaths, build_gate_breakdown
from zicato.query.gate_view import build_rating_view
from zicato.query.inputs import EpochInputs
from zicato.selection.dead_letter import InconclusiveRecord, record_inconclusive
from zicato.selection.evidence_gate import EvidenceVerdict, rating_block
from zicato.selection.strategy import SelectionDecision
from zicato.testing.fixtures import make_experiment, make_outcome_record
from zicato.tournament.records import field_tournament_record, write_field_tournament_record

EPOCH_ID = "2026-06-10_e0"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _scoring(threshold: float | None) -> dict[str, object]:
    params: dict[str, object] = {"field_size": 4}
    if threshold is not None:
        params["promote_confidence_threshold"] = threshold
    return {
        "promote_margin": 0.01,
        "tournament": {"structure": "swiss", "params": params},
    }


def _write_durable_record(ws: Path, matches: list[dict[str, object]]) -> None:
    record = field_tournament_record(
        field_tournament_id=f"{EPOCH_ID}:field:v1",
        epoch_id=EPOCH_ID,
        structure="swiss",
        structure_params={},
        competitors=[
            {"generation_id": "v0", "seed": 1, "role": "champion"},
            {"generation_id": "v1", "seed": 2, "role": "challenger"},
            {"generation_id": "v2", "seed": 3, "role": "challenger"},
        ],
        rounds=[{"stage_index": 0, "label": "Swiss round 1", "matches": matches}],
        standings=[],
        field_status=[],
        decision=SelectionDecision("v1", TournamentDecision.PROMOTED, ""),
        ran_at="2026-06-10T00:00:00Z",
    )
    assert record is not None
    write_field_tournament_record(ws, epoch_id=EPOCH_ID, first_challenger_id="v1", record=record)


def _match(left: str, right: str, *, winner: str, delta: float) -> dict[str, object]:
    return {
        "match_id": f"{left}:{right}",
        "competitors": [left, right],
        "winner": winner,
        "decision": "promoted" if winner == right else "rejected",
        "delta_scalar": delta,
    }


def _workspace(tmp_path: Path, *, threshold: float | None) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    _write_json(ws / "epochs" / EPOCH_ID / "scoring.json", _scoring(threshold))
    return ws


# ---------------------------------------------------------------------------
# present=false on a disabled / pre-BT run
# ---------------------------------------------------------------------------


def test_rating_absent_without_threshold(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=None)
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block == {"present": False}


def test_rating_absent_when_no_challenger(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "")
    assert block == {"present": False}


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize(
    ("record_generation", "record_parent", "accepted"),
    [("v1", "v0", True), ("v2", "v0", False), ("v1", "v9", False)],
)
def test_confirmation_requires_requested_generation_and_parent(
    tmp_path: Path,
    captured: bool,
    record_generation: str,
    record_parent: str,
    accepted: bool,
) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    evidence = {
        "present": True,
        "credible": True,
        "evidence_basis": "independent_confirmation",
        "confirmation_status": "complete",
        "champion_id": "v0",
        "challenger_id": "v1",
    }
    _write_json(
        ws / "epochs" / EPOCH_ID / "generations" / "v1" / "experiment.json",
        experiment_record(
            record_generation,
            parent_generation_id=record_parent,
            outcome={"evidence": evidence},
        ),
    )
    paths = WorkspacePaths(ws)
    block = build_rating_view(
        paths,
        EPOCH_ID,
        "v0",
        "v1",
        inputs=EpochInputs.capture(paths, EPOCH_ID) if captured else None,
    )
    if accepted:
        assert block == {**evidence, "next_duel": None}
    else:
        assert block["present"] is False
        assert block["unreadable"]
        assert "credible" not in block


@pytest.mark.parametrize("source", ["experiment", "captured experiment", "inconclusive"])
@pytest.mark.parametrize(
    ("champion_id", "challenger_id"),
    [("v0", "v1"), ("v8", "v1"), ("v0", "v9"), (None, "v1"), ("v0", None)],
)
def test_confirmation_requires_recorded_pair_identity(
    tmp_path: Path, source: str, champion_id: str | None, challenger_id: str | None
) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    evidence = rating_block(
        EvidenceVerdict(
            decision="inconclusive",
            reason="recorded confirmation",
            credible=True,
            champion=None,
            challenger=None,
            p_stronger=0.9,
            threshold=0.975,
            ci_overlap=False,
            champion_id="v0",
            challenger_id="v1",
        )
    )
    for key, value in (("champion_id", champion_id), ("challenger_id", challenger_id)):
        if value is None:
            evidence.pop(key)
        else:
            evidence[key] = value
    history = [{"replicates_spent": 3}]
    if source == "inconclusive":
        record_inconclusive(
            ws, InconclusiveRecord("v1", "v0", EPOCH_ID, evidence, history, "unresolved")
        )
    else:
        write_experiment(
            ws,
            EPOCH_ID,
            "v1",
            make_experiment(
                epoch_id=EPOCH_ID,
                generation_id="v1",
                parent_generation_id="v0",
                outcome=make_outcome_record(evidence=evidence),
            ),
        )
    paths = WorkspacePaths(ws)
    block = build_rating_view(
        paths,
        EPOCH_ID,
        "v0",
        "v1",
        inputs=EpochInputs.capture(paths, EPOCH_ID) if source == "captured experiment" else None,
    )
    if (champion_id, challenger_id) == ("v0", "v1"):
        expected = {**evidence, "next_duel": None}
        if source == "inconclusive":
            expected["ci_history"] = history
        assert block == expected
    else:
        assert block == {
            "present": False,
            "unreadable": "recorded confirmation pair differs from requested contestants",
        }


# ---------------------------------------------------------------------------
# present=true, reconstructed fit from the durable matches
# ---------------------------------------------------------------------------


def test_selection_matchups_do_not_establish_confirmation_confidence(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    matches = [_match("v0", "v1", winner="v1", delta=-0.5) for _ in range(60)]
    _write_durable_record(ws, matches)
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["present"] is True
    assert block["confirmation_status"] == "incomplete"
    assert block["credible"] is False
    assert block["n_duels"] == 0
    assert block["threshold"] == 0.9
    for key in ("champion", "challenger", "difference", "p_stronger", "next_duel"):
        assert block[key] is None


# ---------------------------------------------------------------------------
# Dead-letter record is authoritative for an inconclusive duel
# ---------------------------------------------------------------------------


def test_rating_prefers_dead_letter_record(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    # A durable record that would re-fit one way...
    matches = [_match("v0", "v1", winner="v1", delta=-0.5) for _ in range(6)]
    _write_durable_record(ws, matches)
    # ...but a dead-letter record with an explicit inconclusive block wins.
    authoritative_rating = {
        "present": True,
        "evidence_basis": "independent_confirmation",
        "champion_id": "v0",
        "challenger_id": "v1",
        "credible": True,
        "champion": {"theta": -0.1, "se": 0.8, "ci_lo": -1.6, "ci_hi": 1.4},
        "challenger": {"theta": 0.1, "se": 0.8, "ci_lo": -1.4, "ci_hi": 1.6},
        "p_stronger": 0.57,
        "threshold": 0.9,
        "decision": "inconclusive",
        "ci_overlap": True,
        "replicates_spent": 3,
        "n_duels": 7,
    }
    record_inconclusive(
        ws,
        InconclusiveRecord(
            generation_id="v1",
            champion_id="v0",
            epoch_id=EPOCH_ID,
            rating=authoritative_rating,
            ci_history=[
                {"p_stronger": 0.55, "ci_overlap": True, "replicates_spent": 0},
                {"p_stronger": 0.57, "ci_overlap": True, "replicates_spent": 3},
            ],
            reason="inconclusive: rating CIs still overlap",
        ),
    )
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["present"] is True
    assert block["decision"] == "inconclusive"
    assert block["replicates_spent"] == 3
    assert block["next_duel"] is None  # terminal
    assert len(block["ci_history"]) == 2
    assert block["ci_history"][-1]["replicates_spent"] == 3


def test_rating_refuses_corrupt_inconclusive_record(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    _write_json(ws / "runtime" / "inconclusive" / "v1.json", None)
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["present"] is False
    assert "JSON object" in block["unreadable"]


def test_rating_ignores_other_epochs_inconclusive_record(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    record_inconclusive(
        ws,
        InconclusiveRecord(
            generation_id="v1",
            champion_id="v0",
            epoch_id="other-epoch",
            rating={"present": True, "decision": "inconclusive"},
            ci_history=[],
            reason="unresolved in a different epoch",
        ),
    )
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["decision"] == "deferred"


# ---------------------------------------------------------------------------
# Threaded onto the gate breakdown
# ---------------------------------------------------------------------------


def test_gate_breakdown_carries_rating_block(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=None)
    # gen_score aggregates so the breakdown's own rules can resolve.
    for gid, scalar in (("v0", 0.5), ("v1", 0.3)):
        _write_json(
            ws / "epochs" / EPOCH_ID / "generations" / gid / "gen_score.json",
            {
                "scalar": scalar,
                "pass_rate": 1.0,
                "per_entry": {"e1": {"drift_loss": scalar, "pass_fail": True}},
                "scalar_components": {"drift": scalar, "pass": 0.0},
            },
        )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert "rating" in result
    # No threshold configured → the block is absent.
    assert result["rating"] == {"present": False}
