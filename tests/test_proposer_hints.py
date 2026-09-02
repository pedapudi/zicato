"""Tests for the failure-mode-conditioned slate hints (``zicato.proposer.hints``).

The pure slot→hint mapping is exercised against profiles built by the REAL
renderer (:func:`zicato.proposer.prompts.render_failure_mode_profile` over an
:class:`~zicato.analyzer.outcome_marginals.OutcomeMarginalSummary`) — the
exact string shape the orchestrator threads onto ``ProposerContext`` — so
the parse is proven against the production line shapes, not hand-typed
fixtures. Covers: mapping determinism per dominant mode, exploratory-slot
preservation, the empty-profile byte-identity fallback (hint level AND full
rendered-prompt level), and the wrapper stamping the conditioned hints onto
each slate slot's context.
"""

from __future__ import annotations

import pytest

from tests._proposal_evidence import render_proposal_evidence
from zicato.analyzer.outcome_marginals import OutcomeMarginalSummary
from zicato.core.types import Experiment, ProposerQualityConfig
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import BestOfNProposerAgent
from zicato.proposer.hints import (
    EDIT_CLASS_HINTS,
    FAILURE_MODE_HINTS,
    STRATEGY_HINTS,
    dominant_failure_mode,
    hint_for_slot,
    strategy_for_slot,
)
from zicato.proposer.prompts import render_failure_mode_profile
from zicato.testing import make_experiment, make_hypothesis_spec, make_mutation_point, make_patch

# ---------------------------------------------------------------------------
# Profiles rendered by the PRODUCTION renderer — one per dominant mode
# ---------------------------------------------------------------------------

#: recall >> precision by more than the renderer's 0.15 gap ⇒ the
#: decomposition line carries the ``=> over-retrieves`` marker.
_OVER_RETRIEVES = render_failure_mode_profile(
    OutcomeMarginalSummary(n_runs=10, recall_mean=0.8, precision_mean=0.3)
)

#: precision >> recall ⇒ the ``=> misses relevant items`` marker.
_MISSES = render_failure_mode_profile(
    OutcomeMarginalSummary(n_runs=10, recall_mean=0.3, precision_mean=0.8)
)

#: No decomposition; empty/terse dominates the banded rates.
_EMPTY_DOMINANT = render_failure_mode_profile(
    OutcomeMarginalSummary(n_runs=10, empty_rate=0.6, looping_rate=0.1)
)

#: No decomposition; looping dominates the banded rates.
_LOOPING_DOMINANT = render_failure_mode_profile(
    OutcomeMarginalSummary(n_runs=10, empty_rate=0.1, looping_rate=0.7)
)

#: Runs exist but every failure rate is zero — a profile with no positive
#: failure signal must behave exactly like an absent one.
_NO_SIGNAL = render_failure_mode_profile(OutcomeMarginalSummary(n_runs=10, pass_rate=1.0))


# ---------------------------------------------------------------------------
# dominant_failure_mode — the deterministic profile read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "expected_mode"),
    [
        (_OVER_RETRIEVES, "over_retrieval"),
        (_MISSES, "misses"),
        (_EMPTY_DOMINANT, "empty_terse"),
        (_LOOPING_DOMINANT, "looping"),
        (_NO_SIGNAL, None),
        ("", None),
        ("   \n  ", None),
    ],
)
def test_dominant_failure_mode(profile: str, expected_mode: str | None) -> None:
    assert dominant_failure_mode(profile) == expected_mode


def test_directional_marker_wins_over_rates() -> None:
    """The decomposition marker names the mode even when another banded rate
    is numerically larger — it is the renderer's most actionable signal."""
    profile = render_failure_mode_profile(
        OutcomeMarginalSummary(
            n_runs=10,
            recall_mean=0.8,
            precision_mean=0.3,  # ⇒ "=> over-retrieves" marker
            empty_rate=0.9,  # a larger banded rate that must NOT win
        )
    )
    assert dominant_failure_mode(profile) == "over_retrieval"


def test_rate_tie_breaks_in_fixed_order() -> None:
    """Equal banded rates resolve to the earlier mode in the fixed order, so
    the mapping is deterministic for a fixed profile string."""
    profile = render_failure_mode_profile(
        OutcomeMarginalSummary(n_runs=10, empty_rate=0.4, looping_rate=0.4)
    )
    assert dominant_failure_mode(profile) == "empty_terse"


# ---------------------------------------------------------------------------
# hint_for_slot — determinism, mode conditioning, exploratory last slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "mode"),
    [
        (_OVER_RETRIEVES, "over_retrieval"),
        (_MISSES, "misses"),
        (_EMPTY_DOMINANT, "empty_terse"),
        (_LOOPING_DOMINANT, "looping"),
    ],
)
def test_mode_conditioned_slots_and_exploratory_last_slot(profile: str, mode: str) -> None:
    n = 4
    hints = [hint_for_slot(i, n, profile) for i in range(n)]
    # Slots 0..N-2 all carry the dominant mode's hint.
    assert hints[: n - 1] == [FAILURE_MODE_HINTS[mode]] * (n - 1)
    # The LAST slot stays exploratory: one of the rotation hints, never the
    # mode hint — the slate never goes all-in on one reading of the profile.
    assert hints[-1] in EDIT_CLASS_HINTS
    assert hints[-1] != FAILURE_MODE_HINTS[mode]


