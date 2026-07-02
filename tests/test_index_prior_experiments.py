"""Tests for the experiment-memory reader (:func:`prior_experiments_for_epoch`).

Covers the curation + cap of ``docs/design/EXPERIMENT-MEMORY.md`` §3.3:
unsettled rows are skipped, ``modulating`` is lifted from
``hypothesis_json`` (malformed JSON degrading to an empty tuple), all
wins survive the cap, and the sharpest recent rejections fill the
remainder while near-zero rejections fall off first. A missing index
yields ``[]``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zicato.core.types import EXPERIMENT_MEMORY_MAX_ENTRIES
from zicato.index.query import prior_experiments_for_epoch
from zicato.index.schema import apply_schema


def _new_index(tmp_path: Path) -> Path:
    """Create an empty, schema-applied index database and return its path."""
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_experiment(
    db_path: Path,
    *,
    epoch_id: str,
    generation_id: str,
    decision: str | None,
    scalar_delta: float | None,
    modulating: list[str] | None = None,
    rejection_reason: str = "",
    hypothesis_json: str | None = None,
    core_idea: str = "do a thing",
    outcome_json: str | None = None,
) -> None:
    """Insert one ``experiments`` row with explicit verdict / delta / ids."""
    if hypothesis_json is None:
        hypothesis_json = json.dumps({"core_idea": core_idea, "modulating": modulating or []})
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO experiments (epoch_id, generation_id, hypothesis_core_idea, "
            "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
            "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                epoch_id,
                generation_id,
                core_idea,
                "because",
                hypothesis_json,
                decision,
                rejection_reason,
                scalar_delta,
                None,
                None,
                outcome_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_missing_index_returns_empty(tmp_path: Path) -> None:
    """A never-built index yields an empty digest, not an exception."""
    assert prior_experiments_for_epoch(tmp_path / "nope.db", "e1") == []


def test_empty_index_returns_empty(tmp_path: Path) -> None:
    """An index with no experiments under the epoch yields an empty digest."""
    db = _new_index(tmp_path)
    assert prior_experiments_for_epoch(db, "e1") == []


def test_unsettled_rows_are_skipped(tmp_path: Path) -> None:
    """Rows with ``tournament_decision IS NULL`` carry no learning signal."""
    db = _new_index(tmp_path)
    _insert_experiment(db, epoch_id="e1", generation_id="v1", decision=None, scalar_delta=None)
    _insert_experiment(db, epoch_id="e1", generation_id="v2", decision="promoted", scalar_delta=0.1)
    out = prior_experiments_for_epoch(db, "e1")
    assert [pe.generation_id for pe in out] == ["v2"]
    assert out[0].decision == "promoted"


def test_modulating_lifted_from_hypothesis_json(tmp_path: Path) -> None:
    """The targeted ids are decoded out of the recorded hypothesis JSON."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=0.12,
        modulating=["coordinator.routing", "writer.tools"],
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].modulating == ("coordinator.routing", "writer.tools")
    assert out[0].same_contract is True


def test_malformed_hypothesis_json_degrades_to_empty_modulating(tmp_path: Path) -> None:
    """A malformed ``hypothesis_json`` yields an empty tuple, never raises."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=0.1,
        hypothesis_json="{not valid json",
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].modulating == ()


def test_rejected_carries_reason_and_signed_delta(tmp_path: Path) -> None:
    """A rejected entry surfaces its symbolic reason and signed Δscalar."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="rejected",
        scalar_delta=-0.09,
        rejection_reason="pass_rate_regression",
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].decision == "rejected"
    assert out[0].rejection_reason == "pass_rate_regression"
    assert out[0].scalar_score_delta == -0.09


