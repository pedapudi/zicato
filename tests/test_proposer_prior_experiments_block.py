"""Tests for the experiment-memory prompt block + its placement.

Covers ``docs/design/EXPERIMENT-MEMORY.md`` §3.5: the
``render_prior_experiments_block`` grouping (promoted / rejected /
in-flight), the empty-input sentinel, the cross-contract Δscalar
omission, and the section's placement in ``render_user_prompt`` (after
``## Recent telemetry insights`` and before ``## Current loss summary``).
"""

from __future__ import annotations

from zicato.core.types import PriorExperiment
from zicato.proposer.prompts import render_prior_experiments_block, render_user_prompt
from zicato.testing.fixtures import make_mutation_point, make_pattern


def _promoted() -> PriorExperiment:
    return PriorExperiment(
        generation_id="v5",
        epoch_id="e1",
        core_idea="Add a budget hint to the coordinator routing.",
        modulating=("coordinator.routing",),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=0.12,
    )


def _rejected() -> PriorExperiment:
    return PriorExperiment(
        generation_id="v6",
        epoch_id="e1",
        core_idea="Loosen the writer's summarise tool description.",
        modulating=("writer.tools.summarize.description",),
        decision="rejected",
        rejection_reason="pass_rate_regression_on_summarise_short",
        scalar_score_delta=-0.09,
    )


def _in_flight() -> PriorExperiment:
    return PriorExperiment(
        generation_id="v8",
        epoch_id="e1",
        core_idea="Tighten the researcher instruction to forbid uncited claims.",
        modulating=("researcher.instruction",),
        decision="in_flight",
        rejection_reason="",
        scalar_score_delta=None,
    )


def test_empty_input_returns_empty_string() -> None:
    assert render_prior_experiments_block([]) == ""


def test_groups_promoted_rejected_in_flight() -> None:
    block = render_prior_experiments_block([_promoted(), _rejected(), _in_flight()])
    assert "Already promoted (build on these" in block
    assert "Already rejected (do NOT re-propose" in block
    assert "Proposed this round, not yet evaluated" in block
    # Promoted block comes before rejected, which comes before in-flight.
    assert block.index("Already promoted") < block.index("Already rejected")
    assert block.index("Already rejected") < block.index("Proposed this round")


def test_promoted_line_shape() -> None:
    block = render_prior_experiments_block([_promoted()])
    assert "- v5 PROMOTED Δscalar=+0.120  [coordinator.routing]" in block
    assert "Add a budget hint to the coordinator routing." in block


def test_rejected_line_carries_reason_and_negative_delta() -> None:
    block = render_prior_experiments_block([_rejected()])
    assert "- v6 REJECTED Δscalar=-0.090" in block
    assert "(pass_rate_regression_on_summarise_short)" in block


def test_in_flight_line_has_no_delta() -> None:
    block = render_prior_experiments_block([_in_flight()])
    assert "- v8 IN-FLIGHT  [researcher.instruction]" in block
    assert "Δscalar" not in block


def test_cross_contract_entry_renders_without_delta() -> None:
    """A ``same_contract=False`` entry renders without its Δscalar — the
    number does not transfer across contracts (§3.4)."""
    cross = PriorExperiment(
        generation_id="v2",
        epoch_id="other-epoch",
        core_idea="A cross-contract idea from an earlier epoch.",
        modulating=("router.system",),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=0.30,
        same_contract=False,
    )
    block = render_prior_experiments_block([cross])
    assert "v2 PROMOTED" in block
    assert "Δscalar" not in block


def test_section_omitted_when_prior_empty() -> None:
    prompt = render_user_prompt(
        current_loss_summary="loss=2.3",
        patterns=[make_pattern()],
        mutations=[make_mutation_point()],
        prior_experiments=(),
    )
    assert "What's already been tried" not in prompt


def test_section_lands_between_insights_and_loss_summary() -> None:
    prompt = render_user_prompt(
        current_loss_summary="loss=2.3",
        patterns=[make_pattern()],
        mutations=[make_mutation_point()],
        insights="some analyzer insights",
        prior_experiments=[_promoted()],
    )
    i_insights = prompt.index("## Recent telemetry insights")
    i_tried = prompt.index("## What's already been tried")
    i_loss = prompt.index("## Current loss summary")
    assert i_insights < i_tried < i_loss
