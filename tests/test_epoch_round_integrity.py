"""Round-granular integrity over the durable round logs.

The defended failure: a measurement sweep whose model endpoint lost its
credentials mid-run. The cell straddling the outage kept the results of
the rounds that had already finished, satisfied a "did we reach the
model?" liveness probe, was marked complete, and contributed a mean
built from fewer duels than its peers. Cell-level accounting saw nothing
wrong; a sixth of the individual rounds had emitted no
``gate_evaluated`` event at all.

So every fixture here is a ROUND, and every fixture is written through
the REAL :class:`~zicato.epoch.round_log.RoundLog` writer — a check that
only passes against hand-rolled JSONL is a check of the test's idea of
the format, not the format. The two exceptions are the cases that are
specifically ABOUT malformed bytes: the torn/corrupt-log tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from zicato.epoch.round_integrity import (
    HARD_INFRA_MARKERS,
    EpochRoundIntegrity,
    RoundStatus,
    epoch_round_integrity,
    round_integrity,
)
from zicato.epoch.round_log import (
    CandidateSampled,
    DecisionRecorded,
    ExperimentMinted,
    GateEvaluated,
    PatchesApplied,
    ProposalAttempted,
    RoundClosed,
    RoundEvent,
    RoundLog,
    RoundOpened,
    UnitCompleted,
    ValidationFailed,
    round_dir,
)

EPOCH = "e1"

ENTRIES = ("conv_body", "conv_summary", "conv_citations")


# ---------------------------------------------------------------------------
# Fixture builders — every log goes through the real writer.
# ---------------------------------------------------------------------------


def _write(workspace: Path, round_index: int, events: list[RoundEvent]) -> RoundLog:
    """Append ``events`` to round ``round_index``'s real log."""
    log = RoundLog(workspace, EPOCH, round_index)
    for event in events:
        log.append(event)
    return log


def _complete_events() -> list[RoundEvent]:
    """A round the endpoint consumed: open → propose → duel → gate → close."""
    events: list[RoundEvent] = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=()),
        CandidateSampled(i=1, n=1),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
    ]
    for entry_id in ENTRIES:
        events.append(UnitCompleted(entry_id=entry_id, replicate=0, side="parent"))
        events.append(UnitCompleted(entry_id=entry_id, replicate=0, side="child"))
    events.extend(
        [
            GateEvaluated(
                rule_fired="",
                decision="promoted",
                champion_scalar=0.42,
                challenger_scalar=0.31,
                margin_required=0.05,
            ),
            DecisionRecorded(decision="promoted", provenance={"delta_scalar": -0.11}),
            RoundClosed(),
        ]
    )
    return events


CREDENTIAL_ERROR = "AuthenticationError: 401 Unauthorized — API key expired or revoked"


def _degraded_events() -> list[RoundEvent]:
    """The proposer WAS reached and returned an invalid patch. No gate."""
    return [
        RoundOpened(contract_hash="sha256:contract-t0"),
        CandidateSampled(i=1, n=1),
        ProposalAttempted(errors=("invalid JSON in the proposed experiment",)),
        CandidateSampled(i=2, n=2),
        ProposalAttempted(errors=("post-apply validation rejected the patch set",)),
        ValidationFailed(findings=("agent/agent.py: post-apply syntax error at line 12",)),
        RoundClosed(),
    ]


def _outage_events() -> list[RoundEvent]:
    """Closed, no gate, and the credential lapse is right there in the log."""
    return [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
        RoundClosed(),
    ]


def _torn_events() -> list[RoundEvent]:
    """Opened, gated — but never closed. Deliberately carries a gate."""
    return [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=()),
        CandidateSampled(i=1, n=1),
        ExperimentMinted(experiment_id="exp-v9"),
        PatchesApplied(generation_id="v9"),
        GateEvaluated(rule_fired="", decision="promoted"),
    ]


# ---------------------------------------------------------------------------
# 1. COMPLETE — settled with a gate evaluation
# ---------------------------------------------------------------------------


