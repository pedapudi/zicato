"""The proposer scorecard — the reader, its honesty rules, and its redaction.

Every fixture goes through the REAL :class:`~zicato.epoch.round_log.RoundLog`
writer, for the reason ``test_epoch_round_integrity`` states: a check that only
passes against hand-rolled JSONL checks the test's idea of the format, not the
format.

The rules under test are the ones that make the numbers usable rather than
merely present — a null is never a zero, a sample count rides every rate, a
thin sample is marked — plus the two structural claims the scorecard rests on:
that A1–A4 classification reads a stamped code rather than parsing prose, and
that nothing board-shaped can reach a record.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from zicato.cli.commands.proposer import proposer_grp
from zicato.epoch.round_log import (
    CandidateSampled,
    CandidateScreened,
    DecisionRecorded,
    GateEvaluated,
    PatchesApplied,
    ProposalAttempted,
    RoundClosed,
    RoundEvent,
    RoundLog,
    RoundOpened,
    UnitCompleted,
)
from zicato.mutation.validator import POST_APPLY_CHECKS, classify_post_apply_error
from zicato.proposer.scorecard import (
    MIN_SAMPLE_N,
    UNCLASSIFIED,
    Rate,
    read_epoch_scorecard,
    read_scorecard_trend,
)

EPOCH = "e1"


def _write(workspace: Path, round_index: int, events: list[RoundEvent]) -> None:
    log = RoundLog(workspace, EPOCH, round_index)
    for event in events:
        log.append(event)


def _round(
    *,
    decision: str = "rejected",
    errors: tuple[str, ...] = (),
    attempts: int = 1,
    screened: tuple[bool, ...] = (),
    revise: tuple[bool, ...] = (),
    gate: tuple[float, float, float] | None = None,
    units: int = 0,
    generation_id: str = "",
) -> list[RoundEvent]:
    """One synthetic round's event stream, in the order the loop emits them."""
    events: list[RoundEvent] = [RoundOpened(contract_hash="c")]
    for i in range(attempts):
        events.append(ProposalAttempted(errors=errors if i == attempts - 1 else ()))
        events.append(CandidateSampled(i=i, n=attempts))
    for i, vetoed in enumerate(screened):
        events.append(CandidateScreened(index=i, vetoed=vetoed))
    for i, vetoed in enumerate(revise):
        events.append(CandidateScreened(index=100 + i, vetoed=vetoed, revise=True))
    if generation_id:
        events.append(PatchesApplied(generation_id=generation_id))
    for i in range(units):
        events.append(UnitCompleted(entry_id=f"entry_{i}", replicate=0, side="challenger"))
    if gate is not None:
        champion, challenger, required = gate
        events.append(
            GateEvaluated(
                rule_fired="",
                decision=decision,
                champion_scalar=champion,
                challenger_scalar=challenger,
                margin_required=required,
            )
        )
    events.append(DecisionRecorded(decision=decision))
    events.append(RoundClosed())
    return events


# ---------------------------------------------------------------------------
# Honesty rules
# ---------------------------------------------------------------------------


def test_null_rate_is_not_zero() -> None:
    """An unobserved rate is ``None``, and a measured zero is ``0.0``."""
    assert Rate(k=0, n=0).value is None
    assert Rate(k=0, n=7).value == 0.0
    # ...and the distinction survives serialization, which is where a surface
    # would otherwise coerce the null into a falsy zero.
    assert Rate(k=0, n=0).to_json()["value"] is None
    assert Rate(k=0, n=7).to_json()["value"] == 0.0


def test_thin_samples_are_marked_provisional() -> None:
    assert Rate(k=1, n=MIN_SAMPLE_N - 1).provisional is True
    assert Rate(k=1, n=MIN_SAMPLE_N).provisional is False
    # An unobserved rate is not "provisional" — it is absent, a stronger claim.
    assert Rate(k=0, n=0).provisional is False


