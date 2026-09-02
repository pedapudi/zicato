"""A proposal episode ends four ways, and each one is recorded as itself.

A round that only knew "the proposer gave up" could not tell an operator
whether to widen the mutation surface, raise the budget, or fix a defect.
These cases pin the vocabulary, the mapping from Foe's own outcome
vocabulary onto zicato's, what the round log records, what the scorecard
counts, and that no ending's message can carry board content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import (
    FOE_BLOCKED_CODES,
    PROPOSER_BLOCKED_CODES,
    PROPOSER_BUDGET_DIMENSIONS,
    ProposerEpisodeOutcome,
)
from zicato.epoch.round_log import ProposalEpisodeSettled, RoundLog, fold_round_record
from zicato.proposer.proposer import ProposerBlocked, ProposerError, ProposerExhausted
from zicato.proposer.reflection import assert_redacted
from zicato.proposer.scorecard import read_epoch_scorecard

#: The complete blocked vocabulary Foe's log format declares, transcribed
#: from `foe/docs/log-format.md` at the pinned commit. Held equal to the
#: mapping's keys so a Foe release that adds a code fails here rather than
#: reaching a round as an unrouted string.
FOE_VOCABULARY = frozenset(
    {
        "looping-tool-call",
        "looping-reasoning",
        "goal-unreachable",
        "ambiguous-task",
        "missing-capability",
        "verification-unsatisfiable",
        "child-blocked",
        "recovery-exhausted",
        "recovery-failed",
    }
)


def test_every_foe_blocked_code_maps_to_a_zicato_code() -> None:
    assert set(FOE_BLOCKED_CODES) == FOE_VOCABULARY
    assert set(FOE_BLOCKED_CODES.values()) <= PROPOSER_BLOCKED_CODES


def test_the_readings_that_are_not_renamings_are_the_two_named_ones() -> None:
    """Two Foe codes mean something more specific in this task's terms."""
    assert FOE_BLOCKED_CODES["goal-unreachable"] == "no-groundable-mutation-point"
    assert FOE_BLOCKED_CODES["ambiguous-task"] == "ambiguous-brief"
    unchanged = {k for k, v in FOE_BLOCKED_CODES.items() if k == v}
    assert unchanged == FOE_VOCABULARY - {"goal-unreachable", "ambiguous-task"}


def test_the_one_code_no_foe_ending_produces_is_the_scratch_tree_rule() -> None:
    """A hunk outside every mutation point is a zicato-side condition."""
    assert PROPOSER_BLOCKED_CODES - set(FOE_BLOCKED_CODES.values()) == {
        "edit-outside-mutation-point"
    }


def test_the_foe_exhausted_limits_are_the_dimensions_an_episode_can_name() -> None:
    assert "seconds" in PROPOSER_BUDGET_DIMENSIONS
    assert "model_calls" in PROPOSER_BUDGET_DIMENSIONS
    assert len(set(PROPOSER_BUDGET_DIMENSIONS)) == len(PROPOSER_BUDGET_DIMENSIONS)


def test_a_blocked_episode_reports_its_kind_code_and_message() -> None:
    blocked = ProposerBlocked("no-groundable-mutation-point", "no point matches the brief")
    assert blocked.outcome == ProposerEpisodeOutcome(
        kind="blocked",
        code="no-groundable-mutation-point",
        message="no point matches the brief",
    )


def test_an_exhausted_episode_names_the_budget_dimension_that_ran_out() -> None:
    assert ProposerExhausted("seconds").outcome == ProposerEpisodeOutcome(
        kind="exhausted", code="seconds"
    )


def test_a_plain_proposer_error_is_the_failed_outcome() -> None:
    assert ProposerError(["the binary exited with code 70"]).outcome == ProposerEpisodeOutcome(
        kind="failed", message="the binary exited with code 70"
    )


def test_a_code_outside_the_closed_vocabulary_is_refused() -> None:
    with pytest.raises(ValueError, match="the vocabulary is closed"):
        ProposerBlocked("something-went-wrong")  # type: ignore[arg-type]


def test_both_new_endings_still_reach_a_handler_written_for_the_old_one() -> None:
    """Every round degrades the same way; only the record distinguishes them."""
    assert isinstance(ProposerBlocked("looping-tool-call"), ProposerError)
    assert isinstance(ProposerExhausted("model_calls"), ProposerError)


def test_no_ending_message_can_carry_board_content() -> None:
    for outcome in (
        ProposerBlocked("edit-outside-mutation-point", "src/prompt.py lines 4-9").outcome,
        ProposerExhausted("model_calls").outcome,
        ProposerError(["the transport ended without a done chunk"]).outcome,
    ):
        assert_redacted(
            {"kind": outcome.kind, "code": outcome.code, "message": outcome.message},
            where="proposal_episode_settled",
        )


def test_the_round_log_records_the_ending_and_the_fold_reads_it_back(tmp_path: Path) -> None:
    log = RoundLog(tmp_path, "e1", 0)
    log.append(
        ProposalEpisodeSettled(kind="blocked", code="ambiguous-brief", message="two readings")
    )
    log.append(ProposalEpisodeSettled(kind="exhausted", code="seconds"))
    record = fold_round_record(log.read())
    assert record.proposal.episode_outcomes == (
        ProposerEpisodeOutcome(kind="blocked", code="ambiguous-brief", message="two readings"),
        ProposerEpisodeOutcome(kind="exhausted", code="seconds"),
    )


def test_the_scorecard_counts_each_kind_and_each_blocked_code(tmp_path: Path) -> None:
    for index, events in enumerate(
        [
            [ProposalEpisodeSettled(kind="completed")],
            [ProposalEpisodeSettled(kind="blocked", code="no-groundable-mutation-point")],
            [ProposalEpisodeSettled(kind="blocked", code="no-groundable-mutation-point")],
            [ProposalEpisodeSettled(kind="exhausted", code="model_calls")],
            [ProposalEpisodeSettled(kind="failed")],
        ]
    ):
        log = RoundLog(tmp_path, "e1", index)
        for event in events:
            log.append(event)

    card = read_epoch_scorecard(tmp_path, "e1")
    assert card.episode_outcomes == {
        "completed": 1,
        "blocked": 2,
        "exhausted": 1,
        "errored": 1,
    }
    assert card.blocked_codes == {"no-groundable-mutation-point": 2}
    assert card.exhausted_limits == {"model_calls": 1}
    payload = json.loads(json.dumps(card.to_json()))
    assert payload["blocked_codes"] == {"no-groundable-mutation-point": 2}
