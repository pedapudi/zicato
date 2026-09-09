"""Incomplete confirmation preserves the champion across canonical settlement."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests._orchestrator_harness import (
    evaluation_call_llm,
    install_stub_adapter_factory,
    run_evolve_once,
)
from tests.test_driver_evidence_pregate import _replicate_result
from tests.test_orchestrator_multi_challenger_holdout import (
    _bootstrap,
    _crowned_outcome,
    _field_bracket,
    _install_per_entry_telemetry_stubs,
    _lineage_promoted,
)
from zicato.core.types import LadderConfig, OverfittingConfig
from zicato.core.workspace import ladder_state_path
from zicato.evolve.generation_phase import current_generation
from zicato.query.gate_view import build_rating_view
from zicato.query.paths import WorkspacePaths
from zicato.selection import driver
from zicato.tournament.gate import GateOutcome


@pytest.mark.parametrize(
    "cause",
    [
        "tie",
        "duplicate",
        "zero_budget",
        "missing_runner",
        "runner_error",
        "withheld",
        "exhausted",
        "holdout_failure",
    ],
)
def test_required_confirmation_settles_without_advancing_lineage(
    cause: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    holdout_case = cause in {"withheld", "exhausted", "holdout_failure"}
    allowance = 0 if cause == "exhausted" else 2
    workspace, epoch_id = _bootstrap(
        tmp_path,
        structure="racing",
        field_size=2,
        overfitting=OverfittingConfig(
            ladder=LadderConfig(
                budget=allowance,
                threshold=3.0 if cause == "withheld" else 0.1,
            )
        ),
        confirmation_params=None
        if holdout_case
        else {
            "promote_confidence_threshold": 0.8,
            "promote_confidence_replicates": 0 if cause == "zero_budget" else 3,
        },
    )
    install_stub_adapter_factory(monkeypatch)
    losses = {
        (gid, entry): scalar
        for gid, scalar in (("v0", 2.0), ("v1", 0.5), ("v2", 1.5))
        for entry in (*(f"train_{i}" for i in range(4)), "h0")
    }
    # Both a withheld negative and a released negative use the same measurement.
    losses[("v1", "h0")] = 5.0
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=losses,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    calls = 0
    fixed = _replicate_result("v0", "v1", child_won=True)

    async def replicate(left: str, right: str):
        nonlocal calls
        calls += 1
        if cause == "runner_error":
            raise RuntimeError("confirmation execution unavailable")
        if cause == "duplicate":
            return fixed
        return replace(
            _replicate_result(left, right, child_won=True),
            outcome=GateOutcome("rejected", "tie", delta_scalar=0.0, delta_pass_rate=0.0),
        )

    if not holdout_case:
        monkeypatch.setattr(
            driver,
            "make_evidence_replicate_duel",
            lambda _run: None if cause == "missing_runner" else replicate,
        )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    expected = "rejected" if cause == "holdout_failure" else "deferred"
    assert outcome.tournament_decision == expected
    assert current_generation(workspace, epoch_id) == "v0"
    assert _lineage_promoted(workspace, epoch_id, "v1") is False
    record = _crowned_outcome(workspace, epoch_id, "v1")
    assert record["tournament_decision"] == expected
    bracket = _field_bracket(workspace, epoch_id, "v1")
    assert bracket["decision"] == expected
    assert bracket["promoted_generation_id"] == ""
    if holdout_case:
        from zicato.query import build_gate_breakdown

        block = record["holdout"]
        gate = build_gate_breakdown(WorkspacePaths(workspace), epoch_id, "v0", "v1")
        assert gate["decision"] == expected
        assert gate["deciding_rule"] == "holdout"
        assert gate["holdout"] == block
        assert block["confirmation_status"] == (
            "failed" if cause == "holdout_failure" else "incomplete"
        )
        assert block["ladder_budget_remaining"] == allowance - (cause != "exhausted")
        assert block["holdout_consulted"] is (cause != "exhausted")
        assert block["ladder_released"] is (cause == "holdout_failure")
        if cause == "withheld":
            assert block["confirmed"] is None
            assert block["holdout_scalar"] is None
            assert "holdout_not_confirmed" not in outcome.rejection_reason
        state = json.loads(ladder_state_path(workspace, epoch_id).read_text())
        assert state["budget_remaining"] == block["ladder_budget_remaining"]
    else:
        evidence = record["evidence"]
        assert evidence["confirmation_status"] == "incomplete"
        assert evidence["replicates_spent"] == calls
        assert len(evidence["attempts"]) > calls
        assert sum(row["budget_spent"] for row in evidence["attempts"]) == calls
        assert build_rating_view(WorkspacePaths(workspace), epoch_id, "v0", "v1") == {
            **evidence,
            "next_duel": None,
        }
        assert record["holdout"] is None
