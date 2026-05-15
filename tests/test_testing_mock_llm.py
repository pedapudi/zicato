"""Tests for :mod:`zicato.testing.mock_llm`."""

from __future__ import annotations

import pytest

from zicato.testing.mock_llm import (
    CannedCallLLM,
    RecordingCallLLM,
    ScriptedCallLLM,
)

# ---------------------------------------------------------------------------
# CannedCallLLM
# ---------------------------------------------------------------------------


async def test_canned_returns_responses_in_order() -> None:
    llm = CannedCallLLM(["a", "b", "c"])
    assert await llm("sys", "u1", "m") == "a"
    assert await llm("sys", "u2", "m") == "b"
    assert await llm("sys", "u3", "m") == "c"


async def test_canned_exhausted_raises_runtimeerror() -> None:
    llm = CannedCallLLM(["only"])
    assert await llm("s", "u", "m") == "only"
    with pytest.raises(RuntimeError, match="exhausted"):
        await llm("s", "u", "m")


async def test_canned_empty_responses_raises_on_first_call() -> None:
    llm = CannedCallLLM([])
    with pytest.raises(RuntimeError, match="exhausted after 0"):
        await llm("s", "u", "m")


async def test_canned_records_model_field() -> None:
    llm = CannedCallLLM(["x"], model="my-model")
    assert llm.model == "my-model"


async def test_canned_copies_response_list_on_construction() -> None:
    # Mutating the source list after construction must not change what
    # the double replays.
    responses = ["a", "b"]
    llm = CannedCallLLM(responses)
    responses.append("c")
    assert await llm("s", "u", "m") == "a"
    assert await llm("s", "u", "m") == "b"
    with pytest.raises(RuntimeError):
        await llm("s", "u", "m")


# ---------------------------------------------------------------------------
# RecordingCallLLM
# ---------------------------------------------------------------------------


async def test_recording_records_each_call() -> None:
    inner = CannedCallLLM(["r1", "r2"])
    rec = RecordingCallLLM(inner)

    assert await rec("sysA", "userA", "m1") == "r1"
    assert await rec("sysB", "userB", "m2") == "r2"

    assert rec.calls == [
        {"system": "sysA", "user": "userA", "model": "m1", "response": "r1"},
        {"system": "sysB", "user": "userB", "model": "m2", "response": "r2"},
    ]


async def test_recording_empty_calls_initially() -> None:
    rec = RecordingCallLLM(CannedCallLLM(["x"]))
    assert rec.calls == []


async def test_recording_propagates_inner_exception_without_recording() -> None:
    rec = RecordingCallLLM(CannedCallLLM([]))
    with pytest.raises(RuntimeError):
        await rec("s", "u", "m")
    # No entry recorded when the inner fails.
    assert rec.calls == []


# ---------------------------------------------------------------------------
# ScriptedCallLLM
# ---------------------------------------------------------------------------


async def test_scripted_matches_by_substring() -> None:
    llm = ScriptedCallLLM(
        [
            ("EMULATOR", "", "emulator-said-hi"),
            ("HARNESS", "question?", "harness-answer"),
        ]
    )

    assert (
        await llm("system: EMULATOR persona", "anything goes", "m")
        == "emulator-said-hi"
    )
    assert (
        await llm("system: HARNESS prompt", "what is the question?", "m")
        == "harness-answer"
    )


async def test_scripted_first_match_wins() -> None:
    llm = ScriptedCallLLM(
        [
            ("", "", "first"),
            ("", "", "second"),
        ]
    )
    assert await llm("s", "u", "m") == "first"


async def test_scripted_unmatched_raises() -> None:
    llm = ScriptedCallLLM([("ONLY", "MATCHING", "yes")])
    with pytest.raises(RuntimeError, match="no rule matched"):
        await llm("nothing", "here", "m")


async def test_scripted_empty_strings_are_wildcards() -> None:
    # An empty substring is a wildcard for that side, since "" is a
    # substring of every string. Useful as a fallback rule.
    llm = ScriptedCallLLM(
        [
            ("special", "", "matched-system"),
            ("", "", "fallback"),
        ]
    )
    assert await llm("special prompt", "u", "m") == "matched-system"
    assert await llm("ordinary", "u", "m") == "fallback"


async def test_scripted_requires_both_sides_match() -> None:
    llm = ScriptedCallLLM([("SYS", "USR", "ok")])
    # System matches but user does not.
    with pytest.raises(RuntimeError):
        await llm("SYS only", "no match here", "m")