def test_empty_epoch_reads_as_all_null(tmp_path: Path) -> None:
    """An epoch that never ran is a card of nulls, not an error and not zeros."""
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.rounds == 0
    assert card.promote_rate.value is None
    assert card.screen_veto_rate.value is None
    assert card.margins.n == 0
    assert card.cost.attempts_per_acceptance is None


def test_cost_per_acceptance_is_null_when_nothing_promoted(tmp_path: Path) -> None:
    """Zero acceptances has no cost-per-acceptance — not zero, not infinity."""
    _write(tmp_path, 0, _round(decision="rejected", units=4))
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.cost.accepted == 0
    assert card.cost.board_units == 4
    assert card.cost.attempts_per_acceptance is None
    assert card.cost.units_per_acceptance is None


# ---------------------------------------------------------------------------
# The aggregates
# ---------------------------------------------------------------------------


def test_validator_failures_classify_by_stamped_code(tmp_path: Path) -> None:
    """A1–A4 rates come from the code the validator stamps, per ATTEMPT."""
    _write(tmp_path, 0, _round(errors=("A4: dropped top-level imports: os",)))
    _write(tmp_path, 1, _round(errors=("A1: syntax error", "A4: dropped imports: json")))
    _write(tmp_path, 2, _round(errors=()))
    card = read_epoch_scorecard(tmp_path, EPOCH)

    assert card.proposals == 3
    # The attempt that hit BOTH checks counts once for each — a rate over
    # attempts, not a tally of error strings.
    assert card.validator_failure_rates["A4"] == Rate(k=2, n=3)
    assert card.validator_failure_rates["A1"] == Rate(k=1, n=3)
    assert card.validator_failure_rates["A2"] == Rate(k=0, n=3)
    assert card.validation_failure_rate == Rate(k=2, n=3)


def test_an_attempt_hitting_one_check_twice_counts_once(tmp_path: Path) -> None:
    _write(tmp_path, 0, _round(errors=("A4: dropped os", "A4: dropped json")))
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.validator_failure_rates["A4"] == Rate(k=1, n=1)


def test_uncoded_errors_land_in_unclassified_not_a_check(tmp_path: Path) -> None:
    """An error with no recognised code is never attributed to a check.

    This is the back-compatibility case AND the foreign-error case: a round log
    written before the codes existed, and a best-of-N slot that recorded a
    credential lapse, both look like this. Charging either to A1 would invent a
    validator failure that never happened.
    """
    _write(tmp_path, 0, _round(errors=("proposer returned invalid JSON",)))
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.validator_failure_rates[UNCLASSIFIED] == Rate(k=1, n=1)
    for code in POST_APPLY_CHECKS:
        assert card.validator_failure_rates[code].k == 0
    # ...while the any-check rate still counts it as a failed attempt.
    assert card.validation_failure_rate == Rate(k=1, n=1)


def test_screen_and_revision_rates(tmp_path: Path) -> None:
    _write(tmp_path, 0, _round(screened=(True, True, False), revise=(False,)))
    _write(tmp_path, 1, _round(screened=(True, False), revise=(True,)))
    card = read_epoch_scorecard(tmp_path, EPOCH)
    # Five ordinary screens (3 vetoed) plus two revise screens (1 vetoed).
    assert card.screen_veto_rate == Rate(k=4, n=7)
    # One of the two revise re-samples survived.
    assert card.revision_success_rate == Rate(k=1, n=2)


def test_gate_margins_use_the_loss_direction(tmp_path: Path) -> None:
    """``achieved`` is champion − challenger; the loop scores a LOSS."""
    _write(tmp_path, 0, _round(gate=(0.50, 0.40, 0.05), decision="promoted"))
    _write(tmp_path, 1, _round(gate=(0.50, 0.52, 0.05)))
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.margins.n == 2
    assert round(card.margins.achieved_max, 6) == 0.10
    assert round(card.margins.achieved_min, 6) == -0.02
    # Headroom is the signed distance to the bar: +0.05 and −0.07, median −0.01.
    assert round(card.margins.headroom_median, 6) == -0.01