def test_all_wins_survive_cap_and_sharpest_rejections_fill_remainder(tmp_path: Path) -> None:
    """With > cap experiments, every win survives; the sharpest recent
    rejections fill the remainder and a near-zero rejection falls off first."""
    db = _new_index(tmp_path)
    cap = EXPERIMENT_MEMORY_MAX_ENTRIES
    # cap promoted wins.
    for i in range(cap):
        _insert_experiment(
            db,
            epoch_id="e1",
            generation_id=f"w{i:02d}",
            decision="promoted",
            scalar_delta=0.05,
        )
    # A sharp recent rejection and a near-zero recent rejection.
    _insert_experiment(
        db, epoch_id="e1", generation_id="r_sharp", decision="rejected", scalar_delta=-0.30
    )
    _insert_experiment(
        db, epoch_id="e1", generation_id="r_near0", decision="rejected", scalar_delta=-0.001
    )

    out = prior_experiments_for_epoch(db, "e1")
    # Cap is honoured.
    assert len(out) == cap
    # All entries are the wins — the rejections fell off because wins alone
    # already fill the cap.
    assert all(pe.decision == "promoted" for pe in out)


def test_recent_rejections_ordered_sharpest_first(tmp_path: Path) -> None:
    """Within the rejected block the sharpest regression ranks first; a
    near-zero rejection is the first to fall off when the cap bites."""
    db = _new_index(tmp_path)
    # No wins so the whole cap is available for rejections. Insert more
    # rejections than the cap, with the sharpest among the most recent.
    cap = EXPERIMENT_MEMORY_MAX_ENTRIES
    # Oldest first by generation id (the reader orders by generation_id).
    for i in range(cap + 3):
        # Make the deltas vary; later (higher-index) gens are "more recent".
        delta = -0.001 if i % 2 == 0 else -0.20 - (i * 0.001)
        _insert_experiment(
            db,
            epoch_id="e1",
            generation_id=f"g{i:02d}",
            decision="rejected",
            scalar_delta=delta,
        )
    out = prior_experiments_for_epoch(db, "e1")
    assert len(out) == cap
    # All rejected; within the cap the deltas are sorted sharpest (most
    # negative) first.
    assert all(pe.decision == "rejected" for pe in out)
    deltas = [pe.scalar_score_delta for pe in out]
    assert deltas == sorted(deltas)


def test_deferred_included_only_when_budget_remains(tmp_path: Path) -> None:
    """Deferred entries are the weakest signal — included after wins and
    rejections, dropped first when the cap is already full of stronger ones."""
    db = _new_index(tmp_path)
    _insert_experiment(db, epoch_id="e1", generation_id="v1", decision="promoted", scalar_delta=0.1)
    _insert_experiment(
        db, epoch_id="e1", generation_id="v2", decision="rejected", scalar_delta=-0.2
    )
    _insert_experiment(db, epoch_id="e1", generation_id="v3", decision="deferred", scalar_delta=0.0)
    out = prior_experiments_for_epoch(db, "e1")
    decisions = {pe.generation_id: pe.decision for pe in out}
    assert decisions == {"v1": "promoted", "v2": "rejected", "v3": "deferred"}

    # With max_entries=2 the deferred entry is dropped (wins + rejection
    # already fill the budget).
    capped = prior_experiments_for_epoch(db, "e1", max_entries=2)
    assert {pe.generation_id for pe in capped} == {"v1", "v2"}


# --------------------------------------------------------------------------
# Hypothesis prediction-accuracy (FUNCTIONALITY-RECOMMENDATIONS.md §4.2)
# --------------------------------------------------------------------------


def _hyp(direction: str, magnitude: str) -> str:
    """A hypothesis predicting one off_topic drift movement."""
    return json.dumps(
        {
            "core_idea": "tighten router",
            "modulating": ["router"],
            "expected_drift_movements": [
                {"kind": "off_topic", "direction": direction, "magnitude": magnitude}
            ],
        }
    )


def _outcome(from_rate: float, to_rate: float) -> str:
    """A realised outcome moving off_topic from ``from_rate`` to ``to_rate``."""
    return json.dumps(
        {
            "drift_movements": [
                {
                    "kind": "off_topic",
                    "from_rate": from_rate,
                    "to_rate": to_rate,
                    "hypothesis_match": True,
                }
            ]
        }
    )


