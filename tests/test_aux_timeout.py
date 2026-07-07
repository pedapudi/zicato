"""Tests for the ``asyncio.wait_for`` wrappers around aux_call_llm.

Each aux call site (proposer, judge, emulator turn, analysis pass)
must surface a deterministic outcome when the LLM endpoint hangs past
the configured budget. We exercise each with a hung-mock that sleeps
forever and a near-zero budget pinned the way the ``--aux-call-timeout``
flag pins it (``zicato.config.pin_overrides``) so the test completes in
milliseconds. The suite-wide autouse fixture clears pins between tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from zicato.aux_timeout import DEFAULT_AUX_CALL_TIMEOUT_S, aux_call_timeout_s
from zicato.config import pin_overrides


def _pin_aux_timeout(seconds: float) -> None:
    """Pin the aux budget exactly as ``zicato evolve --aux-call-timeout`` does."""
    pin_overrides({"aux": {"call_timeout_s": seconds}})


# ---------------------------------------------------------------------------
# Module-level config
# ---------------------------------------------------------------------------


def test_default_timeout_is_120s() -> None:
    assert aux_call_timeout_s() == DEFAULT_AUX_CALL_TIMEOUT_S


def test_pinned_flag_value_wins() -> None:
    """A pinned ``--aux-call-timeout`` value reaches the bare call-site form."""
    _pin_aux_timeout(5.5)
    assert aux_call_timeout_s() == 5.5


def test_deleted_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ZICATO_AUX_CALL_TIMEOUT`` was deleted for the flag; setting it is a no-op."""
    monkeypatch.setenv("ZICATO_AUX_CALL_TIMEOUT", "5.5")
    assert aux_call_timeout_s() == DEFAULT_AUX_CALL_TIMEOUT_S


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------


async def _hung_aux(system: str, user: str, model: str) -> str:
    del system, user, model
    await asyncio.sleep(60)
    return ""


def test_proposer_timeout_raises_proposer_error() -> None:
    """A hung aux LLM exhausts retries with ``auxiliary LLM call timed out`` errors."""
    _pin_aux_timeout(0.05)

    from zicato.core.types import MutationPoint
    from zicato.proposer.proposer import ProposerError, propose_experiment

    mut = MutationPoint(
        id="m1",
        kind="span",
        file=Path("/tmp/agent.py"),
        source_root=Path("/tmp"),
        line_start=1,
        line_end=1,
        content="GREETING = 'hi'",
        content_hash="0" * 64,
    )

    with pytest.raises(ProposerError) as exc_info:
        asyncio.run(
            propose_experiment(
                epoch_id="e",
                parent_generation_id="v0",
                new_generation_id="v1",
                patterns=(),
                mutations=(mut,),
                brief_text="# proposer brief",
                current_loss_summary="",
                aux_call_llm=_hung_aux,
                max_retries=0,
            )
        )

    assert any("timed out" in m for m in exc_info.value.attempts)


# ---------------------------------------------------------------------------
# Rubric (board matcher — the LLM-as-judge OUTCOME matcher)
# ---------------------------------------------------------------------------


def test_rubric_timeout_returns_rubric_timeout_detail() -> None:
    """A hung rubric aux returns ``passed=False`` with detail ``rubric_judge_timeout``."""
    _pin_aux_timeout(0.05)

    from zicato.board.matchers import evaluate_expectation
    from zicato.board.predicates import Rubric
    from zicato.core.types import RunResult

    expectation = Rubric.score("score the answer 0-10", threshold=5.0)
    result = RunResult(
        run_id="r",
        entry_id="e",
        final_output="answer",
        transcript=("answer",),
        runtime_ms=10,
    )

    outcome = asyncio.run(evaluate_expectation(expectation, result, aux_call_llm=_hung_aux))
    assert outcome.passed is False
    assert outcome.detail == "rubric_judge_timeout"


# ---------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------


async def _harness_callable(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def test_emulator_timeout_aborts_with_emulator_timeout() -> None:
    """A hung emulator-side aux aborts the driver with ``emulator_timeout``."""
    _pin_aux_timeout(0.05)

    from zicato.core.types import BoardEntry, RuntimeConfig, UserPersona
    from zicato.emulator.emulator import EmulatedMultiTurnDriver

    persona = UserPersona(
        goal="ask about widgets",
        constraints="be polite",
        stop_when="agent answers",
    )
    entry = BoardEntry(
        id="multi",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=60,
        user_persona=persona,
        max_turns=3,
    )

    async def _run_harness_turn(user_msg: str) -> str:
        del user_msg
        return "agent says hi"

    config = RuntimeConfig(
        instance_id="t",
        workspace_root=Path("/tmp"),
        harness_call_llm=_harness_callable,
        auxiliary_call_llm=_hung_aux,
    )

    driver = EmulatedMultiTurnDriver()
    result = asyncio.run(driver.drive(_run_harness_turn, entry, config))

    assert result.aborted is True
    assert result.abort_reason == "emulator_timeout"


# ---------------------------------------------------------------------------
# Analysis pass
# ---------------------------------------------------------------------------


def test_analysis_timeout_substitutes_placeholder(tmp_path: Path) -> None:
    """A hung analysis aux writes ``analysis.md`` with a placeholder narrative."""
    _pin_aux_timeout(0.05)

    from zicato.core.workspace import analysis_path
    from zicato.epoch.analysis import generate_analysis

    # Drop a minimal experiment so hydrate finds something.
    gen_dir = tmp_path / "epochs" / "epoch_a" / "generations" / "v1"
    gen_dir.mkdir(parents=True)
    (gen_dir / "experiment.json").write_text(
        json.dumps(
            {
                "id": "exp",
                "epoch_id": "epoch_a",
                "generation_id": "v1",
                "parent_generation_id": "v0",
                "proposed_at": "2026-05-14T00:00:00Z",
                "hypothesis": {
                    "core_idea": "test",
                    "modulating": [],
                    "why": "test",
                    "expected_pass_rate_delta": "+0",
                    "risks": "",
                },
                "outcome": {
                    "ran_at": "2026-05-14T00:00:01Z",
                    "drift_movements": [],
                    "pass_rate_delta": 0.0,
                    "drift_loss_delta": 0.0,
                    "scalar_score_delta": -0.1,
                    "tournament_decision": "promoted",
                    "rejection_reason": "",
                },
            }
        )
    )

    out_path = asyncio.run(generate_analysis(tmp_path, "epoch_a", _hung_aux))
    assert out_path == analysis_path(tmp_path, "epoch_a")
    text = out_path.read_text()
    assert "analysis LLM timed out" in text
    assert "## Headline movements" in text
