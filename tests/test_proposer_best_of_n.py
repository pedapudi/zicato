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
    CandidateScreenResult,
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
    context, composed with a per-(slot, round) STRATEGY line; the caller's own
    context is never mutated."""
    from zicato.proposer.best_of_n import EDIT_CLASS_HINTS
    from zicato.proposer.hints import strategy_for_slot

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
    # Each composed hint is the edit-class hint, a newline, then the slot's
    # strategy framing — deterministic per (slot, generation_id="v1").
    expected = [f"{EDIT_CLASS_HINTS[i]}\n{strategy_for_slot(i, 'v1')}" for i in range(3)]
    assert inner.hints == expected
    # The edit-class hints stay distinct (the first axis), and the whole
    # composed hints are distinct too.
    assert len({h.split(chr(10))[0] for h in inner.hints}) == 3
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
# Chosen-candidate tree alignment — the mounted child tree must match the
# selection, not the last-sampled slate slot
# --------------------------------------------------------------------------


class _RecordingValidator:
    """A ``validate_experiment`` double recording every candidate it derives.

    In production this is the post-apply hook
    (:func:`zicato.evolve.round.build_post_apply_validator`): each call
    re-derives the SAME fixed child snapshot from the candidate's patches,
    clearing the prior attempt's tree. ``findings_script`` scripts the
    return of successive calls (empty list = validated cleanly); once the
    script is exhausted every further call validates cleanly.
    """

    def __init__(self, findings_script: list[list[str]] | None = None) -> None:
        self.calls: list[Experiment] = []
        self._script = list(findings_script or [])

    async def __call__(self, candidate: Experiment) -> list[str]:
        self.calls.append(candidate)
        if self._script:
            return self._script.pop(0)
        return []


def _slate3() -> list[Experiment]:
    return [
        _experiment(core_idea="zero", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="one", mutation_id="writer__sp", new_content="b"),
        _experiment(core_idea="two", mutation_id="router__sp", new_content="c"),
    ]


@pytest.mark.asyncio
async def test_chosen_earlier_candidate_rederives_its_child_tree() -> None:
    """WS-CONC: the chosen candidate is mounted into the real next_id tree by
    one unconditional final derive after selection, so the on-disk child tree
    matches the experiment the caller mounts + persists — here the pick is an
    EARLIER slate slot, but the mount runs the same either way."""
    from dataclasses import replace as _replace

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    hook = _RecordingValidator()
    events: list[tuple[str, dict]] = []
    ctx = _replace(
        _context(_CapturingCriticLLM("0")),
        validate_experiment=hook,
        round_event_emitter=lambda t, f: events.append((t, f)),
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)
    assert out is candidates[0]
    # Exactly one post-selection re-derive, for the chosen candidate. (The
    # scripted inner agent does not itself call the hook; the production
    # inner engine calls it once per attempt BEFORE selection.)
    assert hook.calls == [candidates[0]]
    selected = dict(events)["critique_selected"]
    assert selected == {"index": 0, "reason": "critique"}


@pytest.mark.asyncio
async def test_chosen_last_candidate_is_still_mounted() -> None:
    """WS-CONC: every slot validated into its OWN scratch tree (now gone), so
    the chosen candidate is UNCONDITIONALLY derived into the real next_id
    once after selection — even when the pick is the last-sampled slot."""
    from dataclasses import replace as _replace

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    hook = _RecordingValidator()
    ctx = _replace(_context(_CapturingCriticLLM("2")), validate_experiment=hook)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)
    assert out is candidates[2]
    # Exactly one final mount, for the chosen candidate — no conditional skip.
    assert hook.calls == [candidates[2]]


@pytest.mark.asyncio
async def test_no_validation_hook_keeps_selection_untouched() -> None:
    """Without a validate hook there is no derived tree to align — the
    selection is returned as-is (the pre-hook caller contract)."""
    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(_context(_CapturingCriticLLM("1")))
    assert out is candidates[1]


@pytest.mark.asyncio
async def test_mount_failure_raises_proposer_error() -> None:
    """WS-CONC: the chosen candidate validated cleanly in its scratch tree
    moments ago, so a final-mount failure is unexpected — and with NO shared
    last-validated tree to fall back to, it surfaces the standard
    ProposerError every call site already handles."""
    from dataclasses import replace as _replace

    from zicato.proposer.proposer import ProposerError

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    hook = _RecordingValidator(findings_script=[["parent tree changed underneath the slate"]])
    ctx = _replace(_context(_CapturingCriticLLM("0")), validate_experiment=hook)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    with pytest.raises(ProposerError) as exc_info:
        await agent.propose(ctx)
    assert any("parent tree changed" in a for a in exc_info.value.attempts)
    # Only the chosen candidate was mounted — there is no second candidate to
    # fall back to (the shared last-validated tree is gone).
    assert hook.calls == [candidates[0]]


@pytest.mark.asyncio
async def test_mount_failure_does_not_attempt_a_fallback() -> None:
    """The retired ``_align_child_tree`` fell back to the last-validated
    candidate on a re-derive failure; the unconditional final mount has no
    shared tree to fall back to, so it re-validates ONLY the chosen candidate
    and surfaces its finding — never a second candidate's."""
    from dataclasses import replace as _replace

    from zicato.proposer.proposer import ProposerError

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    hook = _RecordingValidator(findings_script=[["chosen failed"], ["would-be fallback"]])
    ctx = _replace(_context(_CapturingCriticLLM("0")), validate_experiment=hook)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    with pytest.raises(ProposerError) as exc_info:
        await agent.propose(ctx)
    attempts = list(exc_info.value.attempts)
    assert any("chosen failed" in a for a in attempts)
    assert not any("would-be fallback" in a for a in attempts)
    assert hook.calls == [candidates[0]]


