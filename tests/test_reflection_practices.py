"""Practice review engine — per-check verdict matrices + policy + composition.

Every check is exercised across its ``sound`` / ``attend`` / ``unsound`` /
``unmeasured`` outcomes over synthetic contracts + histories; the affirmation
policy (sound checks are present in the output), the proposed_op signature
validation, composition honesty (a detector-owned signal flips the practice
verdict — no re-derivation drift), the corpus summary, and the file-first reader
degrade are pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from goldfive import DriftSeverity

from zicato.core.board import Expectation, ExpectationKind, JudgeMode, JudgeSpec
from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER
from zicato.core.scoring_config import OverfittingConfig, ProposerQualityConfig, ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.core.types import BoardEntry
from zicato.reflection import practices as P
from zicato.reflection.corpus import ObservationRun
from zicato.reflection.findings import validate_proposed_op

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _entry(
    eid: str,
    *,
    budget: int = 30,
    kind: str = "expected_text",
    judge_body: str | None = None,
    budget_only: bool = False,
) -> BoardEntry:
    expectation = None
    if not budget_only:
        expectation = Expectation(kind=ExpectationKind(kind), spec="x")
    judges: tuple[JudgeSpec, ...] = ()
    if judge_body is not None:
        judges = (
            JudgeSpec(
                name=f"j_{eid}",
                mode=JudgeMode.INLINE,
                body=judge_body,
                severity=DriftSeverity.WARNING,
            ),
        )
    return BoardEntry(
        id=eid,
        kind="single_turn",
        wall_clock_budget_seconds=budget,
        input="hi",
        expectation=expectation,
        judges=judges,
    )


def _weights(**kw: Any) -> ScoringWeights:
    return ScoringWeights(**kw)


def _floor(scalars: list[float], *, max_abs: float, gen: str = "g0") -> dict[str, Any]:
    return {
        "generation_id": gen,
        "epoch_id": "e",
        "runs": len(scalars),
        "scalars": scalars,
        "max_abs_delta": max_abs,
        "delta_std": 0.1,
        "measured_at": "2026-07-01",
    }


def _exp(
    gen: str,
    *,
    decision: str | None = None,
    placebo: bool = False,
    train: float | None = None,
    holdout: float | None = None,
    gap: float | None = None,
) -> dict[str, Any]:
    outcome: dict[str, Any] | None = None
    if decision is not None or train is not None:
        outcome = {"tournament_decision": decision}
        if train is not None:
            outcome.update(train_loss=train, holdout_loss=holdout, generalization_gap=gap)
    d: dict[str, Any] = {"generation_id": gen, "outcome": outcome}
    if placebo:
        d["hypothesis"] = {"core_idea": PLACEBO_HYPOTHESIS_MARKER + " no-op"}
    return d


def _card(
    name: str, *, disagreement: float = 0.0, ambiguous_pile: bool = False, ambiguous: int = 0
) -> dict[str, Any]:
    return {
        "judge_name": name,
        "disagreement_rate": disagreement,
        "ambiguous_pile": ambiguous_pile,
        "ambiguous": ambiguous,
        "precision": 1.0,
        "recall": 1.0,
    }


# ---------------------------------------------------------------------------
# 1. oracle_mix
# ---------------------------------------------------------------------------


def test_oracle_mix_all_weak_is_unsound() -> None:
    c = P.check_oracle_mix(
        board_entries=[_entry("a", kind="expected_text"), _entry("b", kind="regex")]
    )
    assert c.verdict == P.VERDICT_UNSOUND
    assert c.proposed_op is None  # authoring only


def test_oracle_mix_with_strong_is_sound() -> None:
    c = P.check_oracle_mix(
        board_entries=[_entry("a", kind="expected_text"), _entry("b", kind="rubric")]
    )
    assert c.verdict == P.VERDICT_SOUND


def test_oracle_mix_no_expectations_is_unmeasured() -> None:
    c = P.check_oracle_mix(board_entries=[_entry("a", budget_only=True)])
    assert c.verdict == P.VERDICT_UNMEASURED
    assert c.unmeasured_reason


# ---------------------------------------------------------------------------
# 2. judge_criterion_quality
# ---------------------------------------------------------------------------


def test_judge_criterion_thin_is_attend() -> None:
    c = P.check_judge_criterion_quality(
        board_entries=[_entry("a", judge_body="is it ok")], scorecards=None
    )
    assert c.verdict == P.VERDICT_ATTEND


def test_judge_criterion_specific_is_sound() -> None:
    body = "flags the response when it fabricates a citation without a supporting source document"
    c = P.check_judge_criterion_quality(
        board_entries=[_entry("a", judge_body=body)], scorecards=None
    )
    assert c.verdict == P.VERDICT_SOUND


def test_judge_criterion_thin_with_ambiguous_pile_upgrades_to_unsound() -> None:
    entries = [_entry("a", judge_body="is it ok")]
    cards = [_card("j_a", ambiguous_pile=True, ambiguous=7)]
    c = P.check_judge_criterion_quality(board_entries=entries, scorecards=cards)
    assert c.verdict == P.VERDICT_UNSOUND
    assert c.evidence["ambiguous_piles"] == {"j_a": 7}


def test_judge_criterion_no_inline_judges_is_unmeasured() -> None:
    c = P.check_judge_criterion_quality(board_entries=[_entry("a")], scorecards=None)
    assert c.verdict == P.VERDICT_UNMEASURED


# ---------------------------------------------------------------------------
# 3. statistical_power
# ---------------------------------------------------------------------------


def _power_weights(margin: float, replicates: int = 2) -> ScoringWeights:
    return _weights(
        promote_margin=margin,
        tournament_structure=TournamentStructure(
            structure="gauntlet", params={"replicates": replicates}
        ),
    )


def test_statistical_power_mdd_over_margin_is_unsound_with_replicate_op() -> None:
    entries = [_entry("a"), _entry("b")]
    c = P.check_statistical_power(
        weights=_power_weights(0.05),
        board_entries=entries,
        noise_floor=_floor([1.0, 1.2], max_abs=0.2),
    )
    assert c.verdict == P.VERDICT_UNSOUND
    assert c.proposed_op is not None
    assert c.proposed_op["op"] == "set_param"
    assert c.proposed_op["args"]["key"] == "replicates"
    assert c.proposed_op["args"]["value"] <= P.MAX_REPLICATE_BUMP


def test_statistical_power_within_2x_is_attend() -> None:
    entries = [_entry("a"), _entry("b")]
    c = P.check_statistical_power(
        weights=_power_weights(0.2),
        board_entries=entries,
        noise_floor=_floor([1.0, 1.2], max_abs=0.2),
    )
    assert c.verdict == P.VERDICT_ATTEND


def test_statistical_power_comfortable_is_sound() -> None:
    entries = [_entry("a"), _entry("b")]
    c = P.check_statistical_power(
        weights=_power_weights(1.0),
        board_entries=entries,
        noise_floor=_floor([1.0, 1.2], max_abs=0.2),
    )
    assert c.verdict == P.VERDICT_SOUND


def test_statistical_power_no_floor_is_unmeasured() -> None:
    c = P.check_statistical_power(
        weights=_power_weights(0.1), board_entries=[_entry("a")], noise_floor=None
    )
    assert c.verdict == P.VERDICT_UNMEASURED
    assert "board audit" in (c.unmeasured_reason or "")


# ---------------------------------------------------------------------------
# 4. overfitting_posture
# ---------------------------------------------------------------------------


def _big_board(n: int = 6) -> list[BoardEntry]:
    return [_entry(f"e{i}") for i in range(n)]


def test_overfitting_small_board_is_sound_caveat() -> None:
    c = P.check_overfitting_posture(weights=_weights(), board_entries=[_entry("a")], experiments=[])
    assert c.verdict == P.VERDICT_SOUND
    assert "split floor" in c.headline


def test_overfitting_rotation_off_is_attend() -> None:
    w = _weights(
        overfitting=OverfittingConfig(rotate_holdout=False),
        proposer_quality=ProposerQualityConfig(screen_entries=2),
    )
    c = P.check_overfitting_posture(weights=w, board_entries=_big_board(), experiments=[])
    assert c.verdict == P.VERDICT_ATTEND
    assert c.proposed_op["op"] == "set_holdout"
    assert c.proposed_op["args"]["rotate_holdout"] is True


def test_overfitting_all_good_is_sound() -> None:
    w = _weights(proposer_quality=ProposerQualityConfig(screen_entries=2))
    c = P.check_overfitting_posture(weights=w, board_entries=_big_board(), experiments=[])
    assert c.verdict == P.VERDICT_SOUND


# ---------------------------------------------------------------------------
# 5. loss_monoculture
# ---------------------------------------------------------------------------


def test_loss_monoculture_dominant_namespace_is_attend_with_op() -> None:
    stats = {"term_contributions": {"drift:x": 9.0, "judge:j": 1.0}}
    c = P.check_loss_monoculture(weights=_weights(), corpus_stats=stats)
    assert c.verdict == P.VERDICT_ATTEND
    assert c.proposed_op["op"] == "set_namespace_weights"


def test_loss_monoculture_balanced_is_sound() -> None:
    stats = {"term_contributions": {"drift:x": 5.0, "judge:j": 5.0}}
    c = P.check_loss_monoculture(weights=_weights(), corpus_stats=stats)
    assert c.verdict == P.VERDICT_SOUND


def test_loss_monoculture_no_corpus_is_unmeasured() -> None:
    c = P.check_loss_monoculture(weights=_weights(), corpus_stats=None)
    assert c.verdict == P.VERDICT_UNMEASURED


# ---------------------------------------------------------------------------
# 6. budget_sanity
# ---------------------------------------------------------------------------


def test_budget_outlier_is_attend() -> None:
    entries = [_entry("a", budget=10), _entry("b", budget=10), _entry("c", budget=1000)]
    c = P.check_budget_sanity(board_entries=entries)
    assert c.verdict == P.VERDICT_ATTEND
    assert "c" in c.evidence["outlier_entries"]


def test_budget_balanced_is_sound() -> None:
    c = P.check_budget_sanity(board_entries=[_entry("a", budget=10), _entry("b", budget=12)])
    assert c.verdict == P.VERDICT_SOUND


def test_budget_single_entry_is_unmeasured() -> None:
    c = P.check_budget_sanity(board_entries=[_entry("a")])
    assert c.verdict == P.VERDICT_UNMEASURED


# ---------------------------------------------------------------------------
# 7. calibration_freshness
# ---------------------------------------------------------------------------


def test_calibration_fresh_is_sound_with_ratio() -> None:
    exps = [_exp("g0"), _exp("g1")]
    c = P.check_calibration_freshness(
        weights=_weights(promote_margin=1.0),
        noise_floor=_floor([1.0, 1.2], max_abs=0.2, gen="g1"),
        experiments=exps,
        epoch_cfg=None,
    )
    assert c.verdict == P.VERDICT_SOUND
    assert c.evidence["margin_over_floor"] is not None


def test_calibration_stale_is_attend() -> None:
    exps = [_exp(f"g{i}") for i in range(12)]
    c = P.check_calibration_freshness(
        weights=_weights(),
        noise_floor=_floor([1.0, 1.2], max_abs=0.2, gen="g0"),
        experiments=exps,
        epoch_cfg=None,
    )
    assert c.verdict == P.VERDICT_ATTEND


def test_calibration_no_floor_is_unmeasured() -> None:
    c = P.check_calibration_freshness(
        weights=_weights(), noise_floor=None, experiments=[], epoch_cfg=None
    )
    assert c.verdict == P.VERDICT_UNMEASURED


# ---------------------------------------------------------------------------
# 8. placebo_outcomes
# ---------------------------------------------------------------------------


def test_placebo_rejected_is_sound() -> None:
    exps = [_exp("p0", decision="rejected", placebo=True)]
    c = P.check_placebo_outcomes(
        weights=_weights(overfitting=OverfittingConfig(random_baseline_every_n=5)), experiments=exps
    )
    assert c.verdict == P.VERDICT_SOUND


def test_placebo_promoted_is_unsound() -> None:
    exps = [_exp("p0", decision="promoted", placebo=True)]
    c = P.check_placebo_outcomes(weights=_weights(), experiments=exps)
    assert c.verdict == P.VERDICT_UNSOUND


def test_placebo_cadence_set_never_fired_is_attend() -> None:
    w = _weights(overfitting=OverfittingConfig(random_baseline_every_n=5))
    c = P.check_placebo_outcomes(weights=w, experiments=[_exp("g0", decision="rejected")])
    assert c.verdict == P.VERDICT_ATTEND


def test_placebo_off_is_unmeasured_with_enable_op() -> None:
    c = P.check_placebo_outcomes(weights=_weights(), experiments=[])
    assert c.verdict == P.VERDICT_UNMEASURED
    assert c.proposed_op["op"] == "set_holdout"


# ---------------------------------------------------------------------------
# 9. generalization_trend  (+ composition honesty)
# ---------------------------------------------------------------------------


def test_generalization_widening_is_unsound() -> None:
    exps = [
        _exp("g0", train=0.5, holdout=0.5, gap=0.0),
        _exp("g1", train=0.3, holdout=0.5, gap=0.2),
    ]
    c = P.check_generalization_trend(weights=_weights(), experiments=exps)
    assert c.verdict == P.VERDICT_UNSOUND


def test_generalization_flat_is_sound_affirmation() -> None:
    exps = [
        _exp("g0", train=0.5, holdout=0.7, gap=0.2),
        _exp("g1", train=0.5, holdout=0.6, gap=0.1),
    ]
    c = P.check_generalization_trend(weights=_weights(), experiments=exps)
    assert c.verdict == P.VERDICT_SOUND
    assert "not being memorized" in c.headline


def test_generalization_insufficient_is_unmeasured() -> None:
    c = P.check_generalization_trend(weights=_weights(), experiments=[_exp("g0")])
    assert c.verdict == P.VERDICT_UNMEASURED


def test_generalization_verdict_follows_the_detector() -> None:
    """Composition honesty: flip the detector-owned signal ⇒ the practice verdict follows."""
    widening = [
        _exp("g0", train=0.5, holdout=0.5, gap=0.0),
        _exp("g1", train=0.3, holdout=0.5, gap=0.2),
    ]
    shrinking = [
        _exp("g0", train=0.5, holdout=0.7, gap=0.2),
        _exp("g1", train=0.5, holdout=0.6, gap=0.1),
    ]
    assert (
        P.check_generalization_trend(weights=_weights(), experiments=widening).verdict
        == P.VERDICT_UNSOUND
    )
    assert (
        P.check_generalization_trend(weights=_weights(), experiments=shrinking).verdict
        == P.VERDICT_SOUND
    )


# ---------------------------------------------------------------------------
# 10. promotion_hygiene
# ---------------------------------------------------------------------------


def test_promotion_no_promotions_is_unmeasured() -> None:
    c = P.check_promotion_hygiene(
        weights=_weights(), experiments=[], board_entries=[_entry("a")], noise_floor=None
    )
    assert c.verdict == P.VERDICT_UNMEASURED


def test_promotion_below_floor_no_gate_is_unsound() -> None:
    exps = [_exp("g1", decision="promoted")]
    c = P.check_promotion_hygiene(
        weights=_weights(promote_margin=0.01),
        experiments=exps,
        board_entries=_big_board(),
        noise_floor=_floor([1.0, 1.5], max_abs=0.5),
    )
    assert c.verdict == P.VERDICT_UNSOUND
    assert c.proposed_op["op"] == "set_gate"


def test_promotion_with_evidence_gate_is_sound() -> None:
    w = _weights(
        promote_margin=0.01,
        tournament_structure=TournamentStructure(
            structure="racing", params={"promote_confidence_threshold": 0.8}
        ),
    )
    exps = [_exp("g1", decision="promoted")]
    c = P.check_promotion_hygiene(
        weights=w,
        experiments=exps,
        board_entries=_big_board(),
        noise_floor=_floor([1.0, 1.5], max_abs=0.5),
    )
    assert c.verdict == P.VERDICT_SOUND


def test_promotion_margin_only_no_floor_is_unmeasured() -> None:
    exps = [_exp("g1", decision="promoted")]
    c = P.check_promotion_hygiene(
        weights=_weights(), experiments=exps, board_entries=[_entry("a")], noise_floor=None
    )
    assert c.verdict == P.VERDICT_UNMEASURED


# ---------------------------------------------------------------------------
# 11. weight_revisit
# ---------------------------------------------------------------------------


def test_weight_revisit_no_scorecards_is_unmeasured() -> None:
    c = P.check_weight_revisit(weights=_weights(), board_entries=[], scorecards=None)
    assert c.verdict == P.VERDICT_UNMEASURED


def test_weight_revisit_divergent_default_judge_is_attend() -> None:
    cards = [_card("noisy", disagreement=0.4), _card("steady", disagreement=0.05)]
    c = P.check_weight_revisit(weights=_weights(), board_entries=[], scorecards=cards)
    assert c.verdict == P.VERDICT_ATTEND
    assert c.proposed_op["op"] == "set_weights"
    assert "noisy" in c.proposed_op["args"]["per_judge_weights"]


def test_weight_revisit_close_rates_is_sound() -> None:
    cards = [_card("a", disagreement=0.05), _card("b", disagreement=0.06)]
    c = P.check_weight_revisit(weights=_weights(), board_entries=[], scorecards=cards)
    assert c.verdict == P.VERDICT_SOUND


# ---------------------------------------------------------------------------
# policy + validation + summary
# ---------------------------------------------------------------------------


def test_review_runs_all_eleven_checks() -> None:
    rev = P.review_practices(
        board_entries=[_entry("a")],
        board_meta=None,
        weights=_weights(),
        epoch_cfg=None,
        experiments=[],
    )
    assert len(rev.checks) == 11
    assert {c.check_id for c in rev.checks} == {
        P.CHECK_ORACLE_MIX,
        P.CHECK_JUDGE_CRITERION_QUALITY,
        P.CHECK_STATISTICAL_POWER,
        P.CHECK_OVERFITTING_POSTURE,
        P.CHECK_LOSS_MONOCULTURE,
        P.CHECK_BUDGET_SANITY,
        P.CHECK_CALIBRATION_FRESHNESS,
        P.CHECK_PLACEBO_OUTCOMES,
        P.CHECK_GENERALIZATION_TREND,
        P.CHECK_PROMOTION_HYGIENE,
        P.CHECK_WEIGHT_REVISIT,
    }


def test_affirmation_policy_sound_checks_present_and_first() -> None:
    """Sound verdicts are reported (not suppressed) and lead the report ordering."""
    w = _weights(proposer_quality=ProposerQualityConfig(screen_entries=2))
    rev = P.review_practices(
        board_entries=_big_board(), board_meta=None, weights=w, epoch_cfg=None, experiments=[]
    )
    assert rev.by_verdict(P.VERDICT_SOUND)  # affirmations survive
    ranked = P.rank_checks_for_report(rev)
    assert ranked[0].verdict == P.VERDICT_SOUND


def test_every_proposed_op_validates_against_its_builder_signature() -> None:
    """No practice ever ships a payload the builder would reject."""
    scenarios = [
        P.review_practices(
            board_entries=[_entry("a", kind="expected_text")],
            board_meta=None,
            weights=_power_weights(0.05),
            epoch_cfg=None,
            experiments=[_exp("g1", decision="promoted")],
            scorecards=[_card("j_a", ambiguous_pile=True, ambiguous=5)],
            corpus_stats={"term_contributions": {"drift:x": 9.0, "judge:j": 1.0}},
            noise_floor=_floor([1.0, 1.5], max_abs=0.5),
        ),
        P.review_practices(
            board_entries=_big_board(),
            board_meta=None,
            weights=_weights(overfitting=OverfittingConfig(rotate_holdout=False)),
            epoch_cfg=None,
            experiments=[],
        ),
    ]
    n_ops = 0
    for rev in scenarios:
        for c in rev.checks:
            if c.proposed_op is not None:
                validate_proposed_op(c.proposed_op["op"], c.proposed_op["args"])
                n_ops += 1
    assert n_ops >= 3  # several checks emitted an actionable op


def test_summarize_corpus_sums_absolute_contributions() -> None:
    obs = [
        ObservationRun(
            reflection_id="r",
            candidate_id="g0",
            entry_id="e",
            replicate=0,
            scalar=0.0,
            drift_loss=0.0,
            pass_fail=True,
            runtime_ms=1,
            aborted=False,
            abort_cause=None,
            fidelity="result",
            has_result=True,
            has_judge_io=False,
            loss_ref=None,
            transcript_ref=None,
            loss_decomposition={"drift:x": -2.0, "judge:j": 1.0},
        ),
        ObservationRun(
            reflection_id="r",
            candidate_id="g0",
            entry_id="e",
            replicate=1,
            scalar=0.0,
            drift_loss=0.0,
            pass_fail=True,
            runtime_ms=1,
            aborted=False,
            abort_cause=None,
            fidelity="result",
            has_result=True,
            has_judge_io=False,
            loss_ref=None,
            transcript_ref=None,
            loss_decomposition={"drift:x": 1.0},
        ),
    ]
    stats = P.summarize_corpus(obs)
    assert stats["n_observations"] == 2
    assert stats["term_contributions"] == {"drift:x": 3.0, "judge:j": 1.0}


# ---------------------------------------------------------------------------
# reader degrade (build_practice_review, file-first DQ3)
# ---------------------------------------------------------------------------


def test_build_practice_review_degrades_on_unknown_reflection(tmp_path: Path) -> None:
    from zicato.query.paths import WorkspacePaths
    from zicato.query.reflection_view import build_practice_review

    paths = WorkspacePaths(tmp_path / ".zicato")
    out = build_practice_review(paths, "refl-nope")
    assert out["found"] is False
    assert out["checks"] == []
    assert set(out["verdict_counts"]) == {"sound", "attend", "unsound", "unmeasured"}


def test_practice_review_json_roundtrip_shape() -> None:
    rev = P.review_practices(
        board_entries=[_entry("a")],
        board_meta=None,
        weights=_weights(),
        epoch_cfg=None,
        experiments=[],
    )
    payload = rev.to_json()
    assert set(payload) == {"checks", "verdict_counts"}
    dumped = json.loads(json.dumps(payload))  # JSON-serializable
    assert len(dumped["checks"]) == 11
    for c in dumped["checks"]:
        assert set(c) == {
            "check_id",
            "verdict",
            "headline",
            "evidence",
            "rationale",
            "proposed_op",
            "unmeasured_reason",
        }