def test_mapping_is_deterministic() -> None:
    for profile in (_OVER_RETRIEVES, _MISSES, _EMPTY_DOMINANT, _LOOPING_DOMINANT, "", _NO_SIGNAL):
        for n in (2, 3, 5):
            first = [hint_for_slot(i, n, profile) for i in range(n)]
            second = [hint_for_slot(i, n, profile) for i in range(n)]
            assert first == second


def test_empty_profile_is_the_historical_rotation() -> None:
    """Absent/empty/signal-free profile ⇒ today's pure rotation, hint for
    hint — the byte-identity guarantee at the mapping level."""
    for profile in ("", "  \n", _NO_SIGNAL):
        for n in (2, 3, 5):
            for i in range(n):
                assert hint_for_slot(i, n, profile) == EDIT_CLASS_HINTS[i % len(EDIT_CLASS_HINTS)]


def test_empty_profile_renders_byte_identical_prompts() -> None:
    """The full rendered user prompt for every slate slot is byte-identical
    to the pre-conditioning behaviour when the profile carries no signal."""
    mutations = (make_mutation_point(id="router__sp"),)
    for i, n in ((0, 3), (1, 3), (2, 3), (4, 5)):
        historical = render_proposal_evidence(
            current_loss_summary="loss=1.0",
            patterns=(),
            mutations=mutations,
            sample_hint=EDIT_CLASS_HINTS[i % len(EDIT_CLASS_HINTS)],
        )
        conditioned = render_proposal_evidence(
            current_loss_summary="loss=1.0",
            patterns=(),
            mutations=mutations,
            sample_hint=hint_for_slot(i, n, ""),
        )
        assert conditioned == historical


def test_hints_are_static_and_identity_free() -> None:
    """Every mintable hint is a fixed instruction string carrying no number
    at all — no rate, no count, no entry id can leak through a hint, so the
    restricted-visibility posture is enforceable by inspection."""
    for hint in (*EDIT_CLASS_HINTS, *FAILURE_MODE_HINTS.values(), *STRATEGY_HINTS):
        assert isinstance(hint, str)
        assert hint  # non-empty
        assert not any(ch.isdigit() for ch in hint)


# ---------------------------------------------------------------------------
# The per-(slot, round) STRATEGY framing (Lever 2)
# ---------------------------------------------------------------------------


def test_strategy_for_slot_is_deterministic() -> None:
    """Same (slot, generation_id) always yields the same strategy — no RNG."""
    for i in range(6):
        assert strategy_for_slot(i, "v7") == strategy_for_slot(i, "v7")
        assert strategy_for_slot(i, "v7") in STRATEGY_HINTS


def test_strategy_slots_span_the_vocabulary_within_one_round() -> None:
    """Within a round the slot offset walks the vocabulary — a 4-slot slate on
    a 4-item vocabulary draws four DISTINCT strategies."""
    n = len(STRATEGY_HINTS)
    strategies = [strategy_for_slot(i, "gen-42") for i in range(n)]
    assert len(set(strategies)) == n


def test_strategy_rotates_across_rounds_for_a_fixed_slot() -> None:
    """The same slot draws a different strategy in a different round (the
    generation id shifts the rotation offset), so the two-axis diversity is
    real. Not every pair need differ, but the assignment is not round-invariant.
    """
    slot0 = {strategy_for_slot(0, f"v{r}") for r in range(12)}
    assert len(slot0) > 1


# ---------------------------------------------------------------------------
# The wrapper stamps the conditioned hints onto each slot's context
# ---------------------------------------------------------------------------


def _experiment(core_idea: str) -> Experiment:
    return make_experiment(
        hypothesis=make_hypothesis_spec(core_idea=core_idea, modulating=("router__sp",)),
        patches=(make_patch(mutation_id="router__sp"),),
    )


class _HintRecordingAgent:
    """Scripted inner agent recording each call's ``sample_hint``."""

    def __init__(self, candidates: list[Experiment]) -> None:
        self._candidates = list(candidates)
        self._calls = 0
        self.hints: list[str] = []

    async def propose(self, ctx: ProposerContext) -> Experiment:
        self.hints.append(ctx.sample_hint)
        candidate = self._candidates[self._calls % len(self._candidates)]
        self._calls += 1
        return candidate


@pytest.mark.asyncio
async def test_slate_conditions_on_dominant_mode_and_keeps_last_slot_exploratory() -> None:
    inner = _HintRecordingAgent([_experiment("a"), _experiment("b"), _experiment("c")])
    agent = BestOfNProposerAgent(
        inner=inner,  # type: ignore[arg-type]
        config=ProposerQualityConfig(best_of_n=3, critique_enabled=False),
    )
    ctx = ProposerContext(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=(make_mutation_point(id="router__sp"),),
        brief_text="# brief",
        current_loss_summary="loss=1.0",
        aux_call_llm=None,  # type: ignore[arg-type]
        failure_profile=_LOOPING_DOMINANT,
    )
    await agent.propose(ctx)
    # Each slot's hint is the edit-class hint (first line) composed with a
    # per-(slot, round) strategy line (second line) — assert on the edit-class
    # axis, which the failure-mode conditioning still owns.
    edit_axis = [h.split("\n", 1)[0] for h in inner.hints]
    assert edit_axis[:2] == [FAILURE_MODE_HINTS["looping"]] * 2
    assert edit_axis[2] in EDIT_CLASS_HINTS
    # The strategy line rode along on every slot.
    assert all("\n" in h for h in inner.hints)
    assert ctx.sample_hint == ""  # the shared context is never mutated