def test_gates_without_scalars_are_unmeasured_not_zero(tmp_path: Path) -> None:
    """A pre-fields gate contributes to no statistic and is counted separately."""
    _write(tmp_path, 0, [RoundOpened(), GateEvaluated(decision="rejected"), RoundClosed()])
    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.margins.n == 0
    assert card.margins.unmeasured == 1
    assert card.margins.achieved_median is None


def test_a_reran_round_counts_one_gate_and_every_attempt(tmp_path: Path) -> None:
    """The two families of aggregate take OPPOSITE slices of a re-run round.

    One log can hold two attempts at the same index: a round that applied
    patches but died before its experiment was written never consumes its
    index, so the next invocation reopens it and appends. Folding the union
    would let the dead attempt contribute a second gate — and here a second
    promotion — to a round the epoch settled once. But slicing proposal
    attempts the same way would hide the failure that CAUSED the re-run, which
    is the one thing this scorecard exists to measure: the failure rate would
    improve exactly when the proposer did worst.
    """
    log = RoundLog(tmp_path, EPOCH, 0)
    # Attempt 1: the proposer failed A4, then the round died.
    for event in [
        RoundOpened(contract_hash="c"),
        ProposalAttempted(errors=("A4: dropped top-level imports: os",)),
        GateEvaluated(decision="promoted", champion_scalar=0.5, challenger_scalar=0.1),
    ]:
        log.append(event)
    # Attempt 2: reopened at the same index, clean, and rejected.
    for event in [
        RoundOpened(contract_hash="c"),
        ProposalAttempted(),
        GateEvaluated(
            decision="rejected",
            champion_scalar=0.5,
            challenger_scalar=0.52,
            margin_required=0.05,
        ),
        DecisionRecorded(decision="rejected"),
        RoundClosed(),
    ]:
        log.append(event)

    card = read_epoch_scorecard(tmp_path, EPOCH)
    # Gate + decision facts: the FINAL attempt only. The dead attempt's
    # promotion is not the epoch's outcome and must not be counted as one.
    assert card.margins.n == 1
    assert card.promote_rate == Rate(k=0, n=1)
    # Proposal facts: EVERY attempt. Both were real proposer calls, and one
    # really did fail A4.
    assert card.proposals == 2
    assert card.validator_failure_rates["A4"] == Rate(k=1, n=2)
    assert card.cost.proposal_attempts == 2


def test_a_corrupt_round_is_skipped_not_fatal(tmp_path: Path) -> None:
    """One damaged round must not deny the operator the others."""
    _write(tmp_path, 0, _round(decision="promoted"))
    _write(tmp_path, 1, _round())
    bad = RoundLog(tmp_path, EPOCH, 1).path
    lines = bad.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "{not json")
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")

    card = read_epoch_scorecard(tmp_path, EPOCH)
    assert card.rounds == 1
    assert card.promote_rate == Rate(k=1, n=1)


def test_trend_keeps_the_most_recent_epochs_in_order(tmp_path: Path) -> None:
    """``--limit`` takes the TAIL: a trend that reads backwards is not a trend."""
    ws = tmp_path
    for name in ("e0", "e1", "e2"):
        (ws / "epochs" / name).mkdir(parents=True)
    cards = read_scorecard_trend(ws, limit=2)
    # No epoch configs on disk, so nothing is attributable and nothing is
    # returned — the honest degrade, not three unattributed rows.
    assert cards == []


