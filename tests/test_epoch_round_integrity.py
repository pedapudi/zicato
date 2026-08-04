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
    CALL_BOUNDARY_PREFIXES,
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


#: The REAL emitted shape: `proposer.py`'s call-boundary template
#: (`f"auxiliary LLM call raised {type(exc).__name__}: {exc}"`) wrapping an
#: SDK credential error. The prefix is load-bearing, not decoration — see
#: `test_a_content_rejection_is_never_read_as_an_outage`.
CREDENTIAL_ERROR = (
    "auxiliary LLM call raised AuthenticationError: "
    "401 Unauthorized — API key expired or revoked"
)


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
    # The PRODUCTION shape: `enforce_forbidden` returns the bare strings,
    # and the retry loop always wraps the joined list in its own prefix
    # before it reaches the log — the unprefixed form never appears in a
    # `proposal_attempted`. (`mutation/validator.py::check_forbidden_ids`
    # emits a similar string, but its only caller raises BadPatchSetError
    # after the proposer returned, so it cannot reach this channel at all.)
    brief_error = (
        "patches violate proposer-brief forbidden-edits list: "
        "patch 'p1' targets forbidden mutation id 'sys_prompt'; "
        "patch 'p2' targets forbidden mutation id 'tool_choice'"
    )
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            PatchesApplied(generation_id="v1"),
            ProposalAttempted(errors=(brief_error,)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.infra_markers == ()
    assert verdict.proposer_reached and verdict.invalid_patch
    assert epoch_round_integrity(tmp_path, EPOCH).accepted


#: The four REAL false-positive vectors, each a production template that can
#: carry a marker substring while meaning the OPPOSITE of an outage. Every
#: string here was traced to its emitter; none is invented.
CONTENT_REJECTIONS_CARRYING_MARKERS = (
    # 1. `resource_exhausted` is a BUILT-IN goldfive drift kind, and the
    #    parse error prints the whole sorted list (`proposer/structured.py`
    #    renders it from `core/drift_kinds.py`). Deterministic: any
    #    `drift:`-prefixed metric name that is not a built-in kind emits it.
    "metric_name 'drift:latency_p99' is not a declared judge. Built-in drift "
    "kinds (use as 'drift:<kind>'): incomplete_output, resource_exhausted, "
    "schema_violation, tool_error",
    # 2. A LOCAL PermissionError while reading a file to validate it
    #    (`mutation/validator.py` wraps OSError), then wrapped again by the
    #    retry loop. File mode, root-owned scratch, NFS — nothing to do with
    #    the endpoint, on a round that DID produce a patch set.
    "patches failed post-apply validation: Could not read "
    "/tmp/ztw-slate-9f2/agent/tools.py: [Errno 13] Permission denied: "
    "'/tmp/ztw-slate-9f2/agent/tools.py'",
    # 3. Mutation ids are arbitrary OPERATOR strings off `# zicato:mutable
    #    id="..."`, echoed verbatim (`proposer/brief.py::enforce_forbidden`,
    #    then wrapped). `api_key_env` is an entirely ordinary id.
    "patches violate proposer-brief forbidden-edits list: patch 'p1' targets "
    "forbidden mutation id 'client.api_key_env'",
    # 4. jsonschema echoes the MODEL'S OWN instance, and `new_content` is a
    #    plain string schema — so a mistyped patch renders the proposer's
    #    replacement source code into the error (`proposer/structured.py`).
    "schema violation at patches.0.new_content: 42 is not of type 'string' "
    "(instance: headers['authorization'] = f'Bearer {self.api_key}')",
)


def test_a_content_rejection_is_never_read_as_an_outage(tmp_path: Path) -> None:
    """Regression pin. Content rejections quote text zicato does not control.

    Each string in `CONTENT_REJECTIONS_CARRYING_MARKERS` is a production
    template that reaches `proposal.errors` (via `ProposerError.attempts`,
    which `evolve/propose_apply.py` writes out as `proposal_attempted`)
    carrying a `HARD_INFRA_MARKERS` substring — while meaning the precise
    opposite of an outage: the endpoint answered, and what came back was
    bad. Two of the four are not even about the model (a built-in drift-kind
    list; a local `PermissionError`), and two quote strings the OPERATOR or
    the MODEL authored.

    Voiding these would retry a legitimately-degraded arm to exhaustion —
    and, because arms differ in how often they emit invalid patches, it
    would delete rounds in an ARM-CORRELATED pattern, manufacturing the
    exact contamination shape this module exists to detect.

    The prefix anchor is what makes them ineligible. Note that two of the
    four carry NO call-boundary prefix at all and two carry a *content*
    prefix — the anchor covers both shapes, which is why it is the
    mechanism and the vocabulary is only a floor on top of it.
    """
    # Each really does contain a marker — otherwise this pin proves nothing.
    for text in CONTENT_REJECTIONS_CARRYING_MARKERS:
        lowered = text.lower()
        assert any(marker in lowered for marker in HARD_INFRA_MARKERS), text

    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            ProposalAttempted(errors=CONTENT_REJECTIONS_CARRYING_MARKERS),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.infra_markers == ()
    assert epoch_round_integrity(tmp_path, EPOCH).accepted
    # The errors are still rendered — they explain the degradation.
    assert any("proposal error" in line for line in verdict.evidence)


def test_a_content_rejection_never_voids_a_round_that_gated(tmp_path: Path) -> None:
    """The same four strings on a round that DID duel stay `complete`.

    Rule 2 already outranks rule 3, so this cannot regress through the
    marker path — but a future rule inserted ahead of the gate check
    could, and a `complete` round wrongly voided is a deleted duel.
    """
    events = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=CONTENT_REJECTIONS_CARRYING_MARKERS),
        ProposalAttempted(errors=()),
        CandidateSampled(i=1, n=1),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
        GateEvaluated(rule_fired="", decision="rejected"),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.COMPLETE
    assert verdict.infra_markers == ()


