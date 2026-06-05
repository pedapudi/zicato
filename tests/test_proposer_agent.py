"""Tests for the :class:`ProposerAgent` abstraction (Phase 2a core).

Covers three seams:

* :class:`DefaultProposerAgent` runs the single-shot engine and returns a
  valid :class:`Experiment`.
* a spec carrying skills causes the skill body to land in the system
  prompt actually sent to the auxiliary callable.
* :func:`build_proposer_agent` returns the default agent for the builtin
  and for a skills-only ``dir:*`` spec, and raises ``NotImplementedError``
  when a custom ``agent.py`` is present (the Phase 2b seam).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import Experiment, MutationPoint, Pattern, ProposerSkill, ProposerSpec
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


def test_build_agent_for_builtin_default() -> None:
    agent = build_proposer_agent(ProposerSpec.default())
    assert isinstance(agent, DefaultProposerAgent)
    assert agent.spec.skills == ()


def test_build_agent_for_skills_only_dir() -> None:
    spec = ProposerSpec(
        agent_id="dir:demo",
        tools=(),
        skills=(ProposerSkill(name="s", description="", body="b"),),
        agent_source_sha256=None,
    )
    agent = build_proposer_agent(spec)
    assert isinstance(agent, DefaultProposerAgent)
    assert agent.spec is spec


def test_build_agent_with_custom_agent_source_is_phase_2b_seam() -> None:
    spec = ProposerSpec(
        agent_id="dir:demo",
        tools=(),
        skills=(),
        agent_source_sha256="deadbeef",
    )
    with pytest.raises(NotImplementedError):
        build_proposer_agent(spec)
