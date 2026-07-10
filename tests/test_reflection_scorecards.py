"""Scorecards — confusion arithmetic, honest self-consistency, cross-judge graph.

The doc's judge-audit schema, folded from the adjudications. Pins the exact
matrix arithmetic (``tp+fp+fn+tn+ambiguous == n_decisions``, ambiguous excluded
from the rates), severity tracked apart from detection, BOTH the pairwise
disagreement rate AND Fleiss κ (honestly named, never one for the other), the
redundant/conflict cross-judge graph, the exercised flag, and per-fidelity
grouping that never silently mixes tiers.
"""

from __future__ import annotations

from zicato.reflection.adjudicator import (
    VERDICT_AMBIGUOUS,
    VERDICT_FN,
    VERDICT_FP,
    VERDICT_TN,
    VERDICT_TP,
    JudgeAdjudication,
)
from zicato.reflection.corpus import FIDELITY_PREVIEW, FIDELITY_VERBATIM, ObservationRun
from zicato.reflection.scorecards import (
    build_scorecards,
    build_scorecards_by_fidelity,
    fleiss_kappa,
)


def _adj(
    judge: str,
    run_ref: str,
    verdict: str,
    *,
    severity_match: bool | None = None,
    fidelity: str = FIDELITY_VERBATIM,
) -> JudgeAdjudication:
    observed = "fired" if verdict in (VERDICT_TP, VERDICT_FP) else "silent"
    if verdict == VERDICT_AMBIGUOUS:
        adjudicated = "ambiguous"
    elif verdict in (VERDICT_TP, VERDICT_FN):
        adjudicated = "should_fire"
    else:
        adjudicated = "should_be_silent"
    return JudgeAdjudication(
        judge_name=judge,
        run_ref=run_ref,
        observed=observed,
        adjudicated=adjudicated,
        verdict=verdict,
        severity_match=severity_match,
        evidence_span="span",
        meta_judge_rationale="r",
        meta_judge_model="m",
        adjudicator_self_agreement=None,
        operator_confirmed=None,
        fidelity=fidelity,
        prompt_version=1,
        k_adj=1,
    )


def _obs(candidate: str, entry: str, replicate: int, decisions: list[dict]) -> ObservationRun:
    return ObservationRun(
        reflection_id="r",
        candidate_id=candidate,
        entry_id=entry,
        replicate=replicate,
        scalar=0.0,
        drift_loss=0.0,
        pass_fail=True,
        runtime_ms=1,
        aborted=False,
        abort_cause=None,
        fidelity=FIDELITY_VERBATIM,
        has_result=False,
        has_judge_io=True,
        loss_ref=None,
        transcript_ref=None,
        judge_decisions=tuple(decisions),
    )


# ---------------------------------------------------------------------------
# Matrix arithmetic + ambiguous excluded from the rates
# ---------------------------------------------------------------------------


def test_confusion_arithmetic_sums_and_excludes_ambiguous() -> None:
    adjudications = [
        _adj("j", "c:e:r0", VERDICT_TP),
        _adj("j", "c:e:r1", VERDICT_TP),
        _adj("j", "c:e:r2", VERDICT_FP),
        _adj("j", "c:e:r3", VERDICT_FN),
        _adj("j", "c:e:r4", VERDICT_TN),
        _adj("j", "c:e:r5", VERDICT_AMBIGUOUS),
        _adj("j", "c:e:r6", VERDICT_AMBIGUOUS),
    ]
    card = build_scorecards(adjudications=adjudications, corpus=[])[0]
    assert card.tp + card.fp + card.fn + card.tn + card.ambiguous == card.n_decisions == 7
    assert (card.tp, card.fp, card.fn, card.tn, card.ambiguous) == (2, 1, 1, 1, 2)
    # Rates use ONLY the four decided cells — ambiguous never enters a denominator.
    assert card.precision == 2 / 3  # tp/(tp+fp)
    assert card.recall == 2 / 3  # tp/(tp+fn)
    assert card.fpr == 1 / 2  # fp/(fp+tn)
    assert card.f1 == (2 * 2) / (2 * 2 + 1 + 1)


