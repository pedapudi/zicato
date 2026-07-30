"""Tests for the per-round loop-health wiring in the orchestrator.

After each round's tournament outcome is known, ``evolve_once`` calls
``zicato.health.diagnostics.assess_loop_health`` with the epoch's
accumulated losses + experiments + board, writes the resulting
``LoopHealth`` report to ``epochs/{epoch}/health/round_{N}.json``, and —
on a CRITICAL finding — logs a prominent stderr WARNING. The latest
health summary rides home on the :class:`EvolveRoundOutcome`.

These tests mock the (parallel-landing) ``zicato.health`` sibling with a
small dataclass-shaped ``LoopHealth`` / ``Finding`` pair and assert the
orchestrator's behaviour. Everything is stub-driven — no goldfive, no
real LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.test_orchestrator import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)

# ---------------------------------------------------------------------------
# Fake zicato.health.diagnostics sibling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeFinding:
    """Minimal stand-in for the health sibling's finding type."""

    severity: str
    message: str
    #: The real ``HealthFinding`` carries a stable ``code`` + ``summary`` +
    #: structured ``detail``; defaulted here so the many existing tests that
    #: build ``_FakeFinding(severity=, message=)`` keep working, while the
    #: dead-judge warning test can set them.
    code: str = ""
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeLoopHealth:
    """Minimal stand-in for ``zicato.health.diagnostics.LoopHealth``."""

    healthy: bool
    findings: tuple[_FakeFinding, ...] = field(default_factory=tuple)


def _install_fake_health(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health: _FakeLoopHealth,
    calls: list[tuple[Any, ...]],
) -> None:
    """Install a fake ``zicato.health`` package returning ``health``.

    Every ``assess_loop_health`` invocation is recorded in ``calls`` so
    the test can assert on the arguments the orchestrator passed.
    """
    diagnostics_mod = types.ModuleType("zicato.health.diagnostics")

    def assess_loop_health(
        losses_by_generation: dict[str, list[Any]],
        experiments: list[Any],
        board_entries: list[Any],
        epoch_id: str,
        config: Any | None = None,
        max_generations_per_contract: int | None = None,
        noise_floor: dict[str, Any] | None = None,
        promote_margin: float | None = None,
        evidence_gate_on: bool = True,
        preflight: dict[str, Any] | None = None,
        # Tolerant tail: the orchestrator threads further health inputs as
        # they land (``preflight_gate``, the runtime-event pairs), and this
        # stub exists to assert the call, not the signature.
        **_extra: Any,
    ) -> _FakeLoopHealth:
        del config
        calls.append((losses_by_generation, experiments, board_entries, epoch_id))
        return health

    diagnostics_mod.assess_loop_health = assess_loop_health  # type: ignore[attr-defined]
    diagnostics_mod.LoopHealth = _FakeLoopHealth  # type: ignore[attr-defined]

    health_pkg = types.ModuleType("zicato.health")
    health_pkg.diagnostics = diagnostics_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "zicato.health", health_pkg)
    monkeypatch.setitem(sys.modules, "zicato.health.diagnostics", diagnostics_mod)


def _run_one_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    health: _FakeLoopHealth,
    calls: list[tuple[Any, ...]],
) -> tuple[Path, str, Any]:
    """Bootstrap a workspace, install stubs, run one evolve round.

    Returns ``(workspace, epoch_id, outcome)``.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _install_fake_health(monkeypatch, health=health, calls=calls)

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    return workspace, epoch_id, outcome


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_evolve_once_writes_health_round_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """evolve_once writes epochs/{epoch}/health/round_N.json."""
    calls: list[tuple[Any, ...]] = []
    health = _FakeLoopHealth(healthy=True, findings=())
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
    health = _FakeLoopHealth(
        healthy=False,
        findings=(_FakeFinding(severity="WARNING", message="loss variance shrinking"),),
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
    health = _FakeLoopHealth(
        healthy=False,
        findings=(
            _FakeFinding(
                severity="CRITICAL",
                message="degenerate scoring: every generation scores identically",
            ),
        ),
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
    health = _FakeLoopHealth(
        healthy=False,
        findings=(
            _FakeFinding(
                severity="WARNING",
                message="2 board-declared judge(s) never fired",
                code="dead_judge",
                summary="2 board-declared judge(s) never fired across all 3 runs",
                detail={"dead_judges": ["audience_appropriate", "no_fabricated_numbers"]},
            ),
        ),
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
    health = _FakeLoopHealth(
        healthy=False,
        findings=(
            _FakeFinding(
                severity="WARNING",
                message="a mutable tree was never imported",
                code="tree_never_imported",
                summary=(
                    "mutations to tree goldfive cannot have been under test in "
                    "generation v3: no run of that generation ever imported goldfive"
                ),
                detail={"generation_id": "v3", "tree": "goldfive"},
            ),
        ),
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
    health = _FakeLoopHealth(
        healthy=True,
        findings=(_FakeFinding(severity="INFO", message="all good", code="no_expectations"),),
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)
    assert not any("DECLARED JUDGE NEVER FIRED" in rec.getMessage() for rec in caplog.records)


def test_evolve_once_critical_persisted_in_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The persisted round report records the critical finding."""
    calls: list[tuple[Any, ...]] = []
    health = _FakeLoopHealth(
        healthy=False,
        findings=(
            _FakeFinding(severity="CRITICAL", message="degenerate scoring"),
            _FakeFinding(severity="WARNING", message="pass-rate plateau"),
        ),
    )
    workspace, epoch_id, _ = _run_one_round(monkeypatch, tmp_path, health=health, calls=calls)

    report = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["has_critical"] is True
    assert body["healthy"] is False
    assert len(body["findings"]) == 2
    severities = {f["severity"] for f in body["findings"]}
    assert severities == {"CRITICAL", "WARNING"}


def test_evolve_once_runs_without_health_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no zicato.health sibling the round still completes, no report."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    monkeypatch.delitem(sys.modules, "zicato.health", raising=False)
    monkeypatch.delitem(sys.modules, "zicato.health.diagnostics", raising=False)

    real_import = __import__

    def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("zicato.health"):
            raise ImportError(f"no module named {name!r} (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocking_import)

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == "promoted"
    # No assessment ran → no summary, not critical, no report file.
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
    from zicato.orchestrator import _collect_epoch_health_inputs

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
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 1.0, "v2": 1.0, "v3": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )
    calls: list[tuple[Any, ...]] = []
    _install_fake_health(
        monkeypatch,
        health=_FakeLoopHealth(
            healthy=False,
            findings=(_FakeFinding(severity="CRITICAL", message="degenerate scoring"),),
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
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response() for _ in range(4)]),
            max_consecutive_rejections=99,  # isolate the health breaker
        )
    )

    assert len(outcomes) == 2
    assert all(o.health_critical for o in outcomes)


def test_evolve_n_rounds_opt_out_of_health_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """stop_on_degenerate_health=False runs every round despite CRITICAL health."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 1.0, "v2": 1.0, "v3": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )
    calls: list[tuple[Any, ...]] = []
    _install_fake_health(
        monkeypatch,
        health=_FakeLoopHealth(
            healthy=False,
            findings=(_FakeFinding(severity="CRITICAL", message="degenerate scoring"),),
        ),
        calls=calls,
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response() for _ in range(3)]),
            max_consecutive_rejections=99,
            stop_on_degenerate_health=False,
        )
    )

    # No early stop — all three rounds ran.
    assert len(outcomes) == 3