@pytest.mark.asyncio
async def test_mount_hook_raising_is_folded_into_the_error() -> None:
    """A mount hook that RAISES (doubly unexpected — its contract is to return
    findings) is folded into a finding and surfaces the standard ProposerError
    — no fallback candidate is tried."""
    from dataclasses import replace as _replace

    from zicato.proposer.proposer import ProposerError

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    calls: list[Experiment] = []

    async def _hook(candidate: Experiment) -> list[str]:
        calls.append(candidate)
        raise RuntimeError("boom")

    ctx = _replace(_context(_CapturingCriticLLM("0")), validate_experiment=_hook)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    with pytest.raises(ProposerError) as exc_info:
        await agent.propose(ctx)
    assert any("boom" in a for a in exc_info.value.attempts)
    assert calls == [candidates[0]]


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


# --------------------------------------------------------------------------
# Candidate screening (tryouts) — veto filter, tiebreak feeds, guardrails
# --------------------------------------------------------------------------


def _screen_result(
    *,
    vetoed: bool = False,
    scalar: float | None = None,
    confirmed: bool = False,
    passes: int = 2,
) -> CandidateScreenResult:
    return CandidateScreenResult(
        vetoed=vetoed,
        reason=("vetoed: panel 2" if vetoed else "clear: panel 2"),
        scalar=scalar,
        entries_screened=2,
        baseline_passes=2,
        candidate_passes=passes,
        confirmed=confirmed,
    )


class _ScriptedScreen:
    """A screen-runner double: returns scripted results, counts calls."""

    def __init__(self, results: list[CandidateScreenResult]) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, candidates: object) -> list[CandidateScreenResult]:
        self.calls += 1
        return list(self.results)


class _RaisingScreen:
    async def __call__(self, candidates: object) -> list[CandidateScreenResult]:
        raise RuntimeError("screen infrastructure exploded")


class _SequencedScreen:
    """Per-call scripted screen results: call ``k`` returns ``script[k]``.

    Unlike :class:`_ScriptedScreen` (which replays ONE result list on
    every call), this double scripts EACH call independently — needed by
    the revise tests, where the slate screen and the replacement screen
    return differently-sized result lists.
    """

    def __init__(self, script: list[list[CandidateScreenResult]]) -> None:
        self._script = [list(results) for results in script]
        self.calls = 0

    async def __call__(self, candidates: object) -> list[CandidateScreenResult]:
        idx = self.calls
        self.calls += 1
        return list(self._script[idx])


class _ExhaustibleInnerAgent:
    """Scripted inner agent recording each call's context; raises when spent.

    The revise-path double: the slate consumes the scripted candidates in
    order and any call past the script (the revise re-sample, when the
    test wants it to fail) raises the standard
    :class:`~zicato.proposer.proposer.ProposerError`.
    """

    def __init__(self, candidates: list[Experiment]) -> None:
        self._candidates = list(candidates)
        self.contexts: list[ProposerContext] = []
        self.calls = 0

    async def propose(self, ctx: ProposerContext) -> Experiment:
        from zicato.proposer.proposer import ProposerError

        self.contexts.append(ctx)
        idx = self.calls
        self.calls += 1
        if idx >= len(self._candidates):
            raise ProposerError([f"inner agent exhausted at call {idx}"])
        return self._candidates[idx]


def _screened_context(aux: object, screen: object, **overrides: object) -> ProposerContext:
    from dataclasses import replace as _replace

    return _replace(_context(aux), screen_candidates=screen, **overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_vetoed_candidate_is_never_selected() -> None:
    cand0 = _experiment(core_idea="broken", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="fine", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    # The critic would pick 0 — but 0 is vetoed, so it must never win.
    critic = _CapturingCriticLLM("0")
    screen = _ScriptedScreen(
        [_screen_result(vetoed=True, confirmed=True, passes=0), _screen_result(scalar=1.0)]
    )
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(_screened_context(critic, screen))
    assert out is cand1
    assert screen.calls == 1
    # A single survivor needs no critique call at all.
    assert critic.user_prompts == []


@pytest.mark.asyncio
async def test_sole_survivor_mode_string_and_event_ordering() -> None:
    events: list[tuple[str, dict]] = []
    cand0 = _experiment(core_idea="broken", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="fine", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    screen = _ScriptedScreen(
        [_screen_result(vetoed=True, confirmed=True, passes=0), _screen_result(scalar=1.0)]
    )
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(
        _CapturingCriticLLM("0"), screen, round_event_emitter=lambda t, f: events.append((t, f))
    )
    await agent.propose(ctx)
    # candidate_sampled xN, then candidate_screened xN, then critique_selected.
    assert [t for t, _ in events] == [
        "candidate_sampled",
        "candidate_sampled",
        "candidate_screened",
        "candidate_screened",
        "critique_selected",
    ]
    screened = [f for t, f in events if t == "candidate_screened"]
    assert screened[0]["vetoed"] is True
    assert screened[0]["confirmed"] is True
    assert screened[1]["vetoed"] is False
    # Counts-only summary — the seam never carries entry ids.
    assert set(screened[0]["screen_summary"]) == {
        "entries_screened",
        "baseline_passes",
        "candidate_passes",
        "reason",
    }
    selected = dict(events)["critique_selected"]
    assert selected == {"index": 1, "reason": "screen_sole_survivor"}


@pytest.mark.asyncio
async def test_all_vetoed_slate_degrades_to_critic_over_all() -> None:
    """An all-vetoed slate whose revise produces NO replacement (the inner
    proposer is exhausted) degrades exactly as before the revise existed:
    critic-over-all with the ``screen_all_vetoed`` mode prefix."""
    events: list[tuple[str, dict]] = []
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ExhaustibleInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("1")
    screen = _ScriptedScreen(
        [
            _screen_result(vetoed=True, confirmed=True, passes=0),
            _screen_result(vetoed=True, passes=0),
        ]
    )
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(critic, screen, round_event_emitter=lambda t, f: events.append((t, f)))
    out = await agent.propose(ctx)
    assert out is cand1  # the critic still chose — the step never empties
    assert len(critic.user_prompts) == 1
    assert inner.calls == 3  # 2 slate samples + the one (failed) revise
    assert dict(events)["critique_selected"]["reason"] == "screen_all_vetoed:critique"


@pytest.mark.asyncio
async def test_all_vetoed_heuristic_mode_string() -> None:
    events: list[tuple[str, dict]] = []
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="bb")
    inner = _ExhaustibleInnerAgent([cand0, cand1])
    screen = _ScriptedScreen(
        [_screen_result(vetoed=True, passes=0), _screen_result(vetoed=True, passes=0)]
    )
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2),
    )
    ctx = _screened_context(
        _CapturingCriticLLM("0"), screen, round_event_emitter=lambda t, f: events.append((t, f))
    )
    out = await agent.propose(ctx)
    assert out is cand0  # smaller diff wins under the heuristic
    assert dict(events)["critique_selected"]["reason"] == "screen_all_vetoed:heuristic"