def test_call_boundary_prefixes_match_the_real_emitters(tmp_path: Path) -> None:
    """The eligibility prefixes are pinned to the templates that emit them.

    If a proposer refactor renames one of these, this test fails rather
    than the check silently going quiet. The failure direction is safe —
    an unmatched outage still voids by rule 5 — but it costs the operator
    the reason, which on a credential lapse is the whole triage.
    """
    real_templates = (
        # proposer.py — the aux-call boundary, both exception paths.
        f"auxiliary LLM call raised {ConnectionError.__name__}: connection refused",
        "auxiliary LLM call timed out after 120.0s",
        # adk_agent.py — the ADK agent-run boundary.
        "proposer agent run raised PermissionDenied: 403 Forbidden",
    )
    for template in real_templates:
        assert template.lower().startswith(CALL_BOUNDARY_PREFIXES), template

    # And the eligible ones that name hard infra really do classify void.
    for index, template in enumerate((real_templates[0], real_templates[2]), start=1):
        _write(
            tmp_path,
            index,
            [
                RoundOpened(contract_hash="sha256:contract-t0"),
                ProposalAttempted(errors=(template,)),
                RoundClosed(),
            ],
        )
        assert round_integrity(tmp_path, EPOCH, index).status == RoundStatus.VOID

    # The timeout template is eligible but deliberately carries no marker:
    # one attempt timing out is not proof the endpoint was never reached.
    _write(
        tmp_path,
        3,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=(real_templates[1],)),
            RoundClosed(),
        ],
    )
    assert round_integrity(tmp_path, EPOCH, 3).infra_markers == ()


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