def test_complete_round(tmp_path: Path) -> None:
    """A round that opened, duelled, gated, and closed is `complete`."""
    _write(tmp_path, 1, _complete_events())
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.COMPLETE
    assert verdict.opened and verdict.closed and verdict.settled
    assert verdict.gate_count == 1
    assert verdict.proposer_reached
    assert not verdict.invalid_patch
    assert verdict.infra_markers == ()
    assert verdict.log_path == f"epochs/{EPOCH}/rounds/1/round_log.jsonl"
    assert any("1 gate evaluation" in line for line in verdict.evidence)


# ---------------------------------------------------------------------------
# 2. SETTLED_DEGRADED — the deliberately-narrow acceptance
# ---------------------------------------------------------------------------


def test_settled_degraded_round_is_a_real_measurement(tmp_path: Path) -> None:
    """Proposer reached + invalid patch + no gate ⇒ `settled_degraded`.

    ACCEPTING this is the point of the status. A round where the
    proposer really was reached and really did return an invalid patch
    IS a measurement — the arm's honest result is "cannot produce a
    valid child". Voiding it would send a legitimately-degraded arm back
    through the retry loop to exhaustion, burning the sweep's budget
    re-measuring a result it already has.
    """
    _write(tmp_path, 1, _degraded_events())
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.gate_count == 0
    assert verdict.proposer_reached
    assert verdict.invalid_patch
    assert verdict.infra_markers == ()

    # ... and it does NOT disqualify the cell.
    report = epoch_round_integrity(tmp_path, EPOCH)
    assert report.accepted
    assert report.settled_degraded_count == 1
    assert report.void_count == 0


