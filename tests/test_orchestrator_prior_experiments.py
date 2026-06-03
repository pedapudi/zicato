"""Orchestrator-level wiring of experiment memory in the field path.

Proves the multi-challenger field accumulates in-flight siblings: with a
capturing aux LLM that records each proposer call's user prompt and
returns a challenger-specific hypothesis, challenger k's prompt carries
the in-flight core-ideas of challengers 0..k-1 minted earlier this round,
and a challenger whose proposer failed contributes no sibling line.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
)
from tests.test_orchestrator_multi_challenger import _bootstrap_swiss_workspace


def _response_with_core_idea(core_idea: str) -> str:
    """A schema-valid proposer response targeting the stub ``greeting`` marker."""
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": ["greeting"],
                "why": "exercising the sibling-accumulation path",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
                ],
                "expected_pass_rate_delta": "+0.0 to +0.1",
                "risks": "harmless",
            },
            "patches": [
                {
                    "mutation_id": "greeting",
                    "op": "replace",
                    "new_content": '"world"',
                    "rationale": "different greeting word",
                }
            ],
        }
    )


class _CapturingFieldLLM:
    """Yields scripted proposer responses in order, recording each
    PROPOSER user prompt.

    The auxiliary callable is shared by the proposer and the end-of-round
    epoch-report writer, so we record only the proposer prompts (the ones
    carrying the mutation manifest) and return a benign placeholder for
    any post-field report call so the round finishes cleanly.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.proposer_prompts: list[str] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        # The proposer's user prompt is the only one carrying the mutation
        # manifest; the report writer's prompt does not.
        is_proposer = "## Mutation points" in user
        if is_proposer:
            self.proposer_prompts.append(user)
            if not self._responses:
                raise AssertionError("stub aux LLM ran out of proposer responses")
            return self._responses.pop(0)
        # Non-proposer (epoch-report) call — return harmless prose.
        return "report placeholder"


def test_field_accumulates_in_flight_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Challenger k's prompt carries the in-flight core-ideas of the
    siblings minted before it this round."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=3, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.5, "v2": 1.0, "v3": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    idea_a = "challenger-A tighten coordinator routing"
    idea_b = "challenger-B require source citations"
    idea_c = "challenger-C terser specialist descriptions"
    aux = _CapturingFieldLLM(
        [
            _response_with_core_idea(idea_a),
            _response_with_core_idea(idea_b),
            _response_with_core_idea(idea_c),
        ]
    )

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=aux,
        )
    )

    assert len(aux.proposer_prompts) == 3
    p0, p1, p2 = aux.proposer_prompts

    # The first challenger has no prior settled history and no siblings yet,
    # so its prompt carries none of the round's core-ideas.
    assert idea_a not in p0
    assert "What's already been tried" not in p0

    # The second challenger sees challenger-A as an in-flight sibling.
    assert "What's already been tried" in p1
    assert idea_a in p1
    assert idea_b not in p1

    # The third challenger sees both earlier siblings, not yet itself.
    assert idea_a in p2
    assert idea_b in p2
    assert idea_c not in p2


def test_failed_challenger_contributes_no_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger whose proposer fails has no hypothesis to share, so it
    contributes no in-flight sibling line to the next challenger's prompt."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    idea_b = "challenger-B require source citations"
    # First challenger's response is empty (proposer fails with zero
    # retries); the second is valid.
    aux = _CapturingFieldLLM(["", _response_with_core_idea(idea_b)])

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=aux,
            max_proposer_retries=0,
        )
    )

    assert len(aux.proposer_prompts) == 2
    _, p1 = aux.proposer_prompts
    # The second challenger sees no in-flight sibling — the first failed and
    # produced no hypothesis — so the section is omitted entirely.
    assert "What's already been tried" not in p1
