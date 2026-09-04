"""Tests for the per-round loop-health wiring in the orchestrator.

After each round's tournament outcome is known, ``evolve_once`` calls
``zicato.health.diagnostics.assess_loop_health`` with the epoch's
accumulated losses + experiments + board, writes the resulting
``LoopHealth`` report to ``epochs/{epoch}/health/round_{N}.json``, and —
on a CRITICAL finding — logs a prominent stderr WARNING. The latest
health summary rides home on the :class:`EvolveRoundOutcome`.

These tests pin ``assess_loop_health`` to a chosen report — built from the
real :class:`~zicato.health.diagnostics.LoopHealth` and
:class:`~zicato.health.diagnostics.HealthFinding`, so what the orchestrator
reads off a finding is what a detector actually writes — and assert the
orchestrator's behaviour. Everything is stub-driven — no goldfive, no real
LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    bootstrap_workspace,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
    target_call_llm,
)
from zicato.health.diagnostics import HealthFinding, LoopHealth

# ---------------------------------------------------------------------------
# A pinned loop-health assessment
# ---------------------------------------------------------------------------


def _report(*findings: HealthFinding, healthy: bool) -> LoopHealth:
    """A ``LoopHealth`` carrying ``findings``, as a real assessment returns one.

    ``epoch_id`` and ``checked_at`` are placeholders: the persisted round
    report is stamped with the round's own epoch id and assessment time by
    ``_loop_health_to_json``, so the report's own values never reach an
    assertion here.
    """
    return LoopHealth(
        epoch_id="pinned",
        findings=findings,
        healthy=healthy,
        checked_at="2026-01-01T00:00:00+00:00",
    )


def _pin_health_assessment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health: LoopHealth,
    calls: list[tuple[Any, ...]],
) -> None:
    """Pin ``assess_loop_health`` to return ``health`` instead of assessing.

    Every invocation is recorded in ``calls`` so the test can assert on the
    arguments the orchestrator passed. The rest of :mod:`zicato.health` —
    the workspace readers in :mod:`zicato.health.inputs` above all — stays
    real.
    """
    import zicato.health.diagnostics as diagnostics

    def assess_loop_health(
        losses_by_generation: dict[str, list[Any]],
        experiments: list[Any],
        board_entries: list[Any],
        epoch_id: str,
        **_kwargs: Any,
    ) -> LoopHealth:
        calls.append((losses_by_generation, experiments, board_entries, epoch_id))
        return health

    monkeypatch.setattr(diagnostics, "assess_loop_health", assess_loop_health)


def _run_one_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    health: LoopHealth,
    calls: list[tuple[Any, ...]],
) -> tuple[Path, str, Any]:
    """Bootstrap a workspace, install stubs, run one evolve round.

    Returns ``(workspace, epoch_id, outcome)``.
    """
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _pin_health_assessment(monkeypatch, health=health, calls=calls)

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))
    return workspace, epoch_id, outcome


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_evolve_once_writes_health_round_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """evolve_once writes epochs/{epoch}/health/round_N.json."""
    calls: list[tuple[Any, ...]] = []
    health = _report(healthy=True)
    workspace, epoch_id, _ = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    # The child generation is v1, so the report is round_1.json.
    report = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    assert report.exists()

    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["epoch_id"] == epoch_id
    assert body["round"] == 1
    assert body["healthy"] is True
    assert body["has_critical"] is False
    assert "assessed_at" in body

    # assess_loop_health was called once with the board + epoch id.
    assert len(calls) == 1
    losses_by_generation, experiments, board_entries, passed_epoch = calls[0]
    assert passed_epoch == epoch_id
    assert [e.id for e in board_entries] == ["entry_a"]
    # losses_by_generation is a dict keyed by generation id (its content
    # depends on which runs persisted a loss.json — the stub reducer here
    # does not, so it may be empty; the shape is what matters).
    assert isinstance(losses_by_generation, dict)
    # Experiments include the v1 experiment just persisted.
    assert any(getattr(x, "generation_id", None) == "v1" for x in experiments)


def test_evolve_once_health_summary_on_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The EvolveRoundOutcome carries the round's health summary."""
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(
            code="flat_drift_signal",
            severity="warning",
            summary="loss variance shrinking",
        ),
        healthy=False,
    )
    _, _, outcome = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    assert outcome.health_critical is False
    assert "loss variance shrinking" in outcome.health_summary


