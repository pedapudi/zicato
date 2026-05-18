"""End-to-end tests for :class:`EmulatedMultiTurnDriver`.

These tests exercise the driver with stubbed ``auxiliary_call_llm`` and
``run_harness_turn`` callables — no real LLMs. The goal is to pin:

* The two-callable check fires on shared identity.
* The driver alternates emulator and harness turns correctly.
* ``<<END>>`` termination is detected on a line by itself.
* ``max_turns`` caps the loop.
* The answer-leak heuristic aborts the run.
* Audits are produced per emulator turn.
* Audits emit to a goldfive-shaped sink without crashing on sink failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from zicato.core.types import BoardEntry, RuntimeConfig, UserPersona
from zicato.emulator.emulator import (
    EmulatedMultiTurnDriver,
    EmulationCollusionError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persona() -> UserPersona:
    return UserPersona(
        goal="Buy a laptop for travel.",
        constraints="Be vague about budget.",
        stop_when="A specific model has been recommended.",
    )


def _entry(max_turns: int = 3) -> BoardEntry:
    return BoardEntry(
        id="laptop-recommend",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=60,
        user_persona=_persona(),
        max_turns=max_turns,
    )


def _make_canned_aux(
    outputs: list[str],
) -> Callable[[str, str, str], Awaitable[str]]:
    """Build an async stub that returns ``outputs`` in order on each call."""
    state = {"i": 0}

    async def aux(system: str, user: str, model: str) -> str:
        i = state["i"]
        state["i"] += 1
        if i >= len(outputs):
            return "<<END>>"
        return outputs[i]

    return aux


def _make_canned_harness(
    outputs: list[str],
) -> Callable[[str], Awaitable[str]]:
    """Build an async stub that returns ``outputs`` in order on each call."""
    state = {"i": 0}

    async def harness(user_msg: str) -> str:
        i = state["i"]
        state["i"] += 1
        if i >= len(outputs):
            return "I have nothing further to say."
        return outputs[i]

    return harness


async def _unused_harness_llm(system: str, user: str, model: str) -> str:
    raise AssertionError("harness_call_llm should not be invoked by the driver")


def _config(aux: Callable[[str, str, str], Awaitable[str]]) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="test",
        workspace_root=Path("/tmp/zicato-test"),
        harness_call_llm=_unused_harness_llm,
        auxiliary_call_llm=aux,
    )


# ---------------------------------------------------------------------------
# Two-callable hard check
# ---------------------------------------------------------------------------


async def test_drive_raises_on_shared_callable() -> None:
    shared = _make_canned_aux(["hi"])
    config = RuntimeConfig(
        instance_id="test",
        workspace_root=Path("/tmp/zicato-test"),
        harness_call_llm=shared,  # type: ignore[arg-type]
        auxiliary_call_llm=shared,
    )
    driver = EmulatedMultiTurnDriver()
    with pytest.raises(EmulationCollusionError):
        await driver.drive(
            run_harness_turn=_make_canned_harness(["ok"]),
            entry=_entry(),
            config=config,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_drive_alternates_turns_and_stops_on_end_token() -> None:
    aux = _make_canned_aux(
        [
            "Hi, I want a travel laptop.",
            "Sounds great, I'll take the one you suggested.\n<<END>>",
        ]
    )
    harness_outputs = [
        "Sure — what kind of trips and budget?",
        "Based on what you said, I'd recommend the Foo X1.",
    ]
    harness = _make_canned_harness(harness_outputs)
    driver = EmulatedMultiTurnDriver()
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=5), config=_config(aux)
    )
    # Two emulator turns, one harness turn between them, then END_TOKEN
    # short-circuits the loop before a second harness call.
    assert result.transcript == (harness_outputs[0],)
    assert result.final_output == harness_outputs[0]
    assert not result.aborted
    assert result.entry_id == "laptop-recommend"
    # Two audits (one per emulator turn).
    assert len(driver.audits) == 2


async def test_drive_hits_max_turns_cap() -> None:
    # Emulator never says <<END>>; we should loop exactly max_turns times.
    aux = _make_canned_aux(
        [
            "Hi.",
            "Tell me more.",
            "And more.",
        ]
    )
    harness = _make_canned_harness(
        [
            "What trips?",
            "What budget?",
            "Got it; thinking about it.",
        ]
    )
    driver = EmulatedMultiTurnDriver()
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=3), config=_config(aux)
    )
    assert not result.aborted
    # 3 emulator turns => 3 harness turns => transcript length 3.
    assert len(result.transcript) == 3
    assert len(driver.audits) == 3


async def test_drive_treats_end_token_only_on_its_own_line() -> None:
    # <<END>> embedded inside a sentence must NOT terminate.
    aux = _make_canned_aux(
        [
            "Tell me about <<END>> processing in your spec.",
            "Thanks, that's it.\n<<END>>",
        ]
    )
    harness = _make_canned_harness(
        [
            "Sure, here's some info.",
            "Glad to help.",
        ]
    )
    driver = EmulatedMultiTurnDriver()
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=4), config=_config(aux)
    )
    # First emulator turn did NOT terminate (embedded END), second did.
    # So one harness call happened.
    assert len(result.transcript) == 1


# ---------------------------------------------------------------------------
# Leak detection
# ---------------------------------------------------------------------------


async def test_drive_aborts_on_emulator_leak() -> None:
    aux = _make_canned_aux(
        [
            "Hi.",
            "The answer is the Foo X1.",  # leak — triggers abort
        ]
    )
    harness = _make_canned_harness(["What's your budget?"])
    driver = EmulatedMultiTurnDriver()
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=4), config=_config(aux)
    )
    assert result.aborted
    assert result.abort_reason == "emulator_leak_detected"
    # The harness was called once (after the clean first turn), then the
    # second emulator turn triggered the abort before any further harness call.
    assert len(result.transcript) == 1


async def test_drive_aborts_on_code_fence_leak() -> None:
    aux = _make_canned_aux(['```json\n{"x": 1}\n```'])
    harness = _make_canned_harness([])
    driver = EmulatedMultiTurnDriver()
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=4), config=_config(aux)
    )
    assert result.aborted
    assert result.abort_reason == "emulator_leak_detected"
    assert result.transcript == ()


# ---------------------------------------------------------------------------
# Audits + sink
# ---------------------------------------------------------------------------


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


async def test_drive_emits_audits_to_sink() -> None:
    sink = _RecordingSink()
    aux = _make_canned_aux(["Hi.", "Done.\n<<END>>"])
    harness = _make_canned_harness(["Sure thing."])
    driver = EmulatedMultiTurnDriver(sink_emit_fn=sink)
    await driver.drive(run_harness_turn=harness, entry=_entry(max_turns=4), config=_config(aux))
    # Two emulator turns => two audit events.
    assert len(sink.events) == 2
    for event in sink.events:
        assert event["lane"] == "zicato:emulator"
        assert event["kind"] == "zicato.emulator.turn_audit"
        assert isinstance(event["persona_hash"], str)
        assert isinstance(event["transcript_chars_in"], int)
        assert isinstance(event["output_chars_out"], int)
        assert isinstance(event["output_preview"], str)


class _ExplodingSink:
    def emit(self, event: dict) -> None:
        raise RuntimeError("sink is down")


async def test_drive_swallows_sink_failures() -> None:
    sink = _ExplodingSink()
    aux = _make_canned_aux(["Hi.", "Done.\n<<END>>"])
    harness = _make_canned_harness(["Sure thing."])
    driver = EmulatedMultiTurnDriver(sink_emit_fn=sink)
    # MUST NOT raise — audit failures are observability, not policy.
    result = await driver.drive(
        run_harness_turn=harness, entry=_entry(max_turns=4), config=_config(aux)
    )
    assert not result.aborted


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_drive_rejects_wrong_entry_kind() -> None:
    bad_entry = BoardEntry(
        id="x",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )
    aux = _make_canned_aux(["irrelevant"])
    driver = EmulatedMultiTurnDriver()
    with pytest.raises(ValueError, match="multi_turn_emulated"):
        await driver.drive(
            run_harness_turn=_make_canned_harness([]),
            entry=bad_entry,
            config=_config(aux),
        )
