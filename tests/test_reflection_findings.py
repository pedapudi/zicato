"""Findings — evidence-linked, ranked, with signature-validated proposed ops.

Every ``proposed_op`` names a REAL builder op and is validated against that op's
signature at emit time; the margin finding is exactly a ``set_gate`` payload and
the pruning finding a ``set_weights`` payload, both of which round-trip through
:func:`inspect.signature` AND actually apply to a live
:class:`~zicato.builder.draft.TournamentDraft`. An emitter with an unknown arg
raises at emit time. The oracle FN finding NAMES the adjudicated span.
"""

from __future__ import annotations

import pytest

from zicato.builder import operations as ops
from zicato.builder.draft import TournamentDraft
from zicato.reflection.adjudicator import VERDICT_FN, VERDICT_FP, JudgeAdjudication
from zicato.reflection.corpus import FIDELITY_VERBATIM
from zicato.reflection.findings import (
    MARGIN_FLOOR_MULTIPLE,
    Finding,
    derive_findings,
    validate_proposed_op,
)
from zicato.reflection.scorecards import JudgeScorecard


def _card(
    judge: str,
    *,
    tp: int = 0,
    fp: int = 0,
    fn: int = 0,
    tn: int = 0,
    ambiguous: int = 0,
    precision: float | None = None,
    recall: float | None = None,
    exercised: bool = True,
    redundant_with: tuple = (),
    ambiguous_pile: bool = False,
) -> JudgeScorecard:
    return JudgeScorecard(
        judge_name=judge,
        n_decisions=tp + fp + fn + tn + ambiguous,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        ambiguous=ambiguous,
        precision=precision,
        recall=recall,
        f1=None,
        fpr=None,
        severity_accuracy=None,
        disagreement_rate=0.0,
        self_consistency_kappa=None,
        redundant_with=redundant_with,
        conflicts_with=(),
        exercised=exercised,
        ambiguous_pile=ambiguous_pile,
        fidelity_tiers=(FIDELITY_VERBATIM,),
    )


def _adj(judge: str, run_ref: str, verdict: str, span: str) -> JudgeAdjudication:
    return JudgeAdjudication(
        judge_name=judge,
        run_ref=run_ref,
        observed="silent" if verdict == VERDICT_FN else "fired",
        adjudicated="should_fire" if verdict == VERDICT_FN else "should_be_silent",
        verdict=verdict,
        severity_match=None,
        evidence_span=span,
        meta_judge_rationale="r",
        meta_judge_model="m",
        adjudicator_self_agreement=None,
        operator_confirmed=None,
        fidelity=FIDELITY_VERBATIM,
        prompt_version=1,
        k_adj=1,
    )


# ---------------------------------------------------------------------------
# proposed_op signature validation
# ---------------------------------------------------------------------------


def test_validate_proposed_op_accepts_real_args() -> None:
    assert validate_proposed_op("set_gate", {"promote_margin": 0.2}) == {
        "op": "set_gate",
        "args": {"promote_margin": 0.2},
    }
    assert validate_proposed_op("set_weights", {"per_judge_weights": {"j": 0.0}}) == {
        "op": "set_weights",
        "args": {"per_judge_weights": {"j": 0.0}},
    }


def test_validate_proposed_op_rejects_unknown_arg_and_op() -> None:
    with pytest.raises(ValueError, match="unknown arg"):
        validate_proposed_op("set_gate", {"not_a_real_arg": 1})
    with pytest.raises(ValueError, match="unknown builder op"):
        validate_proposed_op("no_such_op", {"x": 1})


def test_proposed_ops_actually_apply_to_a_live_draft() -> None:
    """The emitted payloads are not just signature-valid — they run."""
    margin_op = validate_proposed_op("set_gate", {"promote_margin": 0.25})
    weights_op = validate_proposed_op("set_weights", {"per_judge_weights": {"j": 0.0}})

    draft = TournamentDraft()
    getattr(ops, margin_op["op"])(draft, **margin_op["args"])
    assert draft.scoring.promote_margin == 0.25
    getattr(ops, weights_op["op"])(draft, **weights_op["args"])
    assert draft.scoring.per_judge_weights["j"] == 0.0