def test_degraded_round_needs_the_proposer_to_have_been_reached(tmp_path: Path) -> None:
    """An invalid patch with NO proposer-reached token is still void.

    The acceptance is narrow on purpose: "the loop recorded an error"
    is not evidence a measurement happened. Without a sampled candidate,
    a minted experiment, or an applied generation there is nothing that
    could only exist after the model answered.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=("invalid JSON in the proposed experiment",)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID
    assert not verdict.proposer_reached
    assert verdict.invalid_patch
    assert any("without evidence the proposer was reached" in x for x in verdict.evidence)


# ---------------------------------------------------------------------------
# 3. VOID — no completion marker (rule 1 outranks the gate)
# ---------------------------------------------------------------------------


def test_void_when_the_round_never_closed(tmp_path: Path) -> None:
    """An unclosed round is void EVEN THOUGH it carries a gate.

    Rule 1 outranks rule 2 deliberately: a round that never closed may
    have had more duels coming, so its partial result must not
    contribute a truncated mean.
    """
    _write(tmp_path, 1, _torn_events())
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID
    assert verdict.opened and not verdict.closed
    assert verdict.gate_count == 1  # present, and still not enough
    assert any("no completion marker" in line for line in verdict.evidence)
    assert any("opened but never closed" in line for line in verdict.evidence)


# ---------------------------------------------------------------------------
# 4. VOID — zero gates plus a hard credential failure
# ---------------------------------------------------------------------------


def test_void_on_credential_outage_surfaces_the_marker_verbatim(tmp_path: Path) -> None:
    """The outage case: closed, no gate, credential error in the log.

    The matched string comes back VERBATIM — an operator triaging a
    sweep needs the endpoint's own words to tell a credential lapse from
    a quota wall, and a boolean throws exactly that away.
    """
    _write(tmp_path, 1, _outage_events())
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID
    assert verdict.closed
    assert verdict.gate_count == 0
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)  # de-duplicated, verbatim
    assert any(CREDENTIAL_ERROR in line for line in verdict.evidence)
    assert any("hard infra error" in line for line in verdict.evidence)


def test_a_gate_outranks_another_challenger_s_infra_error(tmp_path: Path) -> None:
    """Rule 2 beats rule 3: a gated round is complete, whatever else died in it.

    The shape is the real one. `_propose_and_apply` emits an errored
    ``proposal_attempted`` per failed attempt ONLY on the raising path,
    and an empty one plus ``experiment_minted`` / ``patches_applied`` on
    success (``evolve/propose_apply.py``) — so errors and a gate coexist
    in one record when the round ran SEVERAL challengers and an earlier
    one died on a credential blip while a later one was served and
    duelled. ``proposal.errors`` is a round-level accumulation, so that
    stale marker is still in the fold.

    That round measured what it claims to have measured. Voiding it on
    the stale marker would discard a good duel and, in a sweep, retry an
    arm with nothing wrong with it.
    """
    events = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        # Challenger 1 — propose raised; one event per failed attempt.
        ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
        # Challenger 2 — served, applied, duelled.
        ProposalAttempted(errors=()),
        CandidateSampled(i=1, n=1),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
        GateEvaluated(
            rule_fired="",
            decision="rejected",
            champion_scalar=0.42,
            challenger_scalar=0.44,
            margin_required=0.05,
        ),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.COMPLETE
    assert verdict.gate_count == 1
    # The marker is still REPORTED — it just does not decide the verdict.
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)
    assert any("settled with 1 gate evaluation(s)" in line for line in verdict.evidence)
    assert epoch_round_integrity(tmp_path, EPOCH).accepted


def test_forbidden_id_rejection_is_degraded_not_void(tmp_path: Path) -> None:
    """A forbidden-id rejection is a MEASUREMENT, not an infra failure.

    Regression pin. zicato's own proposer emits forbidden-id rejections
    as free-text proposal errors — the exact strings below come from
    ``zicato/proposer/brief.py:209`` (``check_forbidden_ids``) and
    ``zicato/mutation/validator.py:365``. Both are the canonical
    proposer-was-reached-and-returned-an-invalid-patch case, i.e.
    `settled_degraded`.

    A bare ``"forbidden"`` marker in the vocabulary would make rule 3
    fire ahead of rule 4 and void these rounds — precisely the failure
    the narrow acceptance exists to prevent, and it would burn the arm's
    retry budget re-measuring a result already in hand. Only the
    unambiguous HTTP reason phrase ``403 forbidden`` may match.
    """
    brief_error = "patch 'p1' targets forbidden mutation id 'sys_prompt'"
    validator_error = (
        "Patch 'p2': mutation_id 'tool_choice' is in the forbidden set and may not be patched"
    )
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            PatchesApplied(generation_id="v1"),
            ProposalAttempted(errors=(brief_error, validator_error)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.infra_markers == ()
    assert verdict.proposer_reached and verdict.invalid_patch
    assert epoch_round_integrity(tmp_path, EPOCH).accepted


def test_infra_tokens_in_a_validation_finding_never_void_a_round(tmp_path: Path) -> None:
    """Markers are scanned over proposal errors ONLY.

    A validation finding proves a patch EXISTED, so the proposer was
    reached and the round is a real measurement. Scanning findings for
    infra tokens could only ever manufacture a false void — here the
    finding is a mutated agent whose own source mentions credentials.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            PatchesApplied(generation_id="v1"),
            ValidationFailed(
                findings=("agent/auth.py: undefined name 'credential' at line 12",),
            ),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.infra_markers == ()
    # The finding still shows up as evidence — it explains the degradation.
    assert any("validation finding" in line for line in verdict.evidence)


