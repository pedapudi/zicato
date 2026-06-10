"""Tests for best-of-N sampling + the self-critique pass.

Covers FUNCTIONALITY-RECOMMENDATIONS.md §4.1:

* ``best_of_n == 1`` is byte-identical to today — the wrapper returns the
  inner agent UNCHANGED and a single ``propose`` runs with no critique call;
* ``best_of_n > 1`` samples N candidates and the self-critique pass picks the
  better of two scripted candidates through the (mock) auxiliary LLM;
* the deterministic heuristic selects the smaller diff that targets an
  observed failure mode when critique is disabled;
* a failing / unparseable critic falls back to the heuristic;
* the critic sees ONLY the restricted proposer context (no holdout) — its
  prompt is assembled from the same restricted renderers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.core.types import (
    Experiment,
    HypothesisSpec,
    MutationPoint,
    Patch,
    Pattern,
    ProposerQualityConfig,
)
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import (
    BestOfNProposerAgent,
    wrap_with_proposer_quality,
)


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


_MUTATIONS = (_mp("router__sp"), _mp("writer__sp"))


def _pattern(affected: tuple[str, ...]) -> Pattern:
    return Pattern(
        id="pat1",
        kind="drift_kind_frequency",
        summary="off_topic dominates",
        detail={"top_kind": "off_topic"},
        affected_mutation_ids=affected,
        severity="warning",
    )


def _experiment(*, core_idea: str, mutation_id: str, new_content: str) -> Experiment:
    """A minimal valid :class:`Experiment` targeting one mutation point."""
    return Experiment(
        id=f"exp_{core_idea}",
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-06-09T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=core_idea,
            modulating=(mutation_id,),
            why="because",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.05",
        ),
        patches=(
            Patch(
                id="p1",
                mutation_id=mutation_id,
                op="replace",
                new_content=new_content,
                new_numeric=None,
                new_enum=None,
                rationale="r",
            ),
        ),
        outcome=None,
    )


class _ScriptedInnerAgent:
    """Returns one scripted candidate per ``propose`` call, in order."""

    def __init__(self, candidates: list[Experiment]) -> None:
        self._candidates = list(candidates)
        self.calls = 0

    async def propose(self, ctx: ProposerContext) -> Experiment:
        idx = self.calls
        self.calls += 1
        return self._candidates[idx % len(self._candidates)]


class _CapturingCriticLLM:
    """A critic double: records every prompt, returns a scripted choice."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.system_prompts.append(system)
        self.user_prompts.append(user)
        return self._response


def _context(
    aux: object,
    *,
    patterns: tuple[Pattern, ...] = (),
    restrict: bool = True,
) -> ProposerContext:
    return ProposerContext(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=patterns,
        mutations=_MUTATIONS,
        brief_text="# Proposer brief\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=aux,  # type: ignore[arg-type]
        model="test-model",
        restrict_visibility=restrict,
    )


# --------------------------------------------------------------------------
# N == 1 — byte-identical to today
# --------------------------------------------------------------------------


def test_wrap_with_n1_returns_inner_unchanged() -> None:
    inner = _ScriptedInnerAgent(
        [_experiment(core_idea="a", mutation_id="router__sp", new_content="x")]
    )
    wrapped = wrap_with_proposer_quality(inner, ProposerQualityConfig())  # best_of_n == 1
    assert wrapped is inner


@pytest.mark.asyncio
async def test_n1_single_inner_call_no_critique() -> None:
    """With best_of_n == 1, exactly one inner propose runs and the critic
    callable is NEVER touched."""
    only = _experiment(core_idea="only", mutation_id="router__sp", new_content="x")
    inner = _ScriptedInnerAgent([only])
    critic = _CapturingCriticLLM("0")
    # Even when explicitly constructed at N=1, the agent short-circuits.
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=1))
    out = await agent.propose(_context(critic))
    assert out is only
    assert inner.calls == 1
    assert critic.user_prompts == []  # no critique call at N=1


# --------------------------------------------------------------------------
# Best-of-N picks the better of two scripted candidates via the critic
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_best_of_n_critic_picks_scripted_winner() -> None:
    cand0 = _experiment(
        core_idea="big speculative edit", mutation_id="writer__sp", new_content="z" * 200
    )
    cand1 = _experiment(
        core_idea="minimal grounded edit", mutation_id="router__sp", new_content="x"
    )
    inner = _ScriptedInnerAgent([cand0, cand1])
    # The critic is scripted to pick candidate index 1.
    critic = _CapturingCriticLLM("1")
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))

    out = await agent.propose(_context(critic, patterns=(_pattern(("router__sp",)),)))

    assert out is cand1
    assert inner.calls == 2  # N candidates sampled
    assert len(critic.user_prompts) == 1  # one critique call
    # The critic's prompt carries both candidates and the restricted context.
    prompt = critic.user_prompts[0]
    assert "Candidate 0" in prompt
    assert "Candidate 1" in prompt
    assert "minimal grounded edit" in prompt


