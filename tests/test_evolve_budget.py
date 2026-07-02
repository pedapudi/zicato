"""Tests for the per-evolve total wall-clock budget in ``evolve_n_rounds``.

``evolve_n_rounds`` accepts an optional ``max_wall_clock_seconds`` ceiling
for the WHOLE invocation, on top of each board entry's own
``wall_clock_budget_seconds``. The budget is enforced two ways:

* between rounds — before starting round N+1, if the monotonic elapsed
  time has reached the budget the loop stops cleanly and returns the
  outcomes gathered so far;
* within a round — each round's work is wrapped in ``asyncio.wait_for``
  with a timeout of the remaining budget, so a single long round that
  would overrun the total is cancelled and recorded as an aborted
  round (a synthetic :class:`EvolveRoundOutcome` carrying a
  ``"wall_clock_budget"`` rejection reason).

These tests are mock-driven: ``evolve_once`` is monkeypatched with a
controllable stand-in (no goldfive, no real LLM). The workspace lock +
heartbeat lifecycle still run against a real bootstrapped workspace so
the loop is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import zicato.orchestrator as orch
from tests.test_orchestrator import _bootstrap_workspace
from zicato.orchestrator import EvolveRoundOutcome

# ---------------------------------------------------------------------------
# Mock evolve_once
# ---------------------------------------------------------------------------


def _make_outcome(round_idx: int, decision: str = "promoted") -> EvolveRoundOutcome:
    """Build a plain promoted/rejected round outcome for round ``round_idx``."""
    return EvolveRoundOutcome(
        parent_generation_id=f"v{round_idx}",
        proposed_generation_id=f"v{round_idx + 1}",
        tournament_decision=decision,
        rejection_reason="" if decision == "promoted" else "regressed",
        parent_scalar=1.0,
        child_scalar=0.5 if decision == "promoted" else 1.5,
        delta_scalar=-0.5 if decision == "promoted" else 0.5,
    )


def _install_mock_evolve_once(
    monkeypatch: pytest.MonkeyPatch,
    *,
    per_round_sleep: float,
    decision: str = "promoted",
    calls: list[int] | None = None,
) -> None:
    """Replace ``orchestrator.evolve_once`` with a sleeping mock.

    Each call sleeps ``per_round_sleep`` seconds (a cooperative
    ``asyncio.sleep`` so ``asyncio.wait_for`` can pre-empt it) and then
    returns a plain outcome. The ``calls`` list, when supplied, records
    one entry per invocation so the test can assert how many rounds
    actually started.
    """

    async def _mock_evolve_once(
        *,
        round_index: int = 0,
        **_kwargs: Any,
    ) -> EvolveRoundOutcome:
        if calls is not None:
            calls.append(round_index)
        if per_round_sleep > 0:
            await asyncio.sleep(per_round_sleep)
        return _make_outcome(round_index, decision)

    monkeypatch.setattr(orch, "evolve_once", _mock_evolve_once)


class _FakeClock:
    """A controllable monotonic clock for deterministic budget tests.

    The orchestrator reads ``time.monotonic`` for the total-budget
    deadline. Patching it with this fake lets a test advance virtual
    time by an exact amount per round — so the between-rounds check
    fires deterministically, with no dependence on real wall-clock.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _install_clock_advancing_evolve_once(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _FakeClock,
    per_round_advance: float,
    decision: str = "promoted",
    calls: list[int] | None = None,
) -> None:
    """Mock ``evolve_once`` so each round advances ``clock`` by a fixed amount.

    The mock does no real sleeping — it just bumps the fake clock — so
    ``asyncio.wait_for`` (which uses the real event-loop clock) never
    fires. This isolates the *between-rounds* budget check.
    """

    async def _mock_evolve_once(
        *,
        round_index: int = 0,
        **_kwargs: Any,
    ) -> EvolveRoundOutcome:
        if calls is not None:
            calls.append(round_index)
        clock.advance(per_round_advance)
        return _make_outcome(round_index, decision)

    monkeypatch.setattr(orch, "evolve_once", _mock_evolve_once)
    monkeypatch.setattr(orch.time, "monotonic", clock.monotonic)


async def _harness_call_llm(system: str, user: str, model: str) -> str:
    return "harness-output"


async def _aux_call_llm(system: str, user: str, model: str) -> str:
    return "aux-output"