def test_a_mechanical_recombination_mint_is_not_evidence_of_reach(
    tmp_path: Path,
) -> None:
    """Misclassification A: a round with ZERO model responses, accepted.

    A mechanical recombination mint emits ``candidate_sampled`` having
    made NO model call — ``mint_recombined_experiment`` is pure
    (``proposer/recombine.py``) — and best-of-N DISCARDS the failed slots'
    errors whenever any slot survives (``proposer/best_of_n.py``). So on an
    A3/A7 cell mid-outage: the LLM slots raise 401s that never reach the
    log, the mint survives, its mount then fails, and the round closes
    with ``candidates_sampled=1``, ``proposal.errors=()``, and no gate.

    Under a plain ``candidates_sampled > 0`` that reads as
    `settled_degraded` — an ACCEPTED cell whose mean is built without the
    endpoint ever having answered. The recombined count is folded already
    (``round_log.py``), so the predicate subtracts it and the round lands
    void by rule 5, on the honest reason.
    """
    events = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        CandidateSampled(i=2, n=3, recombined=True),  # no model call behind it
        ValidationFailed(findings=("mount failed: derived tree is not importable",)),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert not verdict.proposer_reached
    assert verdict.status == RoundStatus.VOID
    assert verdict.infra_markers == ()
    assert any("without evidence the proposer was reached" in v for v in verdict.evidence)
    assert not epoch_round_integrity(tmp_path, EPOCH).accepted


def test_rule_five_never_denies_the_evidence_printed_beside_it(tmp_path: Path) -> None:
    """Misclassification B: the verdict was right, the reason was a lie.

    A recombination mint that DOES apply emits `experiment_minted` and
    `patches_applied`, so `proposer_reached` is satisfied on those tokens
    however the candidate was produced. If the round then dies on infra
    deferred past the gate, it reaches rule 5 — correctly void — but the
    blanket reason "without evidence the proposer was reached" printed
    directly above an evidence line reading "proposer reached: ..." makes
    the report argue with itself, and the operator triages from that text.
    """
    events = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        CandidateSampled(i=2, n=3, recombined=True),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID  # verdict unchanged
    assert verdict.proposer_reached
    assert any("despite proposer activity" in v for v in verdict.evidence)
    assert not any("without evidence the proposer was reached" in v for v in verdict.evidence)
    # The two lines now agree with each other.
    assert any("proposer reached:" in v for v in verdict.evidence)


def test_only_the_final_attempt_span_is_classified(tmp_path: Path) -> None:
    """Misclassification C: a prior attempt vouching for a later one.

    The log is append-only and a round INDEX can be reused: `evolve/loop.py`
    derives the next index from the highest *persisted*
    `experiment.round_index`, so an attempt that applied patches but died
    before its experiment was written never consumes its index, and the
    next invocation opens the same round and appends to the same file.
    `fold_round_record` has no attempt scope — it accumulates across the
    whole stream and reduces the lifecycle markers to two booleans — so the
    first attempt's `candidate_sampled` / `patches_applied` would satisfy
    `proposer_reached` for a second attempt that only ever saw a 401.
    """
    _write(
        tmp_path,
        1,
        [
            # Attempt 1 — reached the model, applied patches, then died.
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=()),
            CandidateSampled(i=1, n=1),
            ExperimentMinted(experiment_id="exp-v1"),
            PatchesApplied(generation_id="v1"),
            # Attempt 2 — same index, endpoint now refusing.
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert not verdict.proposer_reached  # attempt 1's tokens do not carry over
    assert verdict.status == RoundStatus.VOID
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)
    assert any("hard infra error" in v for v in verdict.evidence)


def test_a_reused_round_index_does_not_inherit_a_prior_gate(tmp_path: Path) -> None:
    """The same span rule, in the direction that would manufacture a duel.

    A first attempt that gated and closed, followed by a second attempt at
    the same index that produced nothing, must not read as `complete` on
    the first attempt's gate — the epoch carries the LAST attempt.
    """
    _write(tmp_path, 1, _complete_events())
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=(CREDENTIAL_ERROR,)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.gate_count == 0
    assert verdict.status == RoundStatus.VOID


#: A CALL-BOUNDARY error whose prose matches no default marker — the
#: vocabulary MISS the whole floor/anchor argument turns on.
QUIET_OUTAGE = "auxiliary LLM call raised UpstreamError: upstream said no (code ZX-9)"


