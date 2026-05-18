"""Tests for sealed context construction.

The sealed prompts are the audit boundary for the emulator; their
contents and signatures are part of the contract. These tests pin the
verbatim non-leakage paragraph and the explicit-signature posture.
"""

from __future__ import annotations

import inspect

import pytest

from zicato.core.types import UserPersona
from zicato.emulator.sealed import (
    END_TOKEN,
    NON_LEAKAGE_PARAGRAPH,
    build_emulator_system_prompt,
    build_emulator_user_prompt,
)


def _persona() -> UserPersona:
    return UserPersona(
        goal="Get the agent to recommend a moderately-priced laptop for travel.",
        constraints=(
            "You are polite but vague about budget. You will not paste "
            "specs. You will not name brands first."
        ),
        stop_when=(
            "The agent has recommended a specific model and you feel "
            "confident enough to end the chat."
        ),
    )


# ---------------------------------------------------------------------------
# build_emulator_system_prompt
# ---------------------------------------------------------------------------


def test_system_prompt_includes_all_persona_fields() -> None:
    persona = _persona()
    prompt = build_emulator_system_prompt(persona)
    assert persona.goal in prompt
    assert persona.constraints in prompt
    assert persona.stop_when in prompt


def test_system_prompt_includes_non_leakage_paragraph_verbatim() -> None:
    persona = _persona()
    prompt = build_emulator_system_prompt(persona)
    assert NON_LEAKAGE_PARAGRAPH in prompt


def test_system_prompt_mentions_end_token() -> None:
    persona = _persona()
    prompt = build_emulator_system_prompt(persona)
    assert END_TOKEN in prompt


def test_system_prompt_states_user_not_oracle_posture() -> None:
    persona = _persona()
    prompt = build_emulator_system_prompt(persona)
    lowered = prompt.lower()
    assert "user, not an oracle" in lowered or "not an oracle" in lowered


def test_system_prompt_signature_has_no_kwargs() -> None:
    sig = inspect.signature(build_emulator_system_prompt)
    for param in sig.parameters.values():
        assert param.kind is not inspect.Parameter.VAR_KEYWORD, (
            "build_emulator_system_prompt must NOT accept **kwargs — "
            "sealed signature is the audit boundary"
        )
        assert (
            param.kind is not inspect.Parameter.VAR_POSITIONAL
        ), "build_emulator_system_prompt must NOT accept *args"
    assert list(sig.parameters) == ["persona"]


def test_system_prompt_rejects_extra_kwargs() -> None:
    persona = _persona()
    with pytest.raises(TypeError):
        build_emulator_system_prompt(persona, expectation="boo")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# build_emulator_user_prompt
# ---------------------------------------------------------------------------


def test_user_prompt_empty_transcript_opens_conversation() -> None:
    prompt = build_emulator_user_prompt(())
    # No agent turns, no AGENT: markers.
    assert "AGENT:" not in prompt
    assert "YOU (the user):" in prompt


def test_user_prompt_renders_agent_turns_in_order() -> None:
    transcript = (
        "Hi! Tell me about your trip.",
        "Got it — domestic flights, two weeks, light packing.",
    )
    prompt = build_emulator_user_prompt(transcript)
    # All turns present.
    for turn in transcript:
        assert turn in prompt
    # Order preserved.
    assert prompt.index(transcript[0]) < prompt.index(transcript[1])
    # Two AGENT: markers, one per agent turn.
    assert prompt.count("AGENT:") == len(transcript)
    assert prompt.rstrip().endswith("YOU (the user):")


def test_user_prompt_signature_has_no_kwargs() -> None:
    sig = inspect.signature(build_emulator_user_prompt)
    for param in sig.parameters.values():
        assert param.kind is not inspect.Parameter.VAR_KEYWORD
        assert param.kind is not inspect.Parameter.VAR_POSITIONAL
    assert list(sig.parameters) == ["transcript"]


def test_user_prompt_does_not_leak_extras() -> None:
    # If the caller had a way to smuggle extras, we'd see them — but the
    # function only accepts `transcript`. Verify a transcript containing
    # tool-call-shaped text is rendered verbatim WITHOUT injection of
    # anything outside the tuple.
    transcript = ("Plain agent text only.",)
    prompt = build_emulator_user_prompt(transcript)
    # The prompt should not invent contents beyond the persona-less
    # framing and the supplied transcript.
    assert "tool_call" not in prompt
    assert "system_prompt" not in prompt
    assert "expectation" not in prompt
    assert "predicate" not in prompt