def test_evolve_once_critical_finding_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CRITICAL finding logs a prominent warning and flags the outcome."""
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(
            code="degenerate_scoring",
            severity="critical",
            summary="degenerate scoring: every generation scores identically",
        ),
        healthy=False,
    )

    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _, _, outcome = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    # The outcome is flagged critical and the summary names the problem.
    assert outcome.health_critical is True
    assert "CRITICAL" in outcome.health_summary
    assert "degenerate scoring" in outcome.health_summary

    # A prominent WARNING was logged — the operator must see "no signal".
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and rec.name == "zicato.orchestrator"
    ]
    assert any("LOOP HEALTH CRITICAL" in m for m in warnings)
    assert any("no usable signal" in m for m in warnings)


def test_evolve_once_dead_judge_finding_logs_loud_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``dead_judge`` finding is promoted to a LOUD run-level WARNING (issue #84).

    A board-declared judge that produced no metric across the whole
    generation is only a soft ``warning`` in the health report — easy to
    miss. The orchestrator must surface it on the terminal so an operator
    cannot mistake a never-invoked / unreachable judge for one that ran and
    passed.
    """
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(
            code="dead_judge",
            severity="warning",
            summary="2 board-declared judge(s) never fired across all 3 runs",
            detail={"dead_judges": ["audience_appropriate", "no_fabricated_numbers"]},
        ),
        healthy=False,
    )

    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _, _, outcome = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    # A dead judge is a WARNING, not a CRITICAL — the loop is not "no signal".
    assert outcome.health_critical is False

    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and rec.name == "zicato.orchestrator"
    ]
    assert any("DECLARED JUDGE NEVER FIRED" in m for m in warnings), warnings
    # The dead judges are named so the operator knows which to fix.
    assert any("audience_appropriate" in m for m in warnings)


def test_evolve_once_tree_never_imported_logs_loud_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``tree_never_imported`` finding is promoted to a LOUD WARNING (issue #110).

    The round's verdict compared two identical unmutated trees, which reads
    exactly like an honest null result from the terminal. It must be as visible
    as the dead-judge alarm, for the same reason.
    """
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(
            code="tree_never_imported",
            severity="warning",
            summary=(
                "mutations to tree goldfive cannot have been under test in "
                "generation v3: no run of that generation ever imported goldfive"
            ),
            detail={"generation_id": "v3", "tree": "goldfive"},
        ),
        healthy=False,
    )

    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _, _, outcome = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    # A warning, not a critical — the operator, not the detector, judges it.
    assert outcome.health_critical is False
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and rec.name == "zicato.orchestrator"
    ]
    assert any("MUTATED TREE NEVER IMPORTED" in m for m in warnings), warnings
    assert any("goldfive" in m for m in warnings)


def test_evolve_once_no_dead_judge_warning_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``dead_judge`` finding ⇒ no loud dead-judge warning."""
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(code="no_expectations", severity="info", summary="all good"),
        healthy=True,
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)
    assert not any("DECLARED JUDGE NEVER FIRED" in rec.getMessage() for rec in caplog.records)


def test_evolve_once_critical_persisted_in_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The persisted round report records the critical finding."""
    calls: list[tuple[Any, ...]] = []
    health = _report(
        HealthFinding(code="degenerate_scoring", severity="critical", summary="degenerate scoring"),
        HealthFinding(code="stalled_loop", severity="warning", summary="pass-rate plateau"),
        healthy=False,
    )
    workspace, epoch_id, _ = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    report = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["has_critical"] is True
    assert body["healthy"] is False
    assert len(body["findings"]) == 2
    severities = {f["severity"] for f in body["findings"]}
    assert severities == {"critical", "warning"}


def test_a_detector_that_raises_costs_the_report_not_the_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A detector raising mid-assessment leaves the round's verdict standing.

    Loop health is observability, and observability must never decide a
    tournament: the round is assessed only after its duels have settled, so
    a detector that raises costs the round its health report and nothing
    else. The round still promotes, and the outcome carries the degraded
    (empty, non-critical) health summary rather than an exception.
    """
    import zicato.health.diagnostics as diagnostics

    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    def _raises(*_args: Any, **_kwargs: Any) -> list[HealthFinding]:
        raise RuntimeError("detector failed mid-assessment")

    # The first detector `assess_loop_health` runs: every finding the
    # assessment would have collected is lost with it.
    monkeypatch.setattr(diagnostics, "detect_degenerate_scoring", _raises)

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    assert outcome.tournament_decision == "promoted"
    # No assessment completed → no summary, not critical, no report file.
    assert outcome.health_summary == ""
    assert outcome.health_critical is False
    assert not (workspace / "epochs" / epoch_id / "health").exists()