def test_infra_marker_vocabulary_is_an_explicit_parameter(tmp_path: Path) -> None:
    """A caller can widen the vocabulary without editing the module.

    The default set is a FLOOR on detection, not a proof: an endpoint
    whose error prose uses none of its tokens goes unmatched. This pins the
    widening seam that makes that false-negative recoverable per-caller.

    The round is built to be a GENUINELY degraded one — a content rejection
    the proposer really did produce — so that the default verdict is
    ``settled_degraded`` on its own merits and the widening is the only
    thing that changes it. Pinning the seam on a transport-error-only round
    would be pinning the wrong thing: such a round is void either way (see
    :func:`test_unmatched_call_boundary_error_alone_never_buys_acceptance`).
    """
    real_rejection = "patches failed post-apply validation: agent/agent.py: undefined name 'json'"
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            ProposalAttempted(errors=(real_rejection,)),
            CandidateSampled(i=2, n=2),
            ProposalAttempted(errors=(QUIET_OUTAGE,)),
            RoundClosed(),
        ],
    )
    # With the default vocabulary the outage goes unmatched, and what is left
    # on the record is a real measurement of a degraded arm.
    default = round_integrity(tmp_path, EPOCH, 1)
    assert default.status == RoundStatus.SETTLED_DEGRADED
    assert default.infra_markers == ()
    # Widened, the same log is a void infra failure — rule 3 outranks rule 4.
    widened = round_integrity(tmp_path, EPOCH, 1, infra_markers=HARD_INFRA_MARKERS | {"zx-9"})
    assert widened.status == RoundStatus.VOID
    assert widened.infra_markers == (QUIET_OUTAGE,)


def test_unmatched_call_boundary_error_alone_never_buys_acceptance(tmp_path: Path) -> None:
    """A transport failure is the ABSENCE of a patch, not an invalid one.

    The round issue #141 made reachable: a best-of-N slate where one slot
    survives — minting the reach token — while a sibling slot dies at the
    call boundary in prose the default vocabulary does not know. Before
    #141 the sibling's error was discarded and the round voided by rule 5.
    Emitting it must not be what PROMOTES the round to ``settled_degraded``:
    reporting more evidence can only ever move a verdict toward void.

    This is also what makes a vocabulary miss survivable at all. Rule 3's
    marker set is a floor, and the fallthrough that catches everything it
    misses is rule 5 — which only holds if an unmatched transport error
    cannot satisfy rule 4 by itself.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            # The surviving slot: a real reach token, no error of its own.
            CandidateSampled(i=0, n=2),
            # The dead sibling, reported for the first time since #141.
            ProposalAttempted(errors=(QUIET_OUTAGE,), slot_index=1),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID
    assert verdict.proposer_reached
    assert not verdict.invalid_patch
    assert verdict.infra_markers == ()
    # The void is ACTIONABLE: it names the error and the remedy.
    assert any("matched no infra marker" in line for line in verdict.evidence)
    assert any("widening `infra_markers`" in line for line in verdict.evidence)
    assert not epoch_round_integrity(tmp_path, EPOCH).accepted


def test_call_boundary_error_beside_a_content_rejection_still_accepts(tmp_path: Path) -> None:
    """The tightening excludes transport errors; it does not suppress evidence.

    A round holding BOTH is still a real measurement — the content rejection
    proves a patch existed and was rejected — so rule 4 accepts it exactly as
    before. Only the transport error alone was ever doing illegitimate work.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=0, n=2),
            ProposalAttempted(errors=(QUIET_OUTAGE,), slot_index=1),
            ProposalAttempted(
                errors=("schema violation at patches[0]: 'op' is a required property",)
            ),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.SETTLED_DEGRADED
    assert verdict.invalid_patch
    # Both errors stay on the report; only the PREDICATE changed.
    assert any(QUIET_OUTAGE in line for line in verdict.evidence)
    assert any("schema violation" in line for line in verdict.evidence)


