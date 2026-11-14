"""A restricted experiment memory shows a rejection's code, never its detail.

A stored rejection reason can name board entries and exact scalars, holdout
ones included. Under ``restrict_proposer_visibility`` the proposer and the
critic, which share one rendering, see only a code from a closed vocabulary.
The stored record keeps the full reason for the operator.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tests._proposal_evidence import render_proposal_evidence
from zicato.core.experiment import REJECTION_REASON_CODES, rejection_reason_code
from zicato.core.scoring_config import ExperimentalConfig, ScoringWeights
from zicato.core.types import PriorExperiment, TournamentDecision
from zicato.epoch.containment import containment_reason
from zicato.evolve.persist import _skipped_round_outcome
from zicato.proposer.prompts import render_prior_experiments_block
from zicato.testing.fixtures import make_mutation_point
from zicato.tournament.gate import evaluate_gate

_HOLDOUT_IDS = ("holdout_refund_17", "holdout_escalation_4")
_TRAIN_IDS = ("train_refund_3", "train_refund_9")


def _agg(scalar: float, passed: dict[str, bool], **extra) -> dict:
    return {
        "scalar": scalar,
        "pass_rate": sum(passed.values()) / len(passed),
        "per_entry": {entry: {"pass_fail": ok, "drift_loss": 0.0} for entry, ok in passed.items()},
        **extra,
    }


def _gate_reason(
    parent: dict, child: dict, weights: ScoringWeights | None = None, **holdout
) -> str:
    """The reason the real gate writes for a rejected duel."""
    outcome = evaluate_gate(parent, child, weights or ScoringWeights(), **holdout)
    assert outcome.decision == TournamentDecision.REJECTED
    return outcome.reason


_PASSING = dict.fromkeys(_TRAIN_IDS, True)

# Each reason is built by the code that writes it to a record.
_PRODUCED = {
    "holdout_not_confirmed (entries)": (
        lambda: _gate_reason(
            _agg(0.50, _PASSING),
            _agg(0.30, _PASSING),
            holdout_parent_agg=_agg(0.40, dict.fromkeys(_HOLDOUT_IDS, True)),
            holdout_child_agg=_agg(0.40, dict.fromkeys(_HOLDOUT_IDS, False)),
        ),
        "holdout_not_confirmed",
    ),
    "holdout_not_confirmed (scalar)": (
        lambda: _gate_reason(
            _agg(0.50, _PASSING),
            _agg(0.30, _PASSING),
            holdout_parent_agg=_agg(0.412, dict.fromkeys(_HOLDOUT_IDS, True)),
            holdout_child_agg=_agg(0.44325, dict.fromkeys(_HOLDOUT_IDS, True)),
        ),
        "holdout_not_confirmed",
    ),
    "monotonicity_regression": (
        lambda: _gate_reason(
            _agg(0.50, _PASSING, namespace_aggregates={"drift:": 0.1375}),
            _agg(0.30, _PASSING, namespace_aggregates={"drift:": 0.2625}),
            ScoringWeights(namespace_monotonicity={"drift:": True}),
        ),
        "monotonicity_regression",
    ),
    "pass_rate_regression": (
        lambda: _gate_reason(_agg(0.50, _PASSING), _agg(0.30, dict.fromkeys(_TRAIN_IDS, False))),
        "pass_rate_regression",
    ),
    "insufficient_improvement": (
        lambda: _gate_reason(_agg(0.5, _PASSING), _agg(0.4975, _PASSING)),
        "insufficient_improvement",
    ),
    "challenger_regressed": (
        lambda: _gate_reason(_agg(0.5, _PASSING), _agg(0.6125, _PASSING)),
        "challenger_regressed",
    ),
    "invalid_evidence": (
        lambda: _gate_reason(_agg(0.5, _PASSING), _agg(float("nan"), _PASSING)),
        "invalid_evidence",
    ),
    "diff_complexity_ceiling": (
        lambda: _gate_reason(
            _agg(0.5, _PASSING),
            _agg(0.3, _PASSING, diff_size={"added": 30, "removed": 9, "patches": 2}),
            ScoringWeights(experimental=ExperimentalConfig(diff_complexity_ceiling=10.0)),
        ),
        "diff_complexity_ceiling",
    ),
    "containment_violation": (
        lambda: containment_reason(
            SimpleNamespace(
                status="violated",
                findings=[SimpleNamespace(code="outside", path="tests/a_17.py", detail="changed")],
            )
        ),
        "containment_violation",
    ),
    "skip_round": (
        lambda: _skipped_round_outcome("v5", "entry train_refund_3 looked wrong").rejection_reason,
        "skip_round",
    ),
}


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


@pytest.mark.parametrize("name", sorted(_PRODUCED))
def test_a_produced_reason_renders_as_its_code_alone_when_restricted(name: str) -> None:
    produce, code = _PRODUCED[name]
    reason = produce()
    assert reason != code  # the stored reason carries detail beyond the code

    head = render_prior_experiments_block([_rejected(reason)], restrict=True).splitlines()[1]

    assert head.endswith(f"({code})")
    assert not any(entry in head for entry in _HOLDOUT_IDS + _TRAIN_IDS)
    assert not re.search(r"\d", head.replace("v6", ""))
    assert code in REJECTION_REASON_CODES


def test_the_holdout_reason_names_holdout_entries_in_the_stored_record() -> None:
    reason = _PRODUCED["holdout_not_confirmed (entries)"][0]()
    assert all(entry in reason for entry in _HOLDOUT_IDS)
    assert f"({reason})" in render_prior_experiments_block([_rejected(reason)], restrict=False)


def test_restricted_evidence_shown_to_proposer_and_critic_carries_only_the_code() -> None:
    rendered = render_proposal_evidence(
        mutations=[make_mutation_point()],
        prior_experiments=[_rejected(_PRODUCED["holdout_not_confirmed (entries)"][0]())],
        restrict_visibility=True,
    )
    assert "holdout_not_confirmed" in rendered
    assert not any(entry in rendered for entry in _HOLDOUT_IDS)


@pytest.mark.parametrize(
    "reason, code",
    [
        ("holdout confirmation incomplete: result withheld", "confirmation_incomplete"),
        ("confirmation incomplete: 3 resolved pair duels; at least 6", "confirmation_incomplete"),
        ("inconclusive: strength difference remains unconfirmed", "confirmation_incomplete"),
        ("regression suite failed: 3 tests", "regression_suite_failed"),
        ("incomplete execution: challenger has unstarted board units", "incomplete_execution"),
        ("gate_contradiction: recorded PROMOTE but", "gate_contradiction"),
        ("field_diversity_overlap: overlap 0.910 with sibling v7", "field_diversity_overlap"),
        ("field_diversity_duplicate: duplicates an in-flight sibling", "field_diversity_duplicate"),
        ("validation_failed: unknown mutation id", "validation_failed"),
        ("proposer_retries_exhausted: x; y", "proposer_retries_exhausted"),
        ("deferred_infra: every proposal attempt failed", "deferred_infra"),
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