@pytest.mark.asyncio
async def test_best_of_n_critic_can_pick_the_other_candidate() -> None:
    """The selection follows the critic's choice, not a fixed slot — picking
    index 0 returns candidate 0."""
    cand0 = _experiment(core_idea="idea-zero", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="idea-one", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    out = await agent.propose(_context(critic))
    assert out is cand0


# --------------------------------------------------------------------------
# Heuristic selection (critique disabled) + critic-failure fallback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heuristic_prefers_grounded_minimal_diff_when_critique_disabled() -> None:
    """With critique disabled, the deterministic heuristic picks the smaller
    diff that targets an OBSERVED failure mode — no LLM call."""
    # cand0: large diff, targets writer (not flagged). cand1: small diff,
    # targets router (flagged by the pattern). Heuristic prefers cand1.
    cand0 = _experiment(core_idea="big", mutation_id="writer__sp", new_content="z" * 500)
    cand1 = _experiment(core_idea="small grounded", mutation_id="router__sp", new_content="x")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")  # would pick cand0 — but must not be called
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False),
    )
    out = await agent.propose(_context(critic, patterns=(_pattern(("router__sp",)),)))
    assert out is cand1
    assert critic.user_prompts == []  # heuristic path makes no LLM call


@pytest.mark.asyncio
async def test_unparseable_critic_falls_back_to_heuristic() -> None:
    """A critic that returns garbage does not block the step — the heuristic
    selects instead."""
    cand0 = _experiment(core_idea="big", mutation_id="writer__sp", new_content="z" * 500)
    cand1 = _experiment(core_idea="small grounded", mutation_id="router__sp", new_content="x")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("no integer here")
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    out = await agent.propose(_context(critic, patterns=(_pattern(("router__sp",)),)))
    # Heuristic prefers the grounded, minimal cand1.
    assert out is cand1


@pytest.mark.asyncio
async def test_out_of_range_critic_choice_falls_back_to_heuristic() -> None:
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="x")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="z" * 500)
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("9")  # out of range for a 2-candidate slate
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    out = await agent.propose(_context(critic, patterns=(_pattern(("router__sp",)),)))
    # Heuristic: neither is grounded (no router pattern? router IS flagged) —
    # cand0 targets router (flagged) and is the smaller diff, so it wins.
    assert out is cand0


# --------------------------------------------------------------------------
# Overfitting envelope: the critic sees only the restricted context
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_prompt_uses_restricted_visibility() -> None:
    """When ``restrict_visibility`` is on, the per-entry pattern identities in
    the critic's context are aggregated to counts — the critic never sees a
    raw per-entry id (the same envelope the proposer is held to)."""
    cand0 = _experiment(core_idea="zero", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="one", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")
    # A pattern whose detail carries a per-entry id list — under restrict the
    # renderer aggregates it to a count, so the raw id must not appear.
    leaky_pattern = Pattern(
        id="pat_leak",
        kind="metric_frequency",
        summary="off_topic across entries",
        # The detector emits this as a comma-joined id string; under
        # ``restrict_visibility`` the renderer aggregates it to a count.
        detail={"affected_entry_ids": "secret_entry_42,secret_entry_7"},
        affected_mutation_ids=("router__sp",),
        severity="warning",
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    await agent.propose(_context(critic, patterns=(leaky_pattern,), restrict=True))
    prompt = critic.user_prompts[0]
    assert "secret_entry_42" not in prompt
    # The critic prompt explicitly frames itself as the restricted view.
    assert "no held-out data" in prompt


# --------------------------------------------------------------------------
# Failure contract — an all-failed slate re-raises the inner ProposerError
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_failed_slate_reraises_proposer_error() -> None:
    from zicato.proposer.proposer import ProposerError

    class _AlwaysFails:
        calls = 0

        async def propose(self, ctx: ProposerContext) -> Experiment:
            type(self).calls += 1
            raise ProposerError(["could not produce a valid challenger"])

    inner = _AlwaysFails()
    critic = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    with pytest.raises(ProposerError):
        await agent.propose(_context(critic))
    assert inner.calls == 3  # sampled the whole slate before giving up


@pytest.mark.asyncio
async def test_partial_failure_uses_surviving_candidate() -> None:
    """When only one of N candidates is producible the survivor is returned
    with no critique call (a 1-candidate slate needs no selection)."""
    from zicato.proposer.proposer import ProposerError

    survivor = _experiment(core_idea="survivor", mutation_id="router__sp", new_content="x")

    class _OneSurvives:
        def __init__(self) -> None:
            self.calls = 0

        async def propose(self, ctx: ProposerContext) -> Experiment:
            self.calls += 1
            if self.calls == 2:
                return survivor
            raise ProposerError(["nope"])

    inner = _OneSurvives()
    critic = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(_context(critic))
    assert out is survivor
    assert critic.user_prompts == []  # single survivor — no critique needed