# ---------------------------------------------------------------------------
# Between-rounds budget
# ---------------------------------------------------------------------------


def test_tiny_budget_stops_loop_early(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A tiny total budget stops the loop with fewer rounds than requested.

    Each mock round sleeps ~0.4s; with a 1s total budget, round 0 and
    round 1 complete (cumulative ~0.8s), then before round 2 the
    elapsed time (~0.8s)... is still under 1s, so round 2 also starts
    but finishes around ~1.2s — at which point the next between-rounds
    check halts the loop. Either way, the loop runs strictly fewer
    rounds than the 8 requested and the stop reason is the budget.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    calls: list[int] = []
    _install_mock_evolve_once(monkeypatch, per_round_sleep=0.4, calls=calls)

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=8,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
            max_wall_clock_seconds=1,
            stop_reason_out=stop_reason,
        )
    )
    # Strictly fewer rounds than requested — the budget cut it short.
    assert 0 < len(outcomes) < 8
    # The stop reason is the total wall-clock budget, in one of its two
    # forms (cleanly between rounds, or a mid-round abort).
    assert stop_reason[0] in {
        "wall_clock_budget_between_rounds",
        "wall_clock_budget_mid_round",
    }


def test_between_rounds_budget_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A round that completes but pushes elapsed past the budget stops cleanly.

    Uses a fake monotonic clock so timing is exact: each round advances
    the clock by 4s, the budget is 10s. Round 0 (clock 0→4), round 1
    (4→8), round 2 (8→12). Before round 3 the elapsed (12s) is past the
    10s budget → the between-rounds check halts the loop. The three
    completed rounds are genuine (non-aborted) outcomes.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    clock = _FakeClock()
    calls: list[int] = []
    _install_clock_advancing_evolve_once(
        monkeypatch, clock=clock, per_round_advance=4.0, calls=calls
    )

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=8,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
            max_wall_clock_seconds=10,
            stop_reason_out=stop_reason,
        )
    )
    assert len(outcomes) == 3
    assert len(outcomes) < 8
    assert calls == [0, 1, 2]
    assert stop_reason == ["wall_clock_budget_between_rounds"]
    # All three completed rounds are genuine outcomes, not synthetic aborts.
    assert all(o.tournament_decision == "promoted" for o in outcomes)
    assert all(not o.rejection_reason.startswith("wall_clock_budget") for o in outcomes)


# ---------------------------------------------------------------------------
# Unbounded (None) — historical behaviour
# ---------------------------------------------------------------------------


def test_unbounded_budget_runs_all_rounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``max_wall_clock_seconds=None`` runs every requested round."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    calls: list[int] = []
    # Rounds that would each blow any small budget — but None disables
    # the ceiling entirely.
    _install_mock_evolve_once(monkeypatch, per_round_sleep=0.05, calls=calls)

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=4,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
            max_wall_clock_seconds=None,
            stop_reason_out=stop_reason,
        )
    )
    assert len(outcomes) == 4
    assert calls == [0, 1, 2, 3]
    assert stop_reason == ["completed"]


def test_default_is_unbounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Omitting ``max_wall_clock_seconds`` keeps the historical behaviour."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_mock_evolve_once(monkeypatch, per_round_sleep=0.02)

    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
        )
    )
    assert len(outcomes) == 3


# ---------------------------------------------------------------------------
# Within-round budget — a single round overruns the remaining budget
# ---------------------------------------------------------------------------


def test_round_exceeding_remaining_budget_is_recorded_aborted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A round that itself overruns the budget is recorded aborted; loop stops.

    The single round sleeps far longer than the whole budget, so the
    within-round ``asyncio.wait_for`` cancels it. The orchestrator then
    records a synthetic aborted :class:`EvolveRoundOutcome` and stops.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    calls: list[int] = []
    # 10s round, 1s budget → wait_for fires at ~1s, round 0 aborted.
    _install_mock_evolve_once(monkeypatch, per_round_sleep=10.0, calls=calls)

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=5,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
            max_wall_clock_seconds=1,
            stop_reason_out=stop_reason,
        )
    )
    # The round started (so calls recorded it) but was cancelled.
    assert calls == [0]
    # Exactly one outcome — the synthetic aborted round — and the loop
    # stopped rather than attempting round 1.
    assert len(outcomes) == 1
    aborted = outcomes[0]
    assert aborted.tournament_decision == "rejected"
    assert aborted.rejection_reason.startswith("wall_clock_budget")
    assert "1s" in aborted.rejection_reason
    assert aborted.proposed_generation_id == ""
    assert stop_reason == ["wall_clock_budget_mid_round"]