def test_slot_tagged_content_rejection_is_not_invalid_patch_laundering(tmp_path: Path) -> None:
    """The slot tag must not turn a transport error into patch evidence.

    An all-failed slate raises one error carrying every slot's attempts,
    each prefixed ``slot N: `` (``proposer/best_of_n.py``), and
    ``evolve/propose_apply.py`` writes them out one event per attempt. The
    prefix is stripped before BOTH predicates — marker eligibility and
    invalid-patch evidence — so a slot-tagged transport error stays a
    transport error and a slot-tagged content rejection stays a rejection.
    Testing either predicate against the prefixed string would silently
    invert them on exactly the round that matters most.
    """
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=0, n=2),
            ProposalAttempted(errors=(f"slot 0: {QUIET_OUTAGE}",)),
            ProposalAttempted(errors=(f"slot 1: {CREDENTIAL_ERROR}",)),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    # Slot 1's credential lapse is matched THROUGH the tag: rule 3, void.
    assert verdict.status == RoundStatus.VOID
    assert verdict.infra_markers == (f"slot 1: {CREDENTIAL_ERROR}",)
    # ... and neither slot-tagged transport error counted as an invalid patch.
    assert not verdict.invalid_patch


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


# ---------------------------------------------------------------------------
# 7. Issue #141 — the reader now has the slate evidence it used to lack
#
# The writer-side half of this fix (``proposer/best_of_n.py``) puts every
# failed slate slot's error into the log. These pin what that buys the reader,
# and what must NOT change now that it has it.
# ---------------------------------------------------------------------------


def test_misclassification_a_is_caught_by_evidence_not_only_by_the_predicate(
    tmp_path: Path,
) -> None:
    """Defense in depth, PROVEN: the marker evidence alone voids the round.

    The Misclassification-A shape — every LLM slot dies on a credential lapse,
    the LAST slot mints mechanically, and the mint's mount then fails, so the
    round closes with one candidate and no gate. It used to reach here with
    ``proposal.errors=()``, and the ``recombined_sampled`` discriminator was
    the only thing standing between it and an accepted cell.

    So the discriminator is NEUTRALISED here — the mint is written as an
    ORDINARY ``candidate_sampled``, exactly as a real model response would
    be, which forces ``proposer_reached`` true and puts the round on rule 4's
    doorstep. It still lands VOID, because rule 3 outranks rule 4 and the
    failed slots' errors are now IN THE LOG. Two independent mechanisms, and
    this pins that neither is load-bearing alone.
    """
    events: list[RoundEvent] = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        # The two LLM slots, dead on a 401 — the events the fix added.
        ProposalAttempted(errors=(CREDENTIAL_ERROR,), slot_index=0),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,), slot_index=1),
        # The mechanical mint, deliberately NOT marked `recombined`.
        CandidateSampled(i=2, n=3),
        ValidationFailed(findings=("mount failed: derived tree is not importable",)),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    # The discriminator is off: the round DOES look like the proposer answered.
    assert verdict.proposer_reached
    # And it is void anyway, on the evidence, naming the outage.
    assert verdict.status == RoundStatus.VOID
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)
    assert any("carrying a hard infra error" in v for v in verdict.evidence)
    assert not epoch_round_integrity(tmp_path, EPOCH).accepted


def test_a_gated_round_with_failed_siblings_is_still_complete(tmp_path: Path) -> None:
    """Rule 2 still outranks rule 3 now that siblings write their errors.

    A slate of three where two slots died and the third gated IS a round the
    endpoint consumed — a duel happened and a decision came out of it. The new
    per-slot events must not turn it into an outage: voiding it would send a
    round that produced a real measurement back around the retry loop, which
    is the false positive the whole marker vocabulary is biased against.

    The marker is still REPORTED (the challenger-granularity limit in this
    module's docstring is exactly this evidence, waiting for a policy that
    acts on it) — it just does not decide the verdict.
    """
    events: list[RoundEvent] = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,), slot_index=0),
        ProposalAttempted(errors=(CREDENTIAL_ERROR,), slot_index=1),
        CandidateSampled(i=2, n=3),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
        GateEvaluated(rule_fired="", decision="rejected"),
        DecisionRecorded(decision="rejected", provenance={}),
        RoundClosed(),
    ]
    _write(tmp_path, 1, events)
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.COMPLETE
    assert verdict.gate_count == 1
    assert verdict.infra_markers == (CREDENTIAL_ERROR,)
    assert any("settled with 1 gate evaluation(s)" in v for v in verdict.evidence)
    assert epoch_round_integrity(tmp_path, EPOCH).accepted