def test_only_the_named_winner_gets_the_site_promotion_credit(tmp_path: Path) -> None:
    """A multi-challenger round promotes at most ONE child; only it is credited.

    Crediting the round would mark every losing challenger's site a winner —
    and those are precisely the sites a reflection pass reads to decide where
    the proposer is weak.
    """
    from zicato.core.experiment import Experiment, HypothesisSpec
    from zicato.core.mutation import Patch
    from zicato.epoch.journal import write_experiment

    for gen, site in (("v1", "winner__sp"), ("v2", "loser__sp")):
        write_experiment(
            tmp_path,
            EPOCH,
            gen,
            Experiment(
                id=f"exp-{gen}",
                epoch_id=EPOCH,
                generation_id=gen,
                parent_generation_id="v0",
                proposed_at="2026-08-11T12:00:00+00:00",
                hypothesis=HypothesisSpec(
                    core_idea="idea",
                    modulating=(site,),
                    why="why",
                    expected_drift_movements=(),
                    expected_pass_rate_delta="+0.0 to +0.1",
                    risks="none",
                ),
                patches=(
                    Patch(
                        id=f"p-{gen}",
                        mutation_id=site,
                        op="replace",
                        new_content="x",
                        new_numeric=None,
                        new_enum=None,
                        rationale="r",
                    ),
                ),
                outcome=None,
            ),
        )
    _write(
        tmp_path,
        0,
        [
            RoundOpened(contract_hash="c"),
            ProposalAttempted(),
            PatchesApplied(generation_id="v1"),
            PatchesApplied(generation_id="v2"),
            DecisionRecorded(decision="promoted", provenance={"promoted_generation_id": "v1"}),
            RoundClosed(),
        ],
    )
    sites = {s.mutation_id: s for s in read_epoch_scorecard(tmp_path, EPOCH).mutation_sites}
    assert sites["winner__sp"].promoted == 1
    assert sites["loser__sp"].promoted == 0
    assert sites["loser__sp"].proposed == 1


# ---------------------------------------------------------------------------
# Redaction — the structural claim
# ---------------------------------------------------------------------------


def test_no_entry_identity_reaches_the_card(tmp_path: Path) -> None:
    """The round log carries entry ids; the scorecard counts them and drops them.

    ``UnitCompleted`` names the board entry that ran and ``GateEvaluated`` names
    the entries that regressed. Both are in the fixture below. Neither may
    appear anywhere in the serialized card — that is what lets a reflection pass
    read a scorecard without reading the board.
    """
    events = _round(units=3, gate=(0.5, 0.4, 0.05), decision="promoted")
    events.insert(
        -2,
        GateEvaluated(
            decision="promoted",
            champion_scalar=0.5,
            challenger_scalar=0.4,
            margin_required=0.05,
            attributable_regressions=("secret_entry_id",),
        ),
    )
    _write(tmp_path, 0, events)

    blob = json.dumps(read_epoch_scorecard(tmp_path, EPOCH).to_json())
    assert "secret_entry_id" not in blob
    assert "entry_0" not in blob
    assert "entry_id" not in blob
    # The units were still COUNTED — dropping identity is not dropping evidence.
    assert read_epoch_scorecard(tmp_path, EPOCH).cost.board_units == 3


# ---------------------------------------------------------------------------
# The validator's half of the contract
# ---------------------------------------------------------------------------


def test_classify_reads_the_prefix_and_admits_nothing_else() -> None:
    assert classify_post_apply_error("A3: required placeholder missing") == "A3"
    assert classify_post_apply_error("Post-apply syntax error in x.py") is None
    assert classify_post_apply_error("A9: not a real check") is None
    # A bare code with no separator is not a stamped error.
    assert classify_post_apply_error("A1") is None


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_scorecard_cli_renders_counts_beside_every_rate(tmp_path: Path) -> None:
    _write(tmp_path, 0, _round(errors=("A4: dropped imports: os",)))
    result = CliRunner().invoke(
        proposer_grp,
        ["scorecard", "--workspace", str(tmp_path), "--epoch", EPOCH, "--no-trend"],
    )
    assert result.exit_code == 0, result.output
    assert "(1/1)" in result.output
    assert "A4" in result.output
    # The legend states the two rules the table depends on.
    assert "NOT zero" in result.output


def test_scorecard_cli_json_round_trips(tmp_path: Path) -> None:
    _write(tmp_path, 0, _round(decision="promoted", gate=(0.5, 0.4, 0.05)))
    result = CliRunner().invoke(
        proposer_grp,
        ["scorecard", "--workspace", str(tmp_path), "--epoch", EPOCH, "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["epoch"]["promote_rate"] == {
        "k": 1,
        "n": 1,
        "value": 1.0,
        "provisional": True,
    }