def test_collect_epoch_health_inputs_reads_persisted_losses(tmp_path: Path) -> None:
    """_collect_epoch_health_inputs gathers per-generation loss.json files.

    Exercises the losses-collection path directly with real loss.json
    files on disk (the evolve-loop tests use a stub reducer that does
    not persist, so this is the only place the read path is covered).
    """
    from zicato.core.types import BoardEntry, DriftCount, LossProfile
    from zicato.core.workspace import loss_profile_path
    from zicato.evolve.round_reporting import _collect_epoch_health_inputs

    workspace = tmp_path / ".zicato"
    epoch_id = "e0"

    def _write_loss(gen_id: str, entry_id: str, drift: float) -> None:
        loss = LossProfile(
            run_id=f"r-{gen_id}-{entry_id}",
            entry_id=entry_id,
            generation_id=gen_id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=drift,
            pass_fail=True,
        )
        path = loss_profile_path(workspace, epoch_id, gen_id, entry_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": loss.run_id,
                    "entry_id": loss.entry_id,
                    "generation_id": loss.generation_id,
                    "epoch_id": loss.epoch_id,
                    "drift_counts": [{"kind": "off_topic", "severity": "info", "count": 0}],
                    "plan_revisions": 0,
                    "task_failure_ratio": 0.0,
                    "runtime_ms": 100,
                    "wall_clock_budget_exceeded": False,
                    "expectation_result": None,
                    "drift_loss": drift,
                    "pass_fail": True,
                }
            ),
            encoding="utf-8",
        )

    _write_loss("v0", "entry_a", 2.0)
    _write_loss("v1", "entry_a", 1.0)

    board = [BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="hi")]
    losses_by_generation, experiments = _collect_epoch_health_inputs(workspace, epoch_id, board)

    assert set(losses_by_generation) == {"v0", "v1"}
    assert losses_by_generation["v0"][0].drift_loss == 2.0
    assert losses_by_generation["v1"][0].drift_loss == 1.0
    # No experiment.json was written → experiments is empty, not an error.
    assert experiments == []


def test_evolve_n_rounds_stops_on_consecutive_critical_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two consecutive CRITICAL-health rounds stop evolve_n_rounds early."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 1.0, "v2": 1.0, "v3": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )
    calls: list[tuple[Any, ...]] = []
    _pin_health_assessment(
        monkeypatch,
        health=_report(
            HealthFinding(
                code="degenerate_scoring", severity="critical", summary="degenerate scoring"
            ),
            healthy=False,
        ),
        calls=calls,
    )

    from zicato.orchestrator import evolve_n_rounds

    # Four rounds requested; the loop-health breaker fires after the 2nd
    # consecutive CRITICAL round.
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=4,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=make_aux_responder([]),
            max_consecutive_rejections=99,  # isolate the health breaker
        )
    )

    assert len(outcomes) == 2
    assert all(o.health_critical for o in outcomes)


def test_evolve_n_rounds_opt_out_of_health_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """stop_on_degenerate_health=False runs every round despite CRITICAL health."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 1.0, "v2": 1.0, "v3": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )
    calls: list[tuple[Any, ...]] = []
    _pin_health_assessment(
        monkeypatch,
        health=_report(
            HealthFinding(
                code="degenerate_scoring", severity="critical", summary="degenerate scoring"
            ),
            healthy=False,
        ),
        calls=calls,
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=make_aux_responder([]),
            max_consecutive_rejections=99,
            stop_on_degenerate_health=False,
        )
    )

    # No early stop — all three rounds ran.
    assert len(outcomes) == 3


# ---------------------------------------------------------------------------
# Issue #130 — only a PROMOTED duel's per-entry regressions reach the health
# assessment: a rejected challenger is discarded, so it baked nothing in.
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutcome:
    decision: str
    attributable_regressions: tuple[str, ...] = ()


@dataclass
class _FakeResult:
    outcome: Any
    parent_agg: Any
    child_agg: Any


def _duel(decision: str, regressions: tuple[str, ...]) -> _FakeResult:
    return _FakeResult(
        outcome=_FakeOutcome(decision=decision, attributable_regressions=regressions),
        parent_agg={"per_entry": {"e0": {"score": 1.0, "drift_loss": 0.10}}},
        child_agg={"per_entry": {"e0": {"score": 1.0, "drift_loss": 0.60}}},
    )


def test_promoted_regressions_are_threaded_with_their_evidence() -> None:
    from zicato.evolve.round_reporting import _promoted_entry_regressions

    detail = _promoted_entry_regressions(_duel("promoted", ("e0",)))
    assert detail == {
        "e0": {
            "parent_score": 1.0,
            "child_score": 1.0,
            "parent_drift_loss": 0.10,
            "child_drift_loss": 0.60,
        }
    }


def test_a_rejected_duel_threads_nothing() -> None:
    from zicato.evolve.round_reporting import _promoted_entry_regressions

    assert _promoted_entry_regressions(_duel("rejected", ("e0",))) is None


def test_a_clean_promotion_threads_nothing() -> None:
    from zicato.evolve.round_reporting import _promoted_entry_regressions

    assert _promoted_entry_regressions(_duel("promoted", ())) is None


def test_an_unexpected_result_shape_is_tolerated() -> None:
    """A health input never fails a round."""
    from zicato.evolve.round_reporting import _promoted_entry_regressions

    assert _promoted_entry_regressions(object()) is None
    assert (
        _promoted_entry_regressions(_FakeResult(_FakeOutcome("promoted", ("e0",)), None, None))
        is None
    )