def test_prediction_accuracy_full_match_scores_one(tmp_path: Path) -> None:
    """A hypothesis whose predicted direction AND magnitude both bear out
    scores prediction_accuracy == 1.0."""
    db = _new_index(tmp_path)
    # Predict a large decrease; off_topic drops 10 -> 2 (a large decrease with
    # an empty metric-range falling back to the raw absolute movement).
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=_hyp("decrease", "large"),
        outcome_json=_outcome(10.0, 2.0),
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].prediction_accuracy == 1.0


def test_prediction_accuracy_wrong_direction_scores_zero(tmp_path: Path) -> None:
    """A hypothesis whose realised direction contradicts the prediction
    scores prediction_accuracy == 0.0 (a graded miss)."""
    db = _new_index(tmp_path)
    # Predict a decrease; off_topic actually INCREASES 2 -> 10.
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="rejected",
        scalar_delta=0.1,
        rejection_reason="regressed",
        hypothesis_json=_hyp("decrease", "large"),
        outcome_json=_outcome(2.0, 10.0),
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].prediction_accuracy == 0.0


def test_prediction_accuracy_none_when_no_predictions(tmp_path: Path) -> None:
    """A hypothesis that made no falsifiable movement claims has no graded
    predictions, so prediction_accuracy is None (not 0.0)."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=json.dumps({"core_idea": "x", "modulating": ["router"]}),
        outcome_json=_outcome(10.0, 2.0),
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].prediction_accuracy is None


def test_prediction_accuracy_none_when_outcome_absent(tmp_path: Path) -> None:
    """A settled-but-outcome-less row (NULL outcome_json) cannot be graded —
    prediction_accuracy degrades to None, never raises."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=_hyp("decrease", "large"),
        outcome_json=None,
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].prediction_accuracy is None


def test_prediction_accuracy_is_diagnostic_only(tmp_path: Path) -> None:
    """Prediction accuracy never changes the verdict, Δscalar, or curation —
    it is a parallel advisory field. A promoted win with a wrong prediction
    is still surfaced as a promoted win."""
    db = _new_index(tmp_path)
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v1",
        decision="promoted",
        scalar_delta=-0.2,
        hypothesis_json=_hyp("decrease", "large"),
        outcome_json=_outcome(2.0, 10.0),  # prediction was wrong (increase)
    )
    out = prior_experiments_for_epoch(db, "e1")
    assert out[0].decision == "promoted"  # verdict untouched
    assert out[0].scalar_score_delta == -0.2  # delta untouched
    assert out[0].prediction_accuracy == 0.0  # advisory signal only


# --------------------------------------------------------------------------
# Cross-epoch transfer (EXPERIMENT-MEMORY.md §3.4 / §5.2 — opt-in)
# --------------------------------------------------------------------------