def test_rates_none_when_denominator_zero() -> None:
    # A judge with only true negatives: precision/recall/f1 undefined.
    card = build_scorecards(adjudications=[_adj("j", "c:e:r0", VERDICT_TN)], corpus=[])[0]
    assert card.precision is None
    assert card.recall is None
    assert card.f1 is None
    assert card.fpr == 0.0  # fp/(fp+tn) = 0/1


# ---------------------------------------------------------------------------
# Severity tracked apart from detection
# ---------------------------------------------------------------------------


def test_severity_accuracy_separate_from_detection() -> None:
    adjudications = [
        _adj("j", "c:e:r0", VERDICT_TP, severity_match=True),
        _adj("j", "c:e:r1", VERDICT_TP, severity_match=False),
        _adj("j", "c:e:r2", VERDICT_FN),  # no severity to match
    ]
    card = build_scorecards(adjudications=adjudications, corpus=[])[0]
    # Detection: recall = 2/(2+1).
    assert card.recall == 2 / 3
    # Severity: only the two TP-with-severity decisions count → 1 of 2 correct.
    assert card.severity_accuracy == 0.5


# ---------------------------------------------------------------------------
# Self-consistency — BOTH metrics, honestly named
# ---------------------------------------------------------------------------


def test_disagreement_and_kappa_both_computed_and_named() -> None:
    # Judge 'j' re-judges three units, three replicates each, with mixed firing.
    corpus: list[ObservationRun] = []
    firing = {
        ("cA", "e"): [True, True, False],  # a flip-flop unit ⇒ disagreement > 0
        ("cB", "e"): [True, True, True],
        ("cC", "e"): [False, False, False],
    }
    for (cand, entry), flags in firing.items():
        for i, fired in enumerate(flags):
            corpus.append(_obs(cand, entry, i, [{"judge_name": "j", "fired": fired}]))
    adjudications = [_adj("j", "cA:e:r0", VERDICT_TP)]

    card = build_scorecards(adjudications=adjudications, corpus=corpus)[0]
    assert card.disagreement_rate > 0.0  # the flip-flop unit
    assert card.self_consistency_kappa is not None  # uniform 3-rater items ⇒ κ defined
    # They are DISTINCT statistics under distinct names.
    assert card.disagreement_rate != card.self_consistency_kappa


def test_fleiss_kappa_known_answers() -> None:
    # Perfectly consistent items, evenly split across categories ⇒ κ = 1.0.
    assert fleiss_kappa([(3, 3), (3, 3), (0, 3), (0, 3)]) == 1.0
    # Fewer than two uniform-rater items ⇒ undefined.
    assert fleiss_kappa([(1, 2)]) is None
    # All items in ONE category ⇒ 1 − P_e == 0 ⇒ undefined (not spuriously 1.0).
    assert fleiss_kappa([(3, 3), (3, 3)]) is None
    # No item has >= 2 raters ⇒ undefined.
    assert fleiss_kappa([(1, 1), (0, 1)]) is None


# ---------------------------------------------------------------------------
# Cross-judge redundancy / conflict + exercised
# ---------------------------------------------------------------------------


def test_cross_judge_redundant_and_conflict() -> None:
    # a and b fire identically (redundant); a and c fire oppositely (conflict).
    a_pat = [True, False, True, False]
    b_pat = [True, False, True, False]  # identical to a
    c_pat = [False, True, False, True]  # opposite of a
    corpus: list[ObservationRun] = []
    for i in range(4):
        corpus.append(
            _obs(
                "cand",
                "e",
                i,
                [
                    {"judge_name": "a", "fired": a_pat[i]},
                    {"judge_name": "b", "fired": b_pat[i]},
                    {"judge_name": "c", "fired": c_pat[i]},
                ],
            )
        )
    adjudications = [
        _adj("a", "cand:e:r0", VERDICT_TP),
        _adj("b", "cand:e:r0", VERDICT_TP),
        _adj("c", "cand:e:r1", VERDICT_FP),
    ]
    cards = {c.judge_name: c for c in build_scorecards(adjudications=adjudications, corpus=corpus)}
    assert [r["judge"] for r in cards["a"].redundant_with] == ["b"]
    assert [c["judge"] for c in cards["a"].conflicts_with] == ["c"]
    assert cards["a"].exercised is True


