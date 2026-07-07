"""Tests for the :class:`ProposerAgent` abstraction (Phase 2a core).

Covers the build-time selection + the skill-composed engine:

* :class:`DefaultProposerAgent` (the skill-composed single-shot engine)
  runs and returns a valid :class:`Experiment`.
* a spec carrying skills causes the skill body to land in the system
  prompt actually sent to the auxiliary callable.
* :func:`build_proposer_agent` returns the tool-using
  :class:`~zicato.proposer.adk_agent.ADKProposerAgent` for the BUILTIN
  DEFAULT (no proposer dir configured), the skill-composed
  :class:`DefaultProposerAgent` for a skills-only ``dir:*`` spec (the
  EXPLICIT opt-in), and raises ``ValueError`` when a custom ``agent.py`` is
  present but no ``proposer_path`` was supplied.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import Experiment, MutationPoint, Pattern, ProposerSkill, ProposerSpec
from zicato.proposer.adk_agent import ADKProposerAgent
from zicato.proposer.agent import (
    DefaultProposerAgent,
    ProposerContext,
    build_proposer_agent,
)
from zicato.testing.mock_llm import CannedCallLLM, RecordingCallLLM


def _mp(mid: str) -> MutationPoint:
    return MutationPoint(
        id=mid,
        kind="span",
        file=Path(f"/src/{mid}.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="content",
        content_hash="abc",
        metadata={},
    )


_MUTATIONS = (_mp("router__sp"),)


def _pattern() -> Pattern:
    return Pattern(
        id="pat1",
        kind="drift_kind_frequency",
        summary="off_topic dominates",
        detail={"top_kind": "off_topic"},
        affected_mutation_ids=("router__sp",),
        severity="warning",
    )


def _valid_response() -> str:
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten router preamble",
                "modulating": ["router__sp"],
                "why": "off_topic dominates",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "router__sp",
                    "op": "replace",
                    "new_content": "new router prompt",
                    "rationale": "tighter wording",
                }
            ],
        }
    )


def _context(aux: object) -> ProposerContext:
    return ProposerContext(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(_pattern(),),
        mutations=_MUTATIONS,
        brief_text="# Proposer brief\n- Be careful.\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=aux,  # type: ignore[arg-type]
        model="test-model",
    )


@pytest.mark.asyncio
async def test_default_agent_returns_valid_experiment() -> None:
    agent = DefaultProposerAgent(ProposerSpec.default())
    exp = await agent.propose(_context(CannedCallLLM([_valid_response()])))
    assert isinstance(exp, Experiment)
    assert [p.mutation_id for p in exp.patches] == ["router__sp"]


@pytest.mark.asyncio
async def test_skills_reach_the_system_prompt_sent_to_aux() -> None:
    skill = ProposerSkill(
        name="diversify",
        description="avoid re-proposing rejected directions",
        body="When a direction was rejected, change the lever, not the wording.",
    )
    spec = ProposerSpec(
        agent_id="dir:demo",
        tools=(),
        skills=(skill,),
        agent_source_sha256=None,
    )
    recorder = RecordingCallLLM(CannedCallLLM([_valid_response()]))
    agent = DefaultProposerAgent(spec)

    await agent.propose(_context(recorder))

    assert len(recorder.calls) == 1
    system_prompt = recorder.calls[0]["system"]
    assert skill.body in system_prompt
    assert skill.name in system_prompt
    assert "Proposer skills (composable guidance modules" in system_prompt


@pytest.mark.asyncio
async def test_revise_feedback_threads_into_the_engine_prompt() -> None:
    """The context's revise channel seeds the FIRST engine attempt's repair
    section (the best-of-N screen-informed revise path)."""
    from dataclasses import replace

    recorder = RecordingCallLLM(CannedCallLLM([_valid_response()]))
    agent = DefaultProposerAgent(ProposerSpec.default())
    ctx = replace(_context(recorder), revise_feedback="screen vetoed the whole slate")

    await agent.propose(ctx)

    assert len(recorder.calls) == 1
    user_prompt = recorder.calls[0]["user"]
    assert "Previous attempt was rejected" in user_prompt
    assert "screen vetoed the whole slate" in user_prompt


def test_build_agent_for_builtin_default_is_adk_tool_agent() -> None:
    # The DEFAULT proposer (no proposer dir configured) is the tool-using
    # ADK agent in builtin_default mode — NOT the skill-composed single-shot
    # engine. It builds its LlmAgent lazily from ctx.model at propose time.
    agent = build_proposer_agent(ProposerSpec.default())
    assert isinstance(agent, ADKProposerAgent)
    assert agent.builtin_default is True
    assert agent.proposer_path is None
    assert agent.agent is None  # built lazily on first propose
    assert agent.spec == ProposerSpec.default()


def test_build_agent_for_skills_only_dir_stays_skill_composed() -> None:
    # A configured proposer dir with skills but no agent.py is the EXPLICIT
    # opt-in into the skill-composed single-shot engine — it is NOT the
    # tool-using default.
    spec = ProposerSpec(
        agent_id="dir:demo",
        tools=(),
        skills=(ProposerSkill(name="s", description="", body="b"),),
        agent_source_sha256=None,
    )
    agent = build_proposer_agent(spec)
    assert isinstance(agent, DefaultProposerAgent)
    assert agent.spec is spec


def test_build_agent_with_custom_agent_source_requires_proposer_path() -> None:
    # Phase 2b: a custom-agent spec selects the ADK (Design A) path, which
    # needs the proposer dir to load proposers/<name>/agent.py from. Without
    # a proposer_path the builder raises ValueError rather than silently
    # falling back to the default agent.
    spec = ProposerSpec(
        agent_id="dir:demo",
        tools=(),
        skills=(),
        agent_source_sha256="deadbeef",
    )
    with pytest.raises(ValueError, match="no proposer_path"):
        build_proposer_agent(spec)