def test_bare_numeric_status_codes_do_not_match(tmp_path: Path) -> None:
    """Digit runs inside ordinary prose must not trip the marker rule.

    Ids, byte offsets, and line numbers all carry three-digit runs. Only
    the HTTP reason phrases match, so a proposal error that merely
    happens to contain `403` stays a real measurement.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            PatchesApplied(generation_id="v1"),
            ProposalAttempted(errors=("patch 'p403' failed to parse at offset 1401",)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.infra_markers == ()


def test_a_mechanically_recombined_candidate_does_not_vouch_for_the_endpoint(
    tmp_path: Path,
) -> None:
    """Rule 3 is what protects the `recombine` arms, and this pins it.

    A mechanical recombination mint emits ``candidate_sampled`` having
    made NO model call — ``mint_recombined_experiment`` is pure
    (``proposer/recombine.py``), and the surviving slot's event is
    emitted the same way an ordinary sample's is
    (``proposer/best_of_n.py``). So on an A3/A7 cell
    ``candidates_sampled > 0`` can be true across a credential outage,
    and `proposer_reached` alone would wave the round through as a real
    degraded measurement.

    It does not, because rule 3 outranks rule 4: the matched marker
    voids the round despite the candidate. That ordering is the whole
    protection here, so a change that let rule 4 run first would silently
    re-admit outage rounds on exactly the arms that mint locally.
    """
    events = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        CandidateSampled(i=1, n=3),  # the mint — no model call behind it
        ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.proposer_reached  # the flag is set ...
    assert verdict.status == RoundStatus.VOID  # ... and does not decide it
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)


def test_infra_marker_vocabulary_is_an_explicit_parameter(tmp_path: Path) -> None:
    """A caller can widen the vocabulary without editing the module.

    The default set is a FLOOR on detection, not a proof: an endpoint
    whose error prose uses none of its tokens reads back as a plain
    proposer failure. This pins the widening seam that makes that
    false-negative recoverable per-caller.
    """
    quiet_outage = "upstream said no (code ZX-9)"
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            ProposalAttempted(errors=(quiet_outage,)),
            RoundClosed(),
        ],
    )
    # With the default vocabulary it is a degraded-but-real measurement.
    assert round_integrity(tmp_path, EPOCH, 1).status == RoundStatus.SETTLED_DEGRADED
    # Widened, the same log is a void infra failure.
    widened = round_integrity(tmp_path, EPOCH, 1, infra_markers=HARD_INFRA_MARKERS | {"zx-9"})
    assert widened.status == RoundStatus.VOID
    assert widened.infra_markers == (quiet_outage,)


# ---------------------------------------------------------------------------
# 5. Epoch-level: counts, verdict, and NUMERIC round ordering
# ---------------------------------------------------------------------------


def _contaminated_epoch(workspace: Path) -> EpochRoundIntegrity:
    """Rounds 1 / 2 / 10 / 11: complete, degraded, outage-void, torn-void."""
    _write(workspace, 1, _complete_events())
    _write(workspace, 2, _degraded_events())
    _write(workspace, 10, _outage_events())
    _write(workspace, 11, _torn_events())
    return epoch_round_integrity(workspace, EPOCH)


def test_epoch_mixes_every_status_and_is_rejected(tmp_path: Path) -> None:
    """One void round is enough to reject the whole cell."""
    report = _contaminated_epoch(tmp_path)

    assert report.epoch_id == EPOCH
    assert report.round_count == 4
    assert report.complete_count == 1
    assert report.settled_degraded_count == 1
    assert report.void_count == 2
    assert report.counts == {"complete": 1, "settled_degraded": 1, "void": 2}
    assert not report.accepted
    assert not report.no_rounds


def test_rounds_sort_numerically_not_lexicographically(tmp_path: Path) -> None:
    """Round 10 sorts LAST, not between 1 and 2.

    Lexicographic ordering on the directory names would interleave the
    rounds and misreport which round of the sweep went dark.
    """
    report = _contaminated_epoch(tmp_path)
    assert [entry.round_index for entry in report.rounds] == [1, 2, 10, 11]


def test_non_integer_round_directories_are_skipped(tmp_path: Path) -> None:
    """A directory that is not a round number is not a round."""
    _write(tmp_path, 1, _complete_events())
    (tmp_path / "epochs" / EPOCH / "rounds" / "scratch").mkdir(parents=True)
    report = epoch_round_integrity(tmp_path, EPOCH)
    assert [entry.round_index for entry in report.rounds] == [1]


def test_missing_rounds_tree_is_empty_but_visibly_so(tmp_path: Path) -> None:
    """No rounds at all is vacuously accepted — and flagged as empty.

    An epoch nothing ever ran is not a healthy epoch, it is an
    unmeasured one; `no_rounds` is what keeps emptiness from reading as
    health.
    """
    report = epoch_round_integrity(tmp_path, "never-ran")
    assert report.rounds == ()
    assert report.no_rounds
    # Vacuously true — which is precisely why no gating caller may read it
    # alone. See `test_cli_verify_fails_an_epoch_that_measured_nothing`.
    assert report.accepted


# ---------------------------------------------------------------------------
# 6. VOID — a round directory with no log at all
# ---------------------------------------------------------------------------


def test_round_directory_without_a_log_is_void(tmp_path: Path) -> None:
    """An empty round directory is not evidence of a measurement."""
    round_dir(tmp_path, EPOCH, 3).mkdir(parents=True)
    report = epoch_round_integrity(tmp_path, EPOCH)

    assert [entry.round_index for entry in report.rounds] == [3]
    verdict = report.rounds[0]
    assert verdict.status == RoundStatus.VOID
    assert not verdict.opened and not verdict.closed
    assert any("never opened" in line for line in verdict.evidence)
    assert not report.accepted


# ---------------------------------------------------------------------------
# 7. VOID — interior corruption, and the walk survives it
# ---------------------------------------------------------------------------


def test_interior_corruption_is_void_and_does_not_raise(tmp_path: Path) -> None:
    """A garbled non-tail line voids its round without killing the sweep.

    ``RoundLog.read()`` raises on interior corruption by design (the
    append-only invariant was violated). A sweep-wide verification that
    propagated that would abandon every remaining cell — precisely the
    blindness this check exists to remove.
    """
    # Hand-written on purpose: this test is ABOUT malformed bytes, which
    # the real writer cannot produce.
    log_dir = round_dir(tmp_path, EPOCH, 1)
    log_dir.mkdir(parents=True)
    good = json.dumps({"seq": 1, "ts": "t", "type": "round_opened", "payload": {}})
    close = json.dumps({"seq": 3, "ts": "t", "type": "round_closed", "payload": {}})
    (log_dir / "round_log.jsonl").write_text(
        f"{good}\n{{ not json at all\n{close}\n", encoding="utf-8"
    )
    _write(tmp_path, 2, _complete_events())

    report = epoch_round_integrity(tmp_path, EPOCH)  # must not raise
    corrupt = report.rounds[0]
    assert corrupt.round_index == 1
    assert corrupt.status == RoundStatus.VOID
    assert any("unreadable" in line for line in corrupt.evidence)
    assert any("line 2 is corrupt" in line for line in corrupt.evidence)
    # The rest of the epoch is still classified.
    assert report.rounds[1].status == RoundStatus.COMPLETE
    assert not report.accepted


def test_torn_tail_is_tolerated_not_treated_as_corruption(tmp_path: Path) -> None:
    """A torn FINAL line is the crash-mid-append case the reader forgives.

    The round is still void — it never closed — but for the honest
    reason (no completion marker), not "unreadable".
    """
    log = _write(tmp_path, 1, _torn_events())
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 7, "ts": "t", "type": "round_cl')

    verdict = round_integrity(tmp_path, EPOCH, 1)
    assert verdict.status == RoundStatus.VOID
    assert any("no completion marker" in line for line in verdict.evidence)
    assert not any("unreadable" in line for line in verdict.evidence)


# ---------------------------------------------------------------------------
# 8. CLI — `zicato epoch rounds`
# ---------------------------------------------------------------------------


def test_cli_renders_the_evidence_and_the_verdict(tmp_path: Path) -> None:
    """Default output shows WHY each round was called, not a boolean."""
    from zicato.cli.commands.epoch import epoch_grp

    _contaminated_epoch(tmp_path)
    result = CliRunner().invoke(
        epoch_grp, ["rounds", "--workspace", str(tmp_path), "--epoch", EPOCH]
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "complete" in out and "settled_degraded" in out and "void" in out
    # The endpoint's own words, verbatim — the whole point of the render.
    assert CREDENTIAL_ERROR in out
    assert "no completion marker" in out
    assert "NOT ACCEPTED" in out
    assert "4 round(s)" in out


def test_cli_verify_exit_codes(tmp_path: Path) -> None:
    """--verify makes the acceptance verdict load-bearing; bare does not."""
    from zicato.cli.commands.epoch import epoch_grp

    runner = CliRunner()
    _contaminated_epoch(tmp_path)
    args = ["rounds", "--workspace", str(tmp_path), "--epoch", EPOCH]

    # Pure inspection always exits 0.
    assert runner.invoke(epoch_grp, args).exit_code == 0
    # ... --verify does not.
    assert runner.invoke(epoch_grp, [*args, "--verify"]).exit_code == 1

    clean = tmp_path / "clean"
    _write(clean, 1, _complete_events())
    _write(clean, 2, _degraded_events())
    clean_args = ["rounds", "--workspace", str(clean), "--epoch", EPOCH, "--verify"]
    result = runner.invoke(epoch_grp, clean_args)
    assert result.exit_code == 0, result.output
    assert "ACCEPTED" in result.output


def test_cli_verify_fails_an_epoch_that_measured_nothing(tmp_path: Path) -> None:
    """An empty epoch is vacuously accepted — and --verify must still fail it.

    The campaign protocol gates a cell on this exit code, so an `evolve`
    that died before writing its first round log would otherwise clear
    the integrity check having measured nothing at all: the same
    "healthy at the wrong granularity" failure the command exists to
    catch, one level up. `accepted` stays true (the JSON must not lie
    about an empty set containing no void round); the GATE reads
    `no_rounds` too.
    """
    from zicato.cli.commands.epoch import epoch_grp

    runner = CliRunner()
    args = ["rounds", "--workspace", str(tmp_path), "--epoch", "never-ran"]

    inspection = runner.invoke(epoch_grp, args)
    assert inspection.exit_code == 0
    assert "NO ROUNDS" in inspection.output
    # The verdict line withholds "ACCEPTED" rather than contradicting it.
    assert "NO MEASUREMENT" in inspection.output
    assert "VERDICT: ACCEPTED" not in inspection.output

    assert runner.invoke(epoch_grp, [*args, "--verify"]).exit_code == 1

    payload = json.loads(runner.invoke(epoch_grp, [*args, "--json"]).output)
    assert payload["no_rounds"] is True
    assert payload["accepted"] is True  # vacuous, and reported as such
    assert payload["round_count"] == 0


def test_cli_json_is_machine_readable(tmp_path: Path) -> None:
    """--json carries the rounds, the counts, and the verdict."""
    from zicato.cli.commands.epoch import epoch_grp

    _contaminated_epoch(tmp_path)
    result = CliRunner().invoke(
        epoch_grp, ["rounds", "--workspace", str(tmp_path), "--epoch", EPOCH, "--json"]
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["epoch_id"] == EPOCH
    assert payload["accepted"] is False
    assert payload["no_rounds"] is False
    assert payload["counts"] == {"complete": 1, "settled_degraded": 1, "void": 2}
    assert [r["round_index"] for r in payload["rounds"]] == [1, 2, 10, 11]
    assert [r["status"] for r in payload["rounds"]] == [
        "complete",
        "settled_degraded",
        "void",
        "void",
    ]
    outage = payload["rounds"][2]
    assert outage["infra_markers"] == [CREDENTIAL_ERROR]
    assert outage["log_path"] == f"epochs/{EPOCH}/rounds/10/round_log.jsonl"


def test_cli_defaults_to_the_current_epoch(tmp_path: Path) -> None:
    """--epoch is optional; the workspace's current-epoch marker wins."""
    from zicato.cli.commands.epoch import epoch_grp
    from zicato.epoch.lifecycle import switch_epoch

    _write(tmp_path, 1, _complete_events())
    switch_epoch(tmp_path, EPOCH)

    result = CliRunner().invoke(epoch_grp, ["rounds", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"Epoch {EPOCH}" in result.output
    assert "ACCEPTED" in result.output


def test_cli_errors_without_an_epoch(tmp_path: Path) -> None:
    """No --epoch and no current-epoch marker is a usage error, not a crash."""
    from zicato.cli.commands.epoch import epoch_grp

    result = CliRunner().invoke(epoch_grp, ["rounds", "--workspace", str(tmp_path)])
    assert result.exit_code != 0
    assert "no current_epoch marker" in result.output
