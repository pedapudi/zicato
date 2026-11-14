"""A restricted experiment memory shows a rejection's code, never its detail.

A stored rejection reason can name board entries and exact scalars, holdout
ones included. Under ``restrict_proposer_visibility`` the proposer and the
critic, which share one rendering, see only a code from a closed vocabulary.
The stored record keeps the full reason for the operator.
"""

from __future__ import annotations

import re

import pytest

from tests._proposal_evidence import render_proposal_evidence
from zicato.core.experiment import REJECTION_REASON_CODES, rejection_reason_code
from zicato.core.types import PriorExperiment, TournamentDecision
from zicato.proposer.prompts import render_prior_experiments_block
from zicato.testing.fixtures import make_mutation_point, make_scoring_weights
from zicato.tournament.gate import evaluate_gate

_HOLDOUT_IDS = ("holdout_refund_17", "holdout_escalation_4")


def _agg(scalar: float, passed: dict[str, bool]) -> dict:
    return {
        "scalar": scalar,
        "pass_rate": sum(passed.values()) / len(passed),
        "per_entry": {entry: {"pass_fail": ok, "drift_loss": 0.0} for entry, ok in passed.items()},
    }


def _holdout_rejection_reason() -> str:
    """The reason the real gate writes when two holdout entries regress."""
    outcome = evaluate_gate(
        _agg(0.50, {"train_a": True}),
        _agg(0.30, {"train_a": True}),
        make_scoring_weights(),
        holdout_parent_agg=_agg(0.40, dict.fromkeys(_HOLDOUT_IDS, True)),
        holdout_child_agg=_agg(0.40, dict.fromkeys(_HOLDOUT_IDS, False)),
    )
    assert outcome.decision == TournamentDecision.REJECTED
    return outcome.reason


def _rejected(reason: str) -> PriorExperiment:
    return PriorExperiment(
        generation_id="v6",
        epoch_id="e1",
        core_idea="Loosen the summarise tool description.",
        modulating=("writer.tools.summarize.description",),
        decision="rejected",
        rejection_reason=reason,
        scalar_score_delta=-0.2,
    )


def test_holdout_rejection_renders_no_entry_id_or_scalar_when_restricted() -> None:
    reason = _holdout_rejection_reason()
    assert all(entry in reason for entry in _HOLDOUT_IDS)  # the hazard is real

    block = render_prior_experiments_block([_rejected(reason)], restrict=True)

    assert "(holdout_not_confirmed)" in block
    assert not any(entry in block for entry in _HOLDOUT_IDS)
    head = block.splitlines()[1]
    assert not re.search(r"\d", head.replace("v6", ""))


def test_holdout_scalar_rejection_renders_no_number_when_restricted() -> None:
    reason = (
        "holdout_not_confirmed: holdout loss rose by 0.031250 "
        "(champion 0.412000 -> challenger 0.443250); a train-measured win must "
        "hold within 0.010000 on the holdout slice"
    )
    block = render_prior_experiments_block([_rejected(reason)], restrict=True)
    assert "(holdout_not_confirmed)" in block
    assert "0." not in block.splitlines()[1].split("]", 1)[1]


def test_restricted_evidence_shown_to_proposer_and_critic_carries_only_the_code() -> None:
    rendered = render_proposal_evidence(
        mutations=[make_mutation_point()],
        prior_experiments=[_rejected(_holdout_rejection_reason())],
        restrict_visibility=True,
    )
    assert "holdout_not_confirmed" in rendered
    assert not any(entry in rendered for entry in _HOLDOUT_IDS)


def test_unrestricted_rendering_keeps_the_stored_reason() -> None:
    reason = _holdout_rejection_reason()
    assert f"({reason})" in render_prior_experiments_block([_rejected(reason)], restrict=False)


@pytest.mark.parametrize(
    "reason, code",
    [
        (
            "insufficient improvement: loss fell by only 0.004000 (champion 0.5",
            "insufficient_improvement",
        ),
        ("challenger regressed: loss rose by 0.120000 (champion", "challenger_regressed"),
        ("pass-rate regression on entries: refund_3, refund_9", "pass_rate_regression"),
        ("pass-rate regression: overall pass-rate fell by 0.100000", "pass_rate_regression"),
        ("monotonicity_regression on namespace=drift: (champion 0.1 ->", "monotonicity_regression"),
        (
            "holdout_not_confirmed: incomplete execution of requested board units",
            "holdout_not_confirmed",
        ),
        ("holdout confirmation incomplete: result withheld", "confirmation_incomplete"),
        ("confirmation incomplete: 3 resolved pair duels; at least 6", "confirmation_incomplete"),
        ("inconclusive: strength difference remains unconfirmed", "confirmation_incomplete"),
        ("regression suite failed: 3 tests", "regression_suite_failed"),
        (
            "diff_complexity_ceiling: diff complexity 40 exceeds ceiling 10",
            "diff_complexity_ceiling",
        ),
        ("invalid evidence: champion scalar must be finite", "invalid_evidence"),
        ("incomplete execution: challenger has unstarted board units", "incomplete_execution"),
        ("containment_violation: child mutated 2 file(s)", "containment_violation"),
        ("gate_contradiction: recorded PROMOTE but", "gate_contradiction"),
        ("field_diversity_overlap: overlap 0.910 with sibling v7", "field_diversity_overlap"),
        ("field_diversity_duplicate: duplicates an in-flight sibling", "field_diversity_duplicate"),
        ("validation_failed: unknown mutation id", "validation_failed"),
        ("proposer_retries_exhausted: x; y", "proposer_retries_exhausted"),
        ("deferred_infra: every proposal attempt failed", "deferred_infra"),
        ("skip_round: operator skipped the round", "skip_round"),
        ("wall_clock_budget: evolve total budget of 60s exceeded", "wall_clock_budget"),
        ("operator override: refund_3 looked wrong", "operator_override"),
        ("no challenger duel ran: v4", "not_evaluated"),
        ("multi-challenger field: no challenger applied cleanly (2x x)", "not_evaluated"),
        ("no racing survivor cleared the full-board champion gate", "champion_gate_not_cleared"),
        ("no finalist cleared the champion gate: v3 lost on refund_3", "champion_gate_not_cleared"),
        ("swiss leader did not clear the champion gate: 0.31", "champion_gate_not_cleared"),
        ("refund_3 regressed by 0.25", "rejected"),
        ("", "rejected"),
    ],
)
def test_reason_reduces_to_a_code_in_the_closed_vocabulary(reason: str, code: str) -> None:
    assert rejection_reason_code(reason) == code
    assert code in REJECTION_REASON_CODES


def test_every_code_is_a_bare_token() -> None:
    assert all(re.fullmatch(r"[a-z_]+", code) for code in REJECTION_REASON_CODES)