@pytest.mark.asyncio
async def test_raising_screen_proceeds_unscreened() -> None:
    events: list[tuple[str, dict]] = []
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(
        critic, _RaisingScreen(), round_event_emitter=lambda t, f: events.append((t, f))
    )
    out = await agent.propose(ctx)
    # Screening must never fail a propose: the critic selected as if
    # unscreened, and no candidate_screened event was emitted.
    assert out is cand0
    assert dict(events)["critique_selected"]["reason"] == "critique"
    assert "candidate_screened" not in [t for t, _ in events]
    # The critic prompt carries no screen block on the unscreened path.
    assert "## Screen measurements" not in critic.user_prompts[0]


@pytest.mark.asyncio
async def test_malformed_screen_result_count_proceeds_unscreened() -> None:
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")
    screen = _ScriptedScreen([_screen_result()])  # one result for two candidates
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(_screened_context(critic, screen))
    assert out is cand0


@pytest.mark.asyncio
async def test_screen_scalar_is_penultimate_heuristic_tiebreak() -> None:
    # Equal grounding / calibration / DIFF SIZE: the panel scalar breaks the
    # tie (lower = better) ahead of the stable index...
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="xx")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="yy")
    inner = _ScriptedInnerAgent([cand0, cand1])
    screen = _ScriptedScreen([_screen_result(scalar=5.0), _screen_result(scalar=0.5)])
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2),
    )
    out = await agent.propose(_screened_context(_CapturingCriticLLM("0"), screen))
    assert out is cand1

    # ...but never outranks the diff-size term: the smaller edit still wins
    # even with the worse panel scalar (the screen advises, it cannot rank).
    small = _experiment(core_idea="small", mutation_id="router__sp", new_content="x")
    large = _experiment(core_idea="large", mutation_id="writer__sp", new_content="y" * 50)
    inner2 = _ScriptedInnerAgent([small, large])
    screen2 = _ScriptedScreen([_screen_result(scalar=9.0), _screen_result(scalar=0.1)])
    agent2 = BestOfNProposerAgent(
        inner=inner2,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2),
    )
    out2 = await agent2.propose(_screened_context(_CapturingCriticLLM("0"), screen2))
    assert out2 is small

    # A None (no-signal) scalar sorts after every measured one.
    inner3 = _ScriptedInnerAgent([cand0, cand1])
    screen3 = _ScriptedScreen([_screen_result(scalar=None), _screen_result(scalar=7.0)])
    agent3 = BestOfNProposerAgent(
        inner=inner3,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2),
    )
    out3 = await agent3.propose(_screened_context(_CapturingCriticLLM("0"), screen3))
    assert out3 is cand1


@pytest.mark.asyncio
async def test_veto_only_suppresses_both_tiebreak_feeds() -> None:
    # Heuristic feed: with screen_veto_only the panel scalar is ignored —
    # the stable index decides the (otherwise equal) tie again.
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="xx")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="yy")
    inner = _ScriptedInnerAgent([cand0, cand1])
    screen = _ScriptedScreen([_screen_result(scalar=5.0), _screen_result(scalar=0.5)])
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(
            best_of_n=2, critique_enabled=False, screen_entries=2, screen_veto_only=True
        ),
    )
    out = await agent.propose(_screened_context(_CapturingCriticLLM("0"), screen))
    assert out is cand0

    # Critic feed: no "## Screen measurements" block reaches the prompt.
    inner2 = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("0")
    screen2 = _ScriptedScreen([_screen_result(scalar=5.0), _screen_result(scalar=0.5)])
    agent2 = BestOfNProposerAgent(
        inner=inner2,
        config=ProposerQualityConfig(best_of_n=2, screen_entries=2, screen_veto_only=True),
    )
    await agent2.propose(_screened_context(critic, screen2))
    assert len(critic.user_prompts) == 1
    assert "## Screen measurements" not in critic.user_prompts[0]

    # ...while the default (veto_only False) feeds the counts-only block.
    inner3 = _ScriptedInnerAgent([cand0, cand1])
    critic3 = _CapturingCriticLLM("0")
    screen3 = _ScriptedScreen([_screen_result(scalar=5.0), _screen_result(scalar=0.5)])
    agent3 = BestOfNProposerAgent(
        inner=inner3, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    await agent3.propose(_screened_context(critic3, screen3))
    prompt = critic3.user_prompts[0]
    assert "## Screen measurements" in prompt
    assert "not a ranking" in prompt
    # Counts only — no raw scalar leaks into the critic prompt.
    assert "5.0" not in prompt
    assert "0.5" not in prompt


@pytest.mark.asyncio
async def test_veto_only_still_vetoes() -> None:
    cand0 = _experiment(core_idea="broken", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="fine", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    screen = _ScriptedScreen(
        [_screen_result(vetoed=True, confirmed=True, passes=0), _screen_result()]
    )
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, screen_entries=2, screen_veto_only=True),
    )
    out = await agent.propose(_screened_context(_CapturingCriticLLM("0"), screen))
    assert out is cand1


