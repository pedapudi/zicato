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
    wrapped = wrap_with_proposer_quality(inner, ProposerQualityConfig(best_of_n=1))
    assert wrapped is inner


def test_wrap_default_interposes_wrapper() -> None:
    # The noise-aware default (best_of_n == 3) interposes the best-of-N
    # wrapper; only an explicit best_of_n == 1 pin is a pass-through.
    inner = _ScriptedInnerAgent(
        [_experiment(core_idea="a", mutation_id="router__sp", new_content="x")]
    )
    wrapped = wrap_with_proposer_quality(inner, ProposerQualityConfig())
    assert wrapped is not inner
    assert isinstance(wrapped, BestOfNProposerAgent)
    assert wrapped.config.best_of_n == 3


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


# --------------------------------------------------------------------------
# Intra-slate diversity — distinct edit-class hints per slate slot
# --------------------------------------------------------------------------


class _HintRecordingInnerAgent(_ScriptedInnerAgent):
    """Scripted inner agent that also records each call's ``sample_hint``."""

    def __init__(self, candidates: list[Experiment]) -> None:
        super().__init__(candidates)
        self.hints: list[str] = []

    async def propose(self, ctx: ProposerContext) -> Experiment:
        self.hints.append(ctx.sample_hint)
        return await super().propose(ctx)


@pytest.mark.asyncio
async def test_slate_slots_carry_distinct_edit_class_hints() -> None:
    """Each of the N samples gets a DISTINCT rotating edit-class hint on its
    context; the caller's own context is never mutated."""
    from zicato.proposer.best_of_n import EDIT_CLASS_HINTS

    inner = _HintRecordingInnerAgent(
        [
            _experiment(core_idea="a", mutation_id="router__sp", new_content="x"),
            _experiment(core_idea="b", mutation_id="router__sp", new_content="y"),
            _experiment(core_idea="c", mutation_id="router__sp", new_content="z"),
        ]
    )
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=3, critique_enabled=False),
    )
    ctx = _context(_CapturingCriticLLM("0"))
    await agent.propose(ctx)
    assert inner.hints == list(EDIT_CLASS_HINTS[:3])
    assert len(set(inner.hints)) == 3
    assert ctx.sample_hint == ""  # the shared context is untouched


@pytest.mark.asyncio
async def test_n1_direct_propose_carries_no_hint() -> None:
    """The single-sample short-circuit passes the context through verbatim —
    no hint section is ever added to a non-slate propose."""
    inner = _HintRecordingInnerAgent(
        [_experiment(core_idea="only", mutation_id="router__sp", new_content="x")]
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=1))
    await agent.propose(_context(_CapturingCriticLLM("0")))
    assert inner.hints == [""]


def test_sample_hint_renders_a_prompt_section_only_when_set() -> None:
    from zicato.proposer.prompts import render_user_prompt

    base_kwargs: dict = {
        "current_loss_summary": "loss=1.0",
        "patterns": (),
        "mutations": _MUTATIONS,
    }
    plain = render_user_prompt(**base_kwargs)
    assert "Edit-class hint" not in plain
    # Empty hint is byte-identical to the pre-surface prompt.
    assert render_user_prompt(**base_kwargs, sample_hint="") == plain
    hinted = render_user_prompt(**base_kwargs, sample_hint="Prefer the smallest grounded fix.")
    assert hinted.startswith("## Edit-class hint (this sample)\n")
    assert "Prefer the smallest grounded fix." in hinted
    # The hint only PREPENDS — the rest of the prompt is unchanged.
    assert hinted.endswith(plain)


# --------------------------------------------------------------------------
# Calibration-aware selection — prediction_accuracy steers, never gates
# --------------------------------------------------------------------------


def _prediction_bearing(core_idea: str, *, diff_pad: str = "") -> Experiment:
    """A candidate whose hypothesis states a concrete expected movement."""
    from dataclasses import replace as _replace

    from zicato.core.types import ExpectedDriftMovement

    exp = _experiment(core_idea=core_idea, mutation_id="router__sp", new_content="x" + diff_pad)
    return _replace(
        exp,
        hypothesis=_replace(
            exp.hypothesis,
            expected_drift_movements=(
                ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="medium"),
            ),
        ),
    )


def _prior(accuracy: float | None) -> object:
    from zicato.core.types import PriorExperiment

    return PriorExperiment(
        generation_id="v9",
        epoch_id="e1",
        core_idea="prior",
        modulating=("router__sp",),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=-0.1,
        prediction_accuracy=accuracy,
    )


def test_heuristic_prefers_predictions_when_lineage_calibrated() -> None:
    from dataclasses import replace as _replace

    from zicato.proposer.best_of_n import _heuristic_best_index

    # Candidate 0: no expected movements, SMALLER diff. Candidate 1: carries
    # expected movements, larger diff. Both equally grounded (no patterns).
    bare = _experiment(core_idea="bare", mutation_id="router__sp", new_content="x")
    predicted = _prediction_bearing("predicted", diff_pad="pad-pad-pad")
    candidates = [bare, predicted]

    # Uncalibrated lineage (no graded history): the term is inert — the
    # smaller diff wins as before.
    ctx = _context(_CapturingCriticLLM("0"))
    assert _heuristic_best_index(candidates, ctx) == 0

    # Poorly-calibrated lineage: still inert (guessing is not rewarded).
    ctx_low = _replace(ctx, prior_experiments=(_prior(0.2),))
    assert _heuristic_best_index(candidates, ctx_low) == 0

    # Well-calibrated lineage: the prediction-bearing candidate ranks ahead
    # of the smaller bare edit (advisory ordering, applied before diff size).
    ctx_high = _replace(ctx, prior_experiments=(_prior(0.9), _prior(0.7)))
    assert _heuristic_best_index(candidates, ctx_high) == 1


def test_recent_prediction_accuracy_means_graded_entries_only() -> None:
    from dataclasses import replace as _replace

    from zicato.proposer.best_of_n import recent_prediction_accuracy

    ctx = _context(_CapturingCriticLLM("0"))
    assert recent_prediction_accuracy(ctx) is None
    ctx2 = _replace(ctx, prior_experiments=(_prior(1.0), _prior(None), _prior(0.5)))
    assert recent_prediction_accuracy(ctx2) == 0.75


@pytest.mark.asyncio
async def test_critic_prompt_carries_calibration_note_when_calibrated() -> None:
    from dataclasses import replace as _replace

    critic = _CapturingCriticLLM("1")
    inner = _ScriptedInnerAgent(
        [
            _experiment(core_idea="a", mutation_id="router__sp", new_content="x"),
            _prediction_bearing("b"),
        ]
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=2))
    ctx = _replace(_context(critic), prior_experiments=(_prior(0.9),))
    await agent.propose(ctx)
    assert len(critic.user_prompts) == 1
    assert "predictions have mostly borne out" in critic.user_prompts[0]

    # Uncalibrated lineage: no note.
    critic2 = _CapturingCriticLLM("1")
    inner2 = _ScriptedInnerAgent(
        [
            _experiment(core_idea="a", mutation_id="router__sp", new_content="x"),
            _prediction_bearing("b"),
        ]
    )
    agent2 = BestOfNProposerAgent(inner=inner2, config=ProposerQualityConfig(best_of_n=2))
    await agent2.propose(_context(critic2))
    assert "predictions have mostly borne out" not in critic2.user_prompts[0]