def test_per_entry_budget_still_applies_independently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The total budget does not replace the per-entry budget.

    The bootstrapped board entry still carries its own
    ``wall_clock_budget_seconds`` (60s). Passing a total budget leaves
    that field untouched — both ceilings are live. We assert the board
    entry's per-entry budget is unchanged after an evolve invocation
    that also set a total budget.
    """
    from zicato import workspace_loader

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    board_before = workspace_loader.load_current_board(workspace)
    assert board_before[0].wall_clock_budget_seconds == 60

    _install_mock_evolve_once(monkeypatch, per_round_sleep=0.02)
    asyncio.run(
        orch.evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
            max_wall_clock_seconds=300,
        )
    )
    # The per-entry budget is independent and still 60s — the total
    # budget is an additional ceiling, not a replacement.
    board_after = workspace_loader.load_current_board(workspace)
    assert board_after[0].wall_clock_budget_seconds == 60


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _install_cli_capture(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Patch ``evolve_n_rounds`` as imported by the CLI to capture kwargs."""

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("completed")
        return []

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)


def test_cli_passes_max_wall_clock_seconds_flag(
    monkeypatch: pytest.MonkeyPatch,
    mock_dashboard_spawn: list[Any],
) -> None:
    """``--max-wall-clock-seconds N`` is plumbed into ``evolve_n_rounds``."""
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    captured: dict[str, Any] = {}
    _install_cli_capture(monkeypatch, captured)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_evolve_budget:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_evolve_budget:_aux_call_llm",
            "--max-wall-clock-seconds",
            "450",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_wall_clock_seconds"] == 450


def test_cli_max_wall_clock_seconds_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
    mock_dashboard_spawn: list[Any],
) -> None:
    """With no flag the total budget is ``None`` (unbounded)."""
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    captured: dict[str, Any] = {}
    _install_cli_capture(monkeypatch, captured)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_evolve_budget:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_evolve_budget:_aux_call_llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_wall_clock_seconds"] is None


def test_cli_max_wall_clock_seconds_env_var_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    mock_dashboard_spawn: list[Any],
) -> None:
    """The deleted ``ZICATO_MAX_WALL_CLOCK_SECONDS`` env var is ignored.

    The variable was fully shadowed by ``--max-wall-clock-seconds`` and
    was deleted; setting it must leave the budget unbounded (``None``),
    exactly as if it were never set.
    """
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    captured: dict[str, Any] = {}
    _install_cli_capture(monkeypatch, captured)
    monkeypatch.setenv("ZICATO_MAX_WALL_CLOCK_SECONDS", "720")

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_evolve_budget:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_evolve_budget:_aux_call_llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_wall_clock_seconds"] is None


def test_cli_summary_reports_budget_stop(
    monkeypatch: pytest.MonkeyPatch,
    mock_dashboard_spawn: list[Any],
) -> None:
    """The CLI summary names the total-budget stop distinctly."""
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("wall_clock_budget_between_rounds")
        return [_make_outcome(0)]

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_evolve_budget:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_evolve_budget:_aux_call_llm",
            "--rounds",
            "5",
            "--max-wall-clock-seconds",
            "120",
        ],
    )
    assert result.exit_code == 0, result.output
    # The summary line names the total wall-clock budget and is distinct
    # from "completed all rounds" / the consecutive-reject phrasing.
    assert "total wall-clock budget" in result.output
    assert "120s" in result.output
    assert "completed all" not in result.output


def test_cli_summary_reports_mid_round_abort(
    monkeypatch: pytest.MonkeyPatch,
    mock_dashboard_spawn: list[Any],
) -> None:
    """The CLI summary calls out a mid-round budget abort distinctly."""
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("wall_clock_budget_mid_round")
        return [_make_outcome(0, decision="rejected")]

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_evolve_budget:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_evolve_budget:_aux_call_llm",
            "--rounds",
            "3",
            "--max-wall-clock-seconds",
            "90",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "total wall-clock budget" in result.output
    assert "cancelled" in result.output
    assert "90s" in result.output
