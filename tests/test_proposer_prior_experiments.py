"""Proposer-level wiring of the experiment-memory digest.

A stub auxiliary LLM captures the user prompt so we can assert that a
non-empty ``prior_experiments`` surfaces the ``## What's already been
tried`` section and that an empty one omits it (the inert default — the
gating that keeps every pre-existing standalone-proposer test passing
unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import MutationPoint, Pattern, PriorExperiment
from zicato.proposer.proposer import propose_experiment


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


_MUTATIONS = [_mp("router__sp")]


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


class _CapturingLLM:
    """Returns one scripted response and records every user prompt."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.user_prompts: list[str] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.user_prompts.append(user)
        return self._response


def _prior() -> list[PriorExperiment]:
    return [
        PriorExperiment(
            generation_id="v3",
            epoch_id="e1",
            core_idea="Tighten the researcher instruction to forbid uncited claims.",
            modulating=("researcher.instruction",),
            decision="rejected",
            rejection_reason="pass_rate_regression",
            scalar_score_delta=-0.07,
        ),
    ]


@pytest.mark.asyncio
async def test_prior_experiments_surface_in_user_prompt() -> None:
    stub = _CapturingLLM(_valid_response())
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="loss=2.3",
        aux_call_llm=stub,
        prior_experiments=_prior(),
    )
    (prompt,) = stub.user_prompts
    assert "## What's already been tried" in prompt
    assert "Tighten the researcher instruction to forbid uncited claims." in prompt
    assert "Δscalar=-0.070" in prompt


@pytest.mark.asyncio
async def test_empty_prior_experiments_omit_the_section() -> None:
    stub = _CapturingLLM(_valid_response())
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="loss=2.3",
        aux_call_llm=stub,
    )
    (prompt,) = stub.user_prompts
    assert "What's already been tried" not in prompt