# --------------------------------------------------------------------------
# Screen-informed revise (WS-R) — one bounded re-sample on an all-vetoed slate
# --------------------------------------------------------------------------


def _revise_fixture(
    *,
    replacement_result: CandidateScreenResult | list[CandidateScreenResult] | None,
    critic: str = "1",
    critique_enabled: bool = True,
) -> tuple[list[Experiment], _ExhaustibleInnerAgent, _SequencedScreen, _CapturingCriticLLM]:
    """An all-vetoed two-candidate slate plus one scripted revise replacement.

    ``replacement_result`` scripts the replacement's screen call — a single
    result, a full (malformed-size) list, or ``None`` for no second call
    scripted (the revise-unavailable tests never reach it).
    """
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    replacement = _experiment(core_idea="revised", mutation_id="writer__sp", new_content="c")
    slate_call = [
        _screen_result(vetoed=True, confirmed=True, passes=0),
        _screen_result(vetoed=True, passes=0),
    ]
    script: list[list[CandidateScreenResult]] = [slate_call]
    if isinstance(replacement_result, list):
        script.append(replacement_result)
    elif replacement_result is not None:
        script.append([replacement_result])
    inner = _ExhaustibleInnerAgent([cand0, cand1, replacement])
    return [cand0, cand1, replacement], inner, _SequencedScreen(script), _CapturingCriticLLM(critic)


@pytest.mark.asyncio
async def test_all_vetoed_revise_survivor_is_chosen() -> None:
    candidates, inner, screen, critic = _revise_fixture(replacement_result=_screen_result())
    events: list[tuple[str, dict]] = []
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(critic, screen, round_event_emitter=lambda t, f: events.append((t, f)))
    out = await agent.propose(ctx)
    # The surviving replacement IS the choice — no critique call needed.
    assert out is candidates[2]
    assert critic.user_prompts == []
    assert inner.calls == 3  # 2 slate samples + exactly ONE revise
    assert screen.calls == 2  # the slate, then the replacement (guarded)
    selected = dict(events)["critique_selected"]
    assert selected == {"index": 2, "reason": "screen_revise_survivor"}


@pytest.mark.asyncio
async def test_revise_also_vetoed_falls_back_with_distinct_mode_string() -> None:
    candidates, inner, screen, critic = _revise_fixture(
        replacement_result=_screen_result(vetoed=True, passes=0)
    )
    events: list[tuple[str, dict]] = []
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(critic, screen, round_event_emitter=lambda t, f: events.append((t, f)))
    out = await agent.propose(ctx)
    # The critic chose over the ORIGINAL slate; the vetoed replacement is
    # never returned, and the budget is exactly one revise (no recursion).
    assert out is candidates[1]
    assert inner.calls == 3
    assert screen.calls == 2
    assert dict(events)["critique_selected"]["reason"] == "screen_all_vetoed_after_revise:critique"


@pytest.mark.asyncio
async def test_revise_feedback_carries_counts_only() -> None:
    _, inner, screen, critic = _revise_fixture(replacement_result=_screen_result())
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    await agent.propose(_screened_context(critic, screen))
    # The two slate samples carry no revise feedback; the revise re-sample
    # carries the counts-only veto summary through the repair channel.
    assert [c.revise_feedback for c in inner.contexts[:2]] == ["", ""]
    feedback = inner.contexts[2].revise_feedback
    assert "VETOED every sampled candidate" in feedback
    assert "candidate 0: vetoed: panel 2" in feedback
    assert "candidate 1: vetoed: panel 2" in feedback
    # Counts only — the restricted-visibility envelope: no board-entry
    # identity of any shape reaches the feedback string (the reason
    # strings are counts-only by CandidateScreenResult contract, and the
    # rest of the string is static instruction text).
    assert "entry" not in feedback.lower()
    # The revise re-sample keeps the same restricted context otherwise.
    assert inner.contexts[2].restrict_visibility is True


@pytest.mark.asyncio
async def test_revise_round_log_ordering_and_markers() -> None:
    _, inner, screen, critic = _revise_fixture(replacement_result=_screen_result())
    events: list[tuple[str, dict]] = []
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(critic, screen, round_event_emitter=lambda t, f: events.append((t, f)))
    await agent.propose(ctx)
    # Slate samples, slate screens, THEN the revise sample + its screen,
    # then the selection — the existing vocabulary end to end.
    assert [t for t, _ in events] == [
        "candidate_sampled",
        "candidate_sampled",
        "candidate_screened",
        "candidate_screened",
        "candidate_sampled",
        "candidate_screened",
        "critique_selected",
    ]
    sampled = [f for t, f in events if t == "candidate_sampled"]
    assert sampled[0] == {"i": 0, "n": 2}
    assert sampled[1] == {"i": 1, "n": 2}
    assert sampled[2] == {"i": 2, "n": 2, "revise": True}
    screened = [f for t, f in events if t == "candidate_screened"]
    assert [f["revise"] for f in screened] == [False, False, True]
    assert screened[2]["index"] == 2
    assert screened[2]["vetoed"] is False
    assert set(screened[2]["screen_summary"]) == {
        "entries_screened",
        "baseline_passes",
        "candidate_passes",
        "reason",
    }