# ---------------------------------------------------------------------------
# Concrete emitters
# ---------------------------------------------------------------------------


def test_margin_finding_emits_set_gate_when_below_floor() -> None:
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=0.01,
        noise_floor_max_abs_delta=0.10,
    )
    margin = [f for f in findings if f.pillar == "calibration"]
    assert len(margin) == 1
    assert margin[0].proposed_op == {
        "op": "set_gate",
        "args": {"promote_margin": round(MARGIN_FLOOR_MULTIPLE * 0.10, 6)},
    }
    assert margin[0].severity == "critical"


def test_no_margin_finding_when_margin_clears_floor() -> None:
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=0.5,
        noise_floor_max_abs_delta=0.10,
    )
    assert not [f for f in findings if "margin" in f.title.lower()]


def test_margin_finding_recommendation_scales_delta_std_when_present() -> None:
    # issue #112 / ch.04 §9.4: the RECOMMENDED value scales the draw-count-
    # stable delta_std, not the range, when delta_std is available — even
    # though the range still decides whether the finding fires at all.
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=0.01,
        noise_floor_max_abs_delta=0.10,
        noise_floor_delta_std=0.03,
    )
    margin = [f for f in findings if f.pillar == "calibration"]
    assert len(margin) == 1
    assert margin[0].proposed_op == {
        "op": "set_gate",
        "args": {"promote_margin": round(MARGIN_FLOOR_MULTIPLE * 0.03, 6)},
    }
    assert "delta_std=0.03" in margin[0].detail
    assert "draw-count-stable" in margin[0].detail


def test_margin_finding_never_proposes_an_op_that_lowers_the_margin() -> None:
    """A K-inflated range must not ship an appliable gate WEAKENING.

    ``max_abs_delta`` is a range and grows with the calibration draw count;
    ``delta_std`` does not. For a well-calibrated floor the two disagree: the
    margin can sit below the range (firing this CRITICAL finding) while
    already clearing 2.5x the dispersion the gate actually thresholds. Here
    ``2.5 x 0.01 = 0.025`` is BELOW the live ``promote_margin=0.05``, so
    emitting it verbatim would give the operator a one-command
    ``zicato reflect apply`` that HALVES the promote margin under a headline
    promising to raise it. The finding must report the disagreement and carry
    no op at all.
    """
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=0.05,
        noise_floor_max_abs_delta=0.10,
        noise_floor_delta_std=0.01,
    )
    margin = [f for f in findings if f.pillar == "calibration"]
    assert len(margin) == 1
    assert margin[0].proposed_op is None
    assert "WEAKEN" in margin[0].detail
    assert "0.025" in margin[0].detail
    # The recommendation text must not tell the operator to raise the margin
    # to a number below where it already sits.
    assert "raise promote_margin to 0.025" not in margin[0].recommendation


def test_margin_finding_falls_back_to_range_when_delta_std_absent() -> None:
    # A pre-#112 record with no delta_std keeps the old range-scaled
    # recommendation (and says so in the finding text).
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=0.01,
        noise_floor_max_abs_delta=0.10,
        noise_floor_delta_std=None,
    )
    margin = [f for f in findings if f.pillar == "calibration"]
    assert len(margin) == 1
    assert margin[0].proposed_op == {
        "op": "set_gate",
        "args": {"promote_margin": round(MARGIN_FLOOR_MULTIPLE * 0.10, 6)},
    }
    assert "max_abs_delta=0.1" in margin[0].detail
    assert "range" in margin[0].detail


def test_no_margin_finding_when_floor_is_zero() -> None:
    # NIT: a zero (unmeasured-as-0) floor yields no set_gate margin finding —
    # the 2.5x recommendation would be a useless 0.0, and "below a zero floor"
    # is an absent measurement, not evidence of promoting on noise.
    findings = derive_findings(
        scorecards=[],
        adjudications=[],
        promote_margin=-1.0,  # below zero, yet the floor is 0 ⇒ still no finding
        noise_floor_max_abs_delta=0.0,
    )
    assert not [f for f in findings if f.pillar == "calibration"]


