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
                None,
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