def test_the_slot_tag_does_not_blind_the_marker_anchor(tmp_path: Path) -> None:
    """An all-failed slate's slot-prefixed attempts still match.

    When every slot fails, ``best_of_n`` aggregates them into one error and
    tags each attempt with its slot, so the operator can tell three slots
    failing one way from one slot failing three ways. The tag sits in FRONT of
    the call-boundary template the marker scan anchors on — so an anchor
    tested against the raw string would go blind on precisely the round this
    module exists for: a whole slate lost to a credential lapse.

    The matched string comes back WITH its tag: the slot is part of what the
    operator needs to read.
    """
    tagged = f"slot 0: {CREDENTIAL_ERROR}"
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=(tagged, f"slot 1: {CREDENTIAL_ERROR}")),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.status == RoundStatus.VOID
    assert verdict.infra_markers == (tagged, f"slot 1: {CREDENTIAL_ERROR}")
    assert any("carrying a hard infra error" in v for v in verdict.evidence)


def test_the_slot_tag_admits_no_model_authored_text(tmp_path: Path) -> None:
    """The stripped tag is one FIXED zicato prefix, not a general escape.

    Stripping a prefix before the anchor test is exactly the move that would
    re-open the false-positive hole ``CALL_BOUNDARY_PREFIXES`` closed, if the
    strip were loose. It is not: only a literal ``slot <digits>: `` comes off,
    and a content rejection that merely CONTAINS a call-boundary phrase — the
    challenger quoting an error string into its own patch — stays ineligible.
    """
    quoting_challenger = (
        "patches failed post-apply validation: agent/agent.py:31 raises "
        "RuntimeError('auxiliary LLM call raised AuthenticationError: api key')"
    )
    _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            CandidateSampled(i=1, n=1),
            ProposalAttempted(errors=(quoting_challenger,), slot_index=0),
            RoundClosed(),
        ],
    )
    verdict = round_integrity(tmp_path, EPOCH, 1)

    assert verdict.infra_markers == ()
    assert verdict.status == RoundStatus.SETTLED_DEGRADED


def test_slot_index_is_additive_on_the_wire(tmp_path: Path) -> None:
    """A pre-#141 log decodes unchanged; a tagged one round-trips its slot.

    ``proposal_attempted`` is the oldest event in the vocabulary and every
    log ever written carries some. The new field must therefore be inert on
    read: absent ⇒ ``None`` (the non-slate attempt ``evolve/propose_apply.py``
    emits), present ⇒ the slot, and neither changes the fold.
    """
    log = _write(
        tmp_path,
        1,
        [
            RoundOpened(contract_hash="sha256:contract-t0"),
            ProposalAttempted(errors=("boom",), slot_index=2),
            RoundClosed(),
        ],
    )
    # A log line written BEFORE the field existed — no `slot_index` key at all.
    path = round_dir(tmp_path, EPOCH, 1) / "round_log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    legacy = json.loads(lines[1])
    del legacy["payload"]["slot_index"]
    legacy["seq"] = 4
    path.write_text(
        "\n".join([*lines, json.dumps(legacy, separators=(",", ":"))]) + "\n",
        encoding="utf-8",
    )

    events = log.read()
    tagged = [e.event for e in events if isinstance(e.event, ProposalAttempted)]
    assert [a.slot_index for a in tagged] == [2, None]
    assert [a.errors for a in tagged] == [("boom",), ("boom",)]