@pytest.mark.asyncio
async def test_screening_off_never_revises_byte_identical() -> None:
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ExhaustibleInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("1")
    events: list[tuple[str, dict]] = []
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    from dataclasses import replace as _replace

    out = await agent.propose(
        _replace(_context(critic), round_event_emitter=lambda t, f: events.append((t, f)))
    )
    # No screen runner on the context ⇒ no screen, no revise: exactly the
    # N slate samples, no revise feedback anywhere, and the pre-revise
    # event stream byte-for-byte (no revise markers, no extra events).
    assert out is cand1
    assert inner.calls == 2
    assert all(c.revise_feedback == "" for c in inner.contexts)
    assert events == [
        ("candidate_sampled", {"i": 0, "n": 2}),
        ("candidate_sampled", {"i": 1, "n": 2}),
        ("critique_selected", {"index": 1, "reason": "critique"}),
    ]


@pytest.mark.asyncio
async def test_revise_malformed_screen_result_treated_as_unscreened_survivor() -> None:
    # The replacement's screen returns a malformed (2-for-1) result list —
    # the guarded degrade treats the replacement as unscreened and chooses
    # it (the alternative is a known-vetoed original).
    candidates, inner, screen, critic = _revise_fixture(
        replacement_result=[_screen_result(), _screen_result()]
    )
    events: list[tuple[str, dict]] = []
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    ctx = _screened_context(critic, screen, round_event_emitter=lambda t, f: events.append((t, f)))
    out = await agent.propose(ctx)
    assert out is candidates[2]
    assert dict(events)["critique_selected"]["reason"] == "screen_revise_survivor"
    # The malformed screen emitted no candidate_screened event for the
    # replacement (its verdict is unknown, not clear).
    assert [f.get("revise") for t, f in events if t == "candidate_screened"] == [False, False]


@pytest.mark.asyncio
async def test_revise_survivor_is_mounted() -> None:
    # WS-CONC: the chosen revise replacement (the last candidate) is still
    # mounted into next_id by the one unconditional final derive — its own
    # scratch validation tree is gone.
    _, inner, screen, critic = _revise_fixture(replacement_result=_screen_result())
    hook = _RecordingValidator()
    from dataclasses import replace as _replace

    ctx = _replace(_screened_context(critic, screen), validate_experiment=hook)
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(ctx)
    assert hook.calls == [out]


@pytest.mark.asyncio
async def test_revise_fallback_mounts_the_chosen_original() -> None:
    # Revise-also-vetoed: the selection returns to an ORIGINAL candidate. The
    # one unconditional final derive mounts exactly that chosen original — the
    # replacement validated into its OWN scratch tree, so nothing shared was
    # clobbered.
    candidates, inner, screen, critic = _revise_fixture(
        replacement_result=_screen_result(vetoed=True, passes=0), critic="0"
    )
    hook = _RecordingValidator()
    from dataclasses import replace as _replace

    ctx = _replace(_screened_context(critic, screen), validate_experiment=hook)
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(ctx)
    assert out is candidates[0]
    assert hook.calls == [candidates[0]]


@pytest.mark.asyncio
async def test_failed_revise_needs_no_tree_restore() -> None:
    # WS-CONC: the revise propose fails, but the replacement validated into
    # its OWN throwaway scratch tree — there is no shared tree to restore. The
    # step degrades to critic-over-all and mounts ONLY the chosen original.
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ExhaustibleInnerAgent([cand0, cand1])  # the revise call raises
    screen = _ScriptedScreen(
        [_screen_result(vetoed=True, passes=0), _screen_result(vetoed=True, passes=0)]
    )
    hook = _RecordingValidator()
    from dataclasses import replace as _replace

    ctx = _replace(_screened_context(_CapturingCriticLLM("0"), screen), validate_experiment=hook)
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(ctx)
    assert out is cand0
    # No restore — only the mount of the chosen original.
    assert hook.calls == [cand0]


@pytest.mark.asyncio
async def test_no_screen_runner_on_context_screens_nothing() -> None:
    # A context without a screen runner (every contract that does not opt
    # in) never constructs screen machinery — the selection is the plain
    # critic path even when the config carries screen knobs.
    cand0 = _experiment(core_idea="a", mutation_id="router__sp", new_content="a")
    cand1 = _experiment(core_idea="b", mutation_id="writer__sp", new_content="b")
    inner = _ScriptedInnerAgent([cand0, cand1])
    critic = _CapturingCriticLLM("1")
    agent = BestOfNProposerAgent(
        inner=inner, config=ProposerQualityConfig(best_of_n=2, screen_entries=2)
    )
    out = await agent.propose(_context(critic))
    assert out is cand1
    assert "## Screen measurements" not in critic.user_prompts[0]


# --------------------------------------------------------------------------
# WS-REC — the recombination slot
# --------------------------------------------------------------------------