def test_redundant_judge_emits_set_weights_zero() -> None:
    card = _card("dup", tp=1, redundant_with=({"judge": "orig", "corr": 1.0},))
    findings = derive_findings(scorecards=[card], adjudications=[])
    prune = [f for f in findings if "redundant" in f.title.lower()]
    assert len(prune) == 1
    assert prune[0].proposed_op == {
        "op": "set_weights",
        "args": {"per_judge_weights": {"dup": 0.0}},
    }


def test_fp_heavy_judge_emits_downweight() -> None:
    card = _card("noisy", tp=1, fp=5, precision=1 / 6)
    adjudications = [_adj("noisy", "c:e:r0", VERDICT_FP, "clean span")]
    findings = derive_findings(scorecards=[card], adjudications=adjudications)
    fp_finding = [f for f in findings if "falsely" in f.title.lower()]
    assert len(fp_finding) == 1
    assert fp_finding[0].proposed_op["op"] == "set_weights"
    assert fp_finding[0].proposed_op["args"]["per_judge_weights"]["noisy"] == 0.5
    # Evidence links the FP pile — and carries its adjudicated verdict so the UI
    # colours the chip from the data, not a title regex.
    assert fp_finding[0].evidence[0]["run_ref"] == "c:e:r0"
    assert fp_finding[0].evidence[0]["verdict"] == VERDICT_FP


def test_untested_judge_is_recommendation_only() -> None:
    card = _card("dormant", tn=0, exercised=False)
    findings = derive_findings(scorecards=[card], adjudications=[])
    untested = [f for f in findings if "untested" in f.title.lower()]
    assert len(untested) == 1
    assert untested[0].proposed_op is None


# ---------------------------------------------------------------------------
# The oracle finding — FN names the span
# ---------------------------------------------------------------------------


def test_oracle_fn_finding_names_the_span() -> None:
    card = _card("sleepy", tp=0, fn=1, recall=0.0)
    span = "PLANTED-VIOLATION-uncited-claim-42"
    adjudications = [_adj("sleepy", "v1:entryA:r0", VERDICT_FN, span)]
    findings = derive_findings(scorecards=[card], adjudications=adjudications)
    missed = [f for f in findings if "misses" in f.title.lower()]
    assert len(missed) == 1
    # The finding is evidence-linked to the adjudicated span the judge slept through.
    assert missed[0].evidence[0]["span"] == span
    assert missed[0].evidence[0]["run_ref"] == "v1:entryA:r0"
    assert missed[0].evidence[0]["verdict"] == VERDICT_FN
    assert missed[0].proposed_op is None  # broadening is an authoring decision


# ---------------------------------------------------------------------------
# Ranking + ids
# ---------------------------------------------------------------------------


def test_findings_ranked_by_severity_and_ids_stable() -> None:
    card_fn = _card("a", fn=1, recall=0.0)  # critical
    card_untested = _card("b", exercised=False)  # warning
    first = derive_findings(scorecards=[card_fn, card_untested], adjudications=[])
    # Critical outranks warning.
    assert first[0].severity == "critical"
    # Ids are content-stable across re-derivations (independent of order).
    second = derive_findings(scorecards=[card_untested, card_fn], adjudications=[])
    assert {f.finding_id for f in first} == {f.finding_id for f in second}


def test_finding_json_round_trips() -> None:
    f = Finding(
        finding_id="find-x",
        pillar="validity",
        severity="warning",
        title="t",
        detail="d",
        evidence=({"run_ref": "c:e:r0", "judge_name": "j", "span": "s", "adjudication_path": "p"},),
        recommendation="do the thing",
        proposed_op={"op": "set_weights", "args": {"per_judge_weights": {"j": 0.0}}},
    )
    assert f.to_json()["proposed_op"]["op"] == "set_weights"
    assert f.to_json()["evidence"][0]["span"] == "s"
