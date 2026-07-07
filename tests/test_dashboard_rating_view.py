"""Tests for the Bradley--Terry ``gate.rating`` block reader.

``build_rating_view`` wires :mod:`zicato.selection.rating` into the gate
breakdown. These tests build a minimal ``.zicato/`` workspace and assert:

* ``present=false`` on a pre-BT / disabled run (no ``promote_confidence_threshold``
  in the structure params) — back-compat clean;
* ``present=true`` with a reconstructed BT fit from the durable field-tournament
  matches, gated on the minimum-duel credibility floor;
* the dead-letter record is the authoritative source for an inconclusive duel
  (its recorded block + ci_history win over a live re-fit);
* the block is threaded onto ``build_gate_breakdown`` under ``rating``.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.query import (
    WorkspacePaths,
    build_gate_breakdown,
    build_rating_view,
)
from zicato.selection.dead_letter import InconclusiveRecord, record_inconclusive

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


def _durable_record(matches: list[dict[str, object]]) -> dict[str, object]:
    return {
        "tournament_id": f"{EPOCH_ID}:field:v1",
        "structure": "swiss",
        "rounds": [{"stage_index": 0, "label": "Swiss round 1", "matches": matches}],
        "standings": [],
    }


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


# ---------------------------------------------------------------------------
# present=true, reconstructed fit from the durable matches
# ---------------------------------------------------------------------------


def test_rating_present_but_uncredible_below_floor(tmp_path: Path) -> None:
    # Two durable duels between v0/v1 — below MIN_CREDIBLE_DUELS=3 → present but
    # not credible.
    ws = _workspace(tmp_path, threshold=0.9)
    matches = [
        _match("v0", "v1", winner="v1", delta=-0.5),
        _match("v0", "v1", winner="v1", delta=-0.5),
    ]
    _write_json(
        ws / "epochs" / EPOCH_ID / "tournaments" / "field-v1.json",
        _durable_record(matches),
    )
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["present"] is True
    assert block["credible"] is False
    assert block["n_duels"] == 2
    assert block["threshold"] == 0.9
    # The fit still placed both sides.
    assert block["champion"] is not None
    assert block["challenger"] is not None


def test_rating_present_and_credible_from_durable(tmp_path: Path) -> None:
    # Enough durable duels for a credible fit; v1 wins them all.
    ws = _workspace(tmp_path, threshold=0.9)
    matches = [_match("v0", "v1", winner="v1", delta=-0.5) for _ in range(6)]
    _write_json(
        ws / "epochs" / EPOCH_ID / "tournaments" / "field-v1.json",
        _durable_record(matches),
    )
    block = build_rating_view(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert block["present"] is True
    assert block["credible"] is True
    assert block["n_duels"] == 6
    assert block["p_stronger"] is not None and block["p_stronger"] > 0.5
    assert set(block) == {
        "present",
        "credible",
        "champion",
        "challenger",
        "p_stronger",
        "threshold",
        "decision",
        "ci_overlap",
        "replicates_spent",
        "n_duels",
        "next_duel",
        "ci_history",
    }
    for side in ("champion", "challenger"):
        assert set(block[side]) == {"theta", "se", "ci_lo", "ci_hi"}
    # A still-overlapping near-tie surfaces the next duel to replicate.
    if block["ci_overlap"]:
        assert block["next_duel"] == {"left": "v0", "right": "v1"}


# ---------------------------------------------------------------------------
# Dead-letter record is authoritative for an inconclusive duel
# ---------------------------------------------------------------------------


def test_rating_prefers_dead_letter_record(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, threshold=0.9)
    # A durable record that would re-fit one way...
    matches = [_match("v0", "v1", winner="v1", delta=-0.5) for _ in range(6)]
    _write_json(
        ws / "epochs" / EPOCH_ID / "tournaments" / "field-v1.json",
        _durable_record(matches),
    )
    # ...but a dead-letter record with an explicit inconclusive block wins.
    authoritative_rating = {
        "present": True,
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