def _rec_pair():
    from zicato.proposer.recombine import RecombinationPair

    def _p(pid: str, mid: str, content: str) -> Patch:
        return Patch(
            id=pid,
            mutation_id=mid,
            op="replace",
            new_content=content,
            new_numeric=None,
            new_enum=None,
            rationale="r",
        )

    return RecombinationPair(
        a_generation_id="v1",
        b_generation_id="v2",
        a_patches=(_p("pa", "router__sp", "fix-a"),),
        b_patches=(_p("pb", "writer__sp", "fix-b"),),
        a_core_idea="fix the router",
        b_core_idea="fix the writer",
        a_improved_count=1,
        b_improved_count=1,
        combined_improved_count=2,
        combined_regressed_count=0,
    )


@pytest.mark.asyncio
async def test_recombination_mint_happy_path() -> None:
    """A pair on the context: the LAST slot mints (no inner call), the
    non-vetoed mint is CHOSEN with selection_mode="recombined" (no critic
    call), and the round log carries the recombined marker."""
    from dataclasses import replace as _replace

    candidates = _slate3()[:2]
    inner = _ScriptedInnerAgent(candidates)
    critic = _CapturingCriticLLM("0")
    events: list[tuple[str, dict]] = []
    ctx = _replace(
        _context(critic),
        recombine_pair=_rec_pair(),
        round_event_emitter=lambda t, f: events.append((t, f)),
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)

    # The mint replaced the LAST slot's inner propose — n−1 calls exactly.
    assert inner.calls == 2
    # The returned experiment IS the mint: union patches, machine provenance.
    assert out.recombined_from == ("v1", "v2")
    assert sorted(p.mutation_id for p in out.patches) == ["router__sp", "writer__sp"]
    assert out.hypothesis.core_idea.startswith("[recombined]")
    # No critic call was made (the selection short-circuit).
    assert critic.user_prompts == []
    # Round-log trace: two ordinary samples, one recombined, the mode.
    sampled = [f for t, f in events if t == "candidate_sampled"]
    assert [s.get("recombined", False) for s in sampled] == [False, False, True]
    selected = dict(events)["critique_selected"]
    assert selected == {"index": 2, "reason": "recombined"}


@pytest.mark.asyncio
async def test_recombination_mint_validation_failure_degrades_to_fresh_sample() -> None:
    """A mint the validate hook rejects DEGRADES the slot to a normal fresh
    sample: the full slate budget is spent, no recombined candidate exists,
    and the ordinary selection runs."""
    from dataclasses import replace as _replace

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)

    class _MintRejectingValidator(_RecordingValidator):
        async def __call__(self, candidate: Experiment) -> list[str]:
            await super().__call__(candidate)
            if candidate.recombined_from:
                return ["mint no longer derives against the parent tree"]
            return []

    hook = _MintRejectingValidator()
    events: list[tuple[str, dict]] = []
    ctx = _replace(
        _context(_CapturingCriticLLM("2")),
        recombine_pair=_rec_pair(),
        validate_experiment=hook,
        round_event_emitter=lambda t, f: events.append((t, f)),
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)

    assert inner.calls == 3  # the degrade re-spent the slot on the inner agent
    assert out is candidates[2]
    assert out.recombined_from == ()
    sampled = [f for t, f in events if t == "candidate_sampled"]
    assert [s.get("recombined", False) for s in sampled] == [False, False, False]
    assert dict(events)["critique_selected"]["reason"] != "recombined"


@pytest.mark.asyncio
async def test_recombination_forbidden_id_degrades_to_fresh_sample() -> None:
    """Defense in depth: a mint touching a NOW-forbidden mutation id never
    enters the slate — the slot degrades to a normal sample."""
    from dataclasses import replace as _replace

    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    ctx = _replace(
        _context(_CapturingCriticLLM("2")),
        recombine_pair=_rec_pair(),
        forbidden_ids=("writer__sp",),
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)
    assert inner.calls == 3
    assert out.recombined_from == ()


@pytest.mark.asyncio
async def test_llm_merge_mode_is_behaviorally_inert_without_a_pair() -> None:
    """The accept-and-inert pin: recombine_merge="llm" with NO pair on the
    context (recombine off, or no eligible pair) behaves byte-identically
    to the default — every slot samples normally, no merge call, no
    provenance."""
    from dataclasses import replace as _replace

    candidates = _slate3()
    critic = _CapturingCriticLLM("2")
    inner = _ScriptedInnerAgent(candidates)
    ctx = _replace(_context(critic), recombine_pair=None)
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=3, recombine_merge="llm"),
    )
    out = await agent.propose(ctx)
    assert inner.calls == 3  # all slots sampled — no slot was replaced
    assert out.recombined_from == ()
    # The critic ran the ordinary selection (no recombined short-circuit).
    assert critic.user_prompts != []


@pytest.mark.asyncio
async def test_vetoed_mint_stays_an_ordinary_slate_member() -> None:
    """A screen-VETOED mint takes no short-circuit: the ordinary selection
    runs over the survivors and the mode string is not "recombined"."""
    from dataclasses import replace as _replace

    candidates = _slate3()[:2]
    inner = _ScriptedInnerAgent(candidates)

    async def _screen(slate):
        results = []
        for exp in slate:
            vetoed = bool(exp.recombined_from)
            results.append(
                CandidateScreenResult(
                    vetoed=vetoed,
                    reason="vetoed: panel 2, budget-aborts 1" if vetoed else "clear: panel 2",
                    scalar=1.0,
                    entries_screened=2,
                    baseline_passes=2,
                    candidate_passes=0 if vetoed else 2,
                    confirmed=False,
                )
            )
        return results

    events: list[tuple[str, dict]] = []
    ctx = _replace(
        _context(_CapturingCriticLLM("0")),
        recombine_pair=_rec_pair(),
        screen_candidates=_screen,
        round_event_emitter=lambda t, f: events.append((t, f)),
    )
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=3, critique_enabled=False, screen_entries=2),
    )
    out = await agent.propose(ctx)

    assert out.recombined_from == ()  # the mint was NOT chosen
    assert out in candidates
    selected = dict(events)["critique_selected"]
    assert selected["reason"] != "recombined"
    assert selected["index"] != 2