def test_zero_variance_judges_not_flagged_redundant() -> None:
    # REDUNDANCY fix: two ALWAYS-fire judges have zero-variance firing vectors —
    # uncorrelatable, so NEITHER is redundant_with the other (the old x==y ⇒ 1.0
    # Pearson convention would have clustered them).
    corpus: list[ObservationRun] = []
    for i in range(4):
        corpus.append(
            _obs(
                "cand",
                "e",
                i,
                [
                    {"judge_name": "always_a", "fired": True},
                    {"judge_name": "always_b", "fired": True},
                ],
            )
        )
    adjudications = [
        _adj("always_a", "cand:e:r0", VERDICT_TP),
        _adj("always_b", "cand:e:r0", VERDICT_TP),
    ]
    cards = {c.judge_name: c for c in build_scorecards(adjudications=adjudications, corpus=corpus)}
    assert cards["always_a"].redundant_with == ()
    assert cards["always_a"].conflicts_with == ()
    assert cards["always_b"].redundant_with == ()


def test_mixed_variance_still_correlates_around_a_constant_judge() -> None:
    # The zero-variance SKIP is surgical: two VARYING judges still correlate; only
    # the constant judge is excluded from every pairing.
    a_pat = [True, False, True, False]
    b_pat = [True, False, True, False]  # varies, identical to a ⇒ redundant
    const = [True, True, True, True]  # zero variance ⇒ uncorrelatable
    corpus: list[ObservationRun] = []
    for i in range(4):
        corpus.append(
            _obs(
                "cand",
                "e",
                i,
                [
                    {"judge_name": "a", "fired": a_pat[i]},
                    {"judge_name": "b", "fired": b_pat[i]},
                    {"judge_name": "const", "fired": const[i]},
                ],
            )
        )
    adjudications = [
        _adj("a", "cand:e:r0", VERDICT_TP),
        _adj("b", "cand:e:r0", VERDICT_TP),
        _adj("const", "cand:e:r0", VERDICT_TP),
    ]
    cards = {c.judge_name: c for c in build_scorecards(adjudications=adjudications, corpus=corpus)}
    assert [r["judge"] for r in cards["a"].redundant_with] == ["b"]  # varying pair still correlates
    assert cards["const"].redundant_with == ()  # the constant judge is skipped everywhere
    assert all(r["judge"] != "const" for r in cards["a"].redundant_with)


def test_unexercised_judge_flagged() -> None:
    corpus = [_obs("cand", "e", 0, [{"judge_name": "j", "fired": False}])]
    adjudications = [_adj("j", "cand:e:r0", VERDICT_TN)]
    card = build_scorecards(adjudications=adjudications, corpus=corpus)[0]
    assert card.exercised is False
    assert "untested" in card.recommendation


# ---------------------------------------------------------------------------
# Per-fidelity grouping never silently mixes tiers
# ---------------------------------------------------------------------------


def test_per_fidelity_grouping_never_mixes() -> None:
    adjudications = [
        _adj("j", "c:e:r0", VERDICT_TP, fidelity=FIDELITY_VERBATIM),
        _adj("j", "c:e:r1", VERDICT_FP, fidelity=FIDELITY_PREVIEW),
    ]
    by_tier = build_scorecards_by_fidelity(adjudications=adjudications, corpus=[])
    assert set(by_tier) == {FIDELITY_VERBATIM, FIDELITY_PREVIEW}
    # The verbatim tier sees only its TP; the preview tier only its FP.
    assert by_tier[FIDELITY_VERBATIM][0].tp == 1
    assert by_tier[FIDELITY_VERBATIM][0].fp == 0
    assert by_tier[FIDELITY_PREVIEW][0].tp == 0
    assert by_tier[FIDELITY_PREVIEW][0].fp == 1


def test_ambiguous_pile_flagged() -> None:
    # Half the decisions ambiguous ⇒ pile flagged (criterion underspecified).
    adjudications = [
        _adj("j", "c:e:r0", VERDICT_TP),
        _adj("j", "c:e:r1", VERDICT_AMBIGUOUS),
        _adj("j", "c:e:r2", VERDICT_AMBIGUOUS),
    ]
    corpus = [_obs("c", "e", 0, [{"judge_name": "j", "fired": True}])]
    card = build_scorecards(adjudications=adjudications, corpus=corpus)[0]
    assert card.ambiguous_pile is True
