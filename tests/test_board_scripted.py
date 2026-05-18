"""Tests for the scripted multi-turn driver."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zicato.board.scripted import ScriptedMultiTurnDriver
from zicato.core.types import BoardEntry, RuntimeConfig, ScriptedTurn


async def _harness_call(_: str, __: str, ___: str) -> str:
    return ""


async def _harness_call_aux(_: str, __: str, ___: str) -> str:
    return ""


@pytest.fixture
def runtime_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="default",
        workspace_root=tmp_path,
        harness_call_llm=_harness_call,
        auxiliary_call_llm=_harness_call_aux,
    )


class _RecordingHarness:
    """Mock harness that records every user message and replies deterministically."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.received: list[str] = []
        self._replies = replies or []

    async def run(self, user_message: str) -> str:
        self.received.append(user_message)
        if self._replies:
            return self._replies.pop(0)
        return f"agent reply to: {user_message}"


class _SlowHarness:
    """Mock harness that sleeps a configurable amount per turn."""

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds
        self.received: list[str] = []

    async def run(self, user_message: str) -> str:
        self.received.append(user_message)
        await asyncio.sleep(self._sleep_seconds)
        return f"slow reply: {user_message}"


class _FinalOutputHarness:
    """Harness whose reply is an object with a ``final_output`` attribute."""

    class _Reply:
        def __init__(self, text: str) -> None:
            self.final_output = text

    async def run(self, user_message: str) -> _FinalOutputHarness._Reply:
        return self._Reply(f"wrapped: {user_message}")


async def test_drive_accumulates_transcript(
    runtime_config: RuntimeConfig,
) -> None:
    entry = BoardEntry(
        id="scripted",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=30,
        turns=(
            ScriptedTurn("hello"),
            ScriptedTurn("again"),
            ScriptedTurn("done"),
        ),
        max_turns=5,
    )
    harness = _RecordingHarness()
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(harness, entry, sinks=[], config=runtime_config)

    assert harness.received == ["hello", "again", "done"]
    assert result.transcript == (
        "agent reply to: hello",
        "agent reply to: again",
        "agent reply to: done",
    )
    assert result.final_output == "agent reply to: done"
    assert result.aborted is False
    assert result.entry_id == "scripted"


async def test_drive_stops_at_max_turns(runtime_config: RuntimeConfig) -> None:
    """When max_turns < len(turns), the driver caps the conversation."""
    entry = BoardEntry(
        id="capped",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=30,
        turns=(
            ScriptedTurn("a"),
            ScriptedTurn("b"),
            ScriptedTurn("c"),
            ScriptedTurn("d"),
        ),
        max_turns=2,
    )
    harness = _RecordingHarness()
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(harness, entry, sinks=[], config=runtime_config)
    assert harness.received == ["a", "b"]
    assert len(result.transcript) == 2
    assert result.aborted is False


async def test_drive_stops_when_turns_exhausted(
    runtime_config: RuntimeConfig,
) -> None:
    """When turns are exhausted before max_turns, conversation ends cleanly."""
    entry = BoardEntry(
        id="short",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=30,
        turns=(ScriptedTurn("only"),),
        max_turns=10,
    )
    harness = _RecordingHarness()
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(harness, entry, sinks=[], config=runtime_config)
    assert harness.received == ["only"]
    assert len(result.transcript) == 1
    assert result.aborted is False


async def test_drive_aborts_on_budget_cutoff(
    runtime_config: RuntimeConfig,
) -> None:
    """A slow harness whose total time exceeds the budget aborts the run."""
    entry = BoardEntry(
        id="slow",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=1,  # very tight
        turns=(
            ScriptedTurn("t1"),
            ScriptedTurn("t2"),
            ScriptedTurn("t3"),
        ),
        max_turns=5,
    )
    harness = _SlowHarness(sleep_seconds=0.6)
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(harness, entry, sinks=[], config=runtime_config)

    assert result.aborted is True
    assert result.abort_reason == "wall_clock_budget"
    # At least one turn should have completed before the budget tripped.
    assert len(harness.received) >= 1
    # Should not have completed all three turns within a 1s budget at 0.6s each.
    assert len(harness.received) < 3


async def test_drive_uses_call_method_when_no_run(
    runtime_config: RuntimeConfig,
) -> None:
    class _CallHarness:
        def __init__(self) -> None:
            self.received: list[str] = []

        async def call(self, user_message: str) -> str:
            self.received.append(user_message)
            return f"call: {user_message}"

    entry = BoardEntry(
        id="call_form",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=10,
        turns=(ScriptedTurn("x"),),
        max_turns=3,
    )
    harness = _CallHarness()
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(harness, entry, sinks=[], config=runtime_config)
    assert harness.received == ["x"]
    assert result.final_output == "call: x"


async def test_drive_unwraps_final_output_attribute(
    runtime_config: RuntimeConfig,
) -> None:
    entry = BoardEntry(
        id="wrap",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=10,
        turns=(ScriptedTurn("hi"),),
        max_turns=2,
    )
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(_FinalOutputHarness(), entry, sinks=[], config=runtime_config)
    assert result.transcript == ("wrapped: hi",)
    assert result.final_output == "wrapped: hi"


async def test_drive_rejects_non_scripted_entry(
    runtime_config: RuntimeConfig,
) -> None:
    entry = BoardEntry(
        id="single",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hi",
    )
    driver = ScriptedMultiTurnDriver()
    with pytest.raises(ValueError, match="multi_turn_scripted"):
        await driver.drive(_RecordingHarness(), entry, sinks=[], config=runtime_config)


async def test_drive_records_harness_error_as_abort(
    runtime_config: RuntimeConfig,
) -> None:
    class _ExplodingHarness:
        async def run(self, user_message: str) -> str:
            raise RuntimeError("kaboom")

    entry = BoardEntry(
        id="boom",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=5,
        turns=(ScriptedTurn("x"),),
        max_turns=2,
    )
    driver = ScriptedMultiTurnDriver()
    result = await driver.drive(_ExplodingHarness(), entry, sinks=[], config=runtime_config)
    assert result.aborted is True
    assert "harness_error" in result.abort_reason
    assert "kaboom" in result.abort_reason