@pytest.mark.asyncio
async def test_chosen_mint_is_scratch_validated_then_mounted() -> None:
    """WS-CONC: the mint validates during minting in its OWN scratch tree,
    then the chosen mint is mounted into next_id by the one unconditional
    final derive — two hook calls, both the recombined mint (there is no
    shared last-validated tree to skip against)."""
    from dataclasses import replace as _replace

    candidates = _slate3()[:2]
    inner = _ScriptedInnerAgent(candidates)
    hook = _RecordingValidator()
    ctx = _replace(
        _context(_CapturingCriticLLM("0")),
        recombine_pair=_rec_pair(),
        validate_experiment=hook,
    )
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(ctx)
    assert out.recombined_from == ("v1", "v2")
    # Two calls: the mint's scratch validation, then the final mount of the
    # chosen mint — both the recombined experiment.
    assert len(hook.calls) == 2
    assert all(c.recombined_from == ("v1", "v2") for c in hook.calls)


@pytest.mark.asyncio
async def test_no_pair_on_context_is_byte_identical() -> None:
    """Without a pair (every knob-off round) the slot loop is unchanged."""
    candidates = _slate3()
    inner = _ScriptedInnerAgent(candidates)
    agent = BestOfNProposerAgent(inner=inner, config=ProposerQualityConfig(best_of_n=3))
    out = await agent.propose(_context(_CapturingCriticLLM("1")))
    assert inner.calls == 3
    assert out is candidates[1]


def test_field_threads_the_pair_to_slot_zero_only() -> None:
    """The field path's slot-0-only rule: identical mints across the field
    would collapse into field-diversity soft-rejects, so exactly one slot
    per round may carry the pair."""
    from zicato.orchestrator import _recombine_pair_for_slot

    pair = _rec_pair()
    assert _recombine_pair_for_slot(pair, 0) is pair
    assert _recombine_pair_for_slot(pair, 1) is None
    assert _recombine_pair_for_slot(pair, 3) is None
    assert _recombine_pair_for_slot(None, 0) is None


# --------------------------------------------------------------------------
# WS-ENS — ensemble proposer roles (breadth = sampling, depth = critique/revise)
# --------------------------------------------------------------------------


async def _plain_aux(system: str, user: str, model: str) -> str:
    """A breadth double: a distinct callable the fake inner never invokes.

    The wiring proof for SAMPLING is that the inner agent RECEIVES this
    object on ``ctx.aux_call_llm`` (the fake inner records the context but
    never calls the callable), so identity — not a call count — is the seam.
    """
    del system, user, model
    return "unused"


@pytest.mark.asyncio
async def test_ens_sampling_uses_breadth_critique_uses_depth() -> None:
    """Slate SAMPLING runs on breadth (N times); the CRITIQUE call runs on
    depth — and both are distinct from the context's base auxiliary, proving
    the wrapper actually swapped ``ctx.aux_call_llm`` per call class."""
    candidates = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
        _experiment(core_idea="c", mutation_id="router__sp", new_content="c"),
    ]
    inner = _ExhaustibleInnerAgent(candidates)
    breadth = _plain_aux
    depth = _CapturingCriticLLM("0")  # the critic double: counts + records
    base_aux = _CapturingCriticLLM("2")  # deliberately different from both roles
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=3),
        breadth_call_llm=breadth,
        depth_call_llm=depth,
    )
    out = await agent.propose(_context(base_aux))

    # SAMPLING: the inner was handed the breadth callable on every slate slot.
    assert inner.calls == 3
    assert [c.aux_call_llm for c in inner.contexts] == [breadth, breadth, breadth]
    # CRITIQUE: exactly one depth call; the base auxiliary was NEVER the critic.
    assert len(depth.system_prompts) == 1
    assert len(base_aux.system_prompts) == 0
    # The critic's pick (index 0) is returned.
    assert out is candidates[0]


@pytest.mark.asyncio
async def test_ens_absent_roles_are_byte_identical_same_callable_object() -> None:
    """With NO roles configured, sampling AND critique run on the SAME object
    the context carries — a counting-double proves the fall-back is the exact
    ``ctx.aux_call_llm`` (byte-identical to the pre-ensemble wrapper)."""
    candidates = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
    ]
    inner = _ExhaustibleInnerAgent(candidates)
    base_aux = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2),
        breadth_call_llm=None,
        depth_call_llm=None,
    )
    await agent.propose(_context(base_aux))

    # Sampling fell back to the base auxiliary — the SAME object, not a copy.
    assert all(c.aux_call_llm is base_aux for c in inner.contexts)
    # Critique fell back to that same object too (one call).
    assert len(base_aux.system_prompts) == 1


@pytest.mark.asyncio
async def test_ens_revise_uses_depth() -> None:
    """The screen-informed REVISE re-sample is a depth pass: its context
    carries the depth callable (and a non-empty revise_feedback), while the
    original slate slots carried breadth."""
    slate = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
        _experiment(core_idea="rev", mutation_id="router__sp", new_content="r"),
    ]
    inner = _ExhaustibleInnerAgent(slate)
    # slate screen (call 0): both vetoed → all-vetoed → one revise; the
    # replacement screen (call 1): clear → the revise is chosen.
    screen = _SequencedScreen(
        [
            [_screen_result(vetoed=True), _screen_result(vetoed=True)],
            [_screen_result(vetoed=False)],
        ]
    )
    breadth = _plain_aux
    depth = _CapturingCriticLLM("0")
    base_aux = _CapturingCriticLLM("0")
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, screen_entries=2),
        breadth_call_llm=breadth,
        depth_call_llm=depth,
    )
    ctx = _screened_context(base_aux, screen)
    await agent.propose(ctx)

    # 2 slate samples on breadth, then the revise re-sample on depth.
    assert inner.calls == 3
    assert inner.contexts[0].aux_call_llm is breadth
    assert inner.contexts[1].aux_call_llm is breadth
    assert inner.contexts[2].aux_call_llm is depth
    # The 3rd call is unmistakably the revise (its repair feedback is seeded).
    assert inner.contexts[2].revise_feedback != ""