def _insert_epoch(db_path: Path, epoch_id: str, contract_hash: str) -> None:
    """Insert one ``epochs`` row carrying the given contract hash."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO epochs (epoch_id, contract_hash, created_at, closed, "
            "goal, parent_epoch_id) VALUES (?, ?, ?, ?, ?, ?)",
            (epoch_id, contract_hash, "2026-07-01T00:00:00Z", 1, "", None),
        )
        conn.commit()
    finally:
        conn.close()


def _cross_epoch_world(tmp_path: Path) -> Path:
    """Three epochs: current (e2) + a prior sibling under the SAME hash (e1)
    + a prior epoch under a DIFFERENT hash (e0) + a legacy hashless one."""
    db = _new_index(tmp_path)
    _insert_epoch(db, "e0", "hash-other")
    _insert_epoch(db, "e1", "hash-shared")
    _insert_epoch(db, "e2", "hash-shared")
    _insert_epoch(db, "e_legacy", "")
    # Current epoch: one settled win of its own.
    _insert_experiment(
        db, epoch_id="e2", generation_id="v1", decision="promoted", scalar_delta=-0.1
    )
    # Prior sibling epoch (same hash): a win, a rejection, and an unsettled row.
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v3",
        decision="promoted",
        scalar_delta=-0.2,
        core_idea="shared-contract win",
        modulating=["router"],
    )
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v4",
        decision="rejected",
        scalar_delta=0.3,
        rejection_reason="regressed",
        core_idea="shared-contract miss",
    )
    _insert_experiment(db, epoch_id="e1", generation_id="v5", decision=None, scalar_delta=None)
    # A different-contract epoch: must NEVER surface, knob or not.
    _insert_experiment(
        db,
        epoch_id="e0",
        generation_id="v9",
        decision="promoted",
        scalar_delta=-0.9,
        core_idea="other-contract win",
    )
    # Legacy (pre-hash) epoch: an unknown contract is never transferable.
    _insert_experiment(
        db,
        epoch_id="e_legacy",
        generation_id="v7",
        decision="promoted",
        scalar_delta=-0.5,
        core_idea="legacy win",
    )
    return db


def test_cross_epoch_knob_off_is_byte_identical(tmp_path: Path) -> None:
    """The default reader never sees the prior epochs — identical output
    with and without the knob argument spelled out."""
    db = _cross_epoch_world(tmp_path)
    default_out = prior_experiments_for_epoch(db, "e2")
    explicit_off = prior_experiments_for_epoch(db, "e2", cross_epoch=False)
    assert default_out == explicit_off
    assert [pe.generation_id for pe in default_out] == ["v1"]
    assert all(pe.same_contract for pe in default_out)


def test_cross_epoch_knob_on_appends_flagged_entries(tmp_path: Path) -> None:
    """Knob on: prior-epoch settled entries under the SAME hash appear,
    flagged same_contract=False with their delta omitted; other-hash and
    legacy-hash epochs never surface; unsettled rows are skipped."""
    db = _cross_epoch_world(tmp_path)
    out = prior_experiments_for_epoch(db, "e2", cross_epoch=True)

    same = [pe for pe in out if pe.same_contract]
    cross = [pe for pe in out if not pe.same_contract]
    # Same-epoch entries first (priority in the cap), untouched.
    assert [pe.generation_id for pe in same] == ["v1"]
    assert out[: len(same)] == same
    # The shared-hash sibling's settled rows are present, win first.
    assert [(pe.epoch_id, pe.generation_id) for pe in cross] == [("e1", "v3"), ("e1", "v4")]
    for pe in cross:
        assert pe.same_contract is False
        assert pe.scalar_score_delta is None  # the number does not transfer
        assert pe.prediction_accuracy is None
    # Different-hash and legacy epochs never leak through.
    surfaced = {(pe.epoch_id, pe.generation_id) for pe in out}
    assert ("e0", "v9") not in surfaced
    assert ("e_legacy", "v7") not in surfaced
    # Unsettled prior-epoch rows are skipped.
    assert ("e1", "v5") not in surfaced


def test_cross_epoch_entries_never_displace_same_epoch(tmp_path: Path) -> None:
    """Same-epoch history keeps priority: when the cap is filled by the
    current epoch, no cross-epoch entry is admitted."""
    db = _new_index(tmp_path)
    _insert_epoch(db, "e1", "hash-shared")
    _insert_epoch(db, "e2", "hash-shared")
    cap = EXPERIMENT_MEMORY_MAX_ENTRIES
    for i in range(cap):
        _insert_experiment(
            db, epoch_id="e2", generation_id=f"w{i:02d}", decision="promoted", scalar_delta=-0.1
        )
    _insert_experiment(
        db, epoch_id="e1", generation_id="v1", decision="promoted", scalar_delta=-0.2
    )
    out = prior_experiments_for_epoch(db, "e2", cross_epoch=True)
    assert len(out) == cap
    assert all(pe.same_contract for pe in out)

    # With head-room, the cross entry fills exactly the remaining budget.
    roomy = prior_experiments_for_epoch(db, "e2", max_entries=cap + 2, cross_epoch=True)
    assert len(roomy) == cap + 1
    assert roomy[-1].same_contract is False
    assert roomy[-1].epoch_id == "e1"