@pytest.mark.asyncio
async def test_ens_no_collusion_guard_between_breadth_and_depth() -> None:
    """Breadth and depth may be the IDENTICAL callable — both are
    proposer-side (one trust domain), so no distinctness guard fires and the
    propose step succeeds normally."""
    candidates = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
    ]
    inner = _ScriptedInnerAgent(candidates)
    shared = _CapturingCriticLLM("1")  # the SAME object for both roles
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2),
        breadth_call_llm=shared,
        depth_call_llm=shared,
    )
    # No exception — the guard does not apply between the two proposer roles.
    out = await agent.propose(_context(_CapturingCriticLLM("1")))
    assert out is candidates[1]


def test_ens_wrap_threads_roles_onto_the_wrapper() -> None:
    """`wrap_with_proposer_quality` stores the two role callables on the
    wrapper when best_of_n > 1 (and they are irrelevant on the pass-through)."""
    inner = _ScriptedInnerAgent(
        [_experiment(core_idea="a", mutation_id="router__sp", new_content="x")]
    )
    breadth = _plain_aux
    depth = _CapturingCriticLLM("0")
    wrapped = wrap_with_proposer_quality(
        inner,
        ProposerQualityConfig(best_of_n=3),
        breadth_call_llm=breadth,
        depth_call_llm=depth,
        breadth_model="breadth-model",
        depth_model="depth-model",
    )
    assert isinstance(wrapped, BestOfNProposerAgent)
    assert wrapped.breadth_call_llm is breadth
    assert wrapped.depth_call_llm is depth
    assert wrapped.breadth_model == "breadth-model"
    assert wrapped.depth_model == "depth-model"
    # The best_of_n <= 1 pass-through ignores the roles (no wrapper at all).
    passthrough = wrap_with_proposer_quality(
        inner,
        ProposerQualityConfig(best_of_n=1),
        breadth_call_llm=breadth,
        depth_call_llm=depth,
        breadth_model="breadth-model",
        depth_model="depth-model",
    )
    assert passthrough is inner


@pytest.mark.asyncio
async def test_ens_spec_role_swaps_ctx_model_for_default_proposer() -> None:
    """A spec-configured role carries a MODEL NAME: every sampling slot's inner
    ctx binds the breadth model string, and the revise binds the depth model —
    so the DEFAULT ADK proposer (which reads ``ctx.model``, not the callable)
    honors the role."""
    slate = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
        _experiment(core_idea="rev", mutation_id="router__sp", new_content="r"),
    ]
    inner = _ExhaustibleInnerAgent(slate)
    # slate screen vetoes both → one revise; the replacement screen clears it.
    screen = _SequencedScreen(
        [
            [_screen_result(vetoed=True), _screen_result(vetoed=True)],
            [_screen_result(vetoed=False)],
        ]
    )
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, screen_entries=2),
        breadth_call_llm=_plain_aux,
        depth_call_llm=_CapturingCriticLLM("0"),
        breadth_model="breadth-model",
        depth_model="depth-model",
    )
    await agent.propose(_screened_context(_CapturingCriticLLM("0"), screen))

    assert inner.calls == 3
    # The 2 slate slots carry the breadth model; the revise carries depth.
    assert inner.contexts[0].model == "breadth-model"
    assert inner.contexts[1].model == "breadth-model"
    assert inner.contexts[2].model == "depth-model"


@pytest.mark.asyncio
async def test_ens_callable_only_role_leaves_ctx_model_unchanged() -> None:
    """A callable-only role (no model name — a bare call_llm / test callable)
    swaps ``ctx.aux_call_llm`` but LEAVES ``ctx.model`` at the auxiliary string
    (the documented degrade: it steers only proposers that read
    ``ctx.aux_call_llm``, not the default ADK proposer)."""
    candidates = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
    ]
    inner = _ExhaustibleInnerAgent(candidates)
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2),
        breadth_call_llm=_plain_aux,
        depth_call_llm=_CapturingCriticLLM("0"),
        # breadth_model / depth_model left None — the callable-only path.
    )
    await agent.propose(_context(_CapturingCriticLLM("0")))

    # The callable was swapped, but the model string stayed the context's own.
    assert [c.aux_call_llm for c in inner.contexts] == [_plain_aux, _plain_aux]
    assert all(c.model == "test-model" for c in inner.contexts)


@pytest.mark.asyncio
async def test_ens_absent_roles_leave_ctx_model_byte_identical() -> None:
    """With NO roles configured, every inner ctx keeps the context's own model
    string unchanged — the byte-identical default extends to ``ctx.model``."""
    candidates = [
        _experiment(core_idea="a", mutation_id="router__sp", new_content="a"),
        _experiment(core_idea="b", mutation_id="writer__sp", new_content="b"),
    ]
    inner = _ExhaustibleInnerAgent(candidates)
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2),
    )
    await agent.propose(_context(_CapturingCriticLLM("0")))

    assert all(c.model == "test-model" for c in inner.contexts)
