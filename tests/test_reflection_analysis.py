"""Reflection pillars 1-2 — pure analyzers, known-answer tests.

Every analyzer is a pure function over the corpus + injected records, so each
is pinned to a hand-computed answer: the seeded-bootstrap decision-flip
probability, the greedy Pearson clustering, the closed-form power analysis, the
coverage flags, and the ``detect_noisy_judge`` integration over verbatim judge
firings.
"""

from __future__ import annotations

import math

from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER
from zicato.reflection.analysis import (
    coverage,
    decision_flip_probability,
    entry_candidate_matrix,
    entry_differentiation,
    judge_self_consistency,
    noise_floor_summary,
    pearson,
    placebo_outcomes,
    power_analysis,
    redundancy_clusters,
    sigma_from_noise_floor,
)
from zicato.reflection.corpus import FIDELITY_PREVIEW, FIDELITY_VERBATIM, ObservationRun


def _obs(
    candidate: str,
    entry: str,
    replicate: int,
    scalar: float,
    *,
    judge_decisions: tuple[dict, ...] = (),
    drift_events: tuple[dict, ...] = (),
    fidelity: str = FIDELITY_PREVIEW,
) -> ObservationRun:
    return ObservationRun(
        reflection_id="refl-x",
        candidate_id=candidate,
        entry_id=entry,
        replicate=replicate,
        scalar=scalar,
        drift_loss=0.0,
        pass_fail=None,
        runtime_ms=1,
        aborted=False,
        abort_cause=None,
        fidelity=fidelity,
        has_result=False,
        has_judge_io=bool(judge_decisions),
        loss_ref=None,
        transcript_ref=None,
        drift_events=drift_events,
        judge_decisions=judge_decisions,
    )


# ---------------------------------------------------------------------------
# Pillar 1 — noise floor summary (consumed, never re-measured)
# ---------------------------------------------------------------------------


def test_noise_floor_summary_consumes_persisted_floor() -> None:
    corpus = [_obs("v0", "a", 5000, 1.0), _obs("v0", "a", 5001, 3.0)]
    out = noise_floor_summary(
        corpus=corpus,
        epoch_noise_floor={"max_abs_delta": 0.5, "runs": 5},
        epoch_preflight={"verdict": "ok"},
    )
    assert out["consumed"] is True
    assert out["fresh"] is False
    assert out["noise_floor_max_abs_delta"] == 0.5
    assert out["noise_floor_runs"] == 5
    assert out["preflight_verdict"] == "ok"
    # Per-candidate scalar SD over the replicate-level draws (1.0, 3.0).
    assert out["per_candidate_scalar_sd"]["v0"]["scalar_sd"] == 1.0
    assert out["fidelity_tiers"] == [FIDELITY_PREVIEW]


def test_noise_floor_summary_fresh_flag_recorded_not_measured() -> None:
    out = noise_floor_summary(corpus=[], epoch_noise_floor=None, fresh=True)
    assert out["fresh"] is True
    assert out["consumed"] is False
    assert out["noise_floor_max_abs_delta"] is None


# ---------------------------------------------------------------------------
# Pillar 1 — decision-flip probability (seeded bootstrap known-answers)
# ---------------------------------------------------------------------------


def _flip_corpus() -> list[ObservationRun]:
    """One entry, iid {0,1,2,3} replicate scalars for both candidates."""
    corpus: list[ObservationRun] = []
    for rep, val in enumerate((0.0, 1.0, 2.0, 3.0)):
        corpus.append(_obs("parent", "a", 5000 + rep, val))
        corpus.append(_obs("child", "a", 5000 + rep, val))
    return corpus


def test_decision_flip_near_zero_when_margin_dwarfs_spread() -> None:
    # child clearly above parent, margin huge ⇒ decision never flips.
    corpus: list[ObservationRun] = []
    for rep, val in enumerate((0.0, 1.0, 2.0, 3.0)):
        corpus.append(_obs("parent", "a", 5000 + rep, val))
        corpus.append(_obs("child", "a", 5000 + rep, val + 10.0))
    out = decision_flip_probability(
        corpus=corpus,
        reflection_id="refl-x",
        parent_id="parent",
        child_id="child",
        promote_margin=100.0,
        b=1000,
    )
    assert out["p_flip"] == 0.0
    assert out["base_decision"] == "promote"


def test_decision_flip_material_case_band_and_monotonic() -> None:
    # S1: k-with-replacement mean resample. margin below the spread ⇒ a
    # materially positive but bounded p_flip; growing the margin shrinks it.
    def _p(margin: float) -> float:
        out = decision_flip_probability(
            corpus=_flip_corpus(),
            reflection_id="refl-x",
            parent_id="parent",
            child_id="child",
            promote_margin=margin,
            b=4000,
        )
        return out["p_flip"]

    p_small = _p(0.5)
    p_large = _p(1.5)
    assert 0.0 < p_small < 0.5  # material but not a coin flip
    assert p_large < p_small  # monotone: a wider margin flips less often


def test_decision_flip_k_mean_resample_below_single_pick() -> None:
    # S1: the k-mean estimator matches the base mean-of-K, so its resample
    # variance — and thus p_flip — is LOWER than the single-draw estimator's.
    common = dict(
        corpus=_flip_corpus(),
        reflection_id="refl-x",
        parent_id="parent",
        child_id="child",
        promote_margin=0.5,
        b=4000,
    )
    k_mean = decision_flip_probability(resample="k_mean", **common)["p_flip"]  # type: ignore[arg-type]
    single = decision_flip_probability(resample="single", **common)["p_flip"]  # type: ignore[arg-type]
    assert k_mean < single


def test_decision_flip_is_deterministic_under_pair_and_id() -> None:
    # N1: the seed folds (reflection_id, parent, child); same triple ⇒ identical,
    # a different reflection_id ⇒ an independent stream.
    kwargs = dict(
        corpus=_flip_corpus(),
        parent_id="parent",
        child_id="child",
        promote_margin=0.5,
        b=1000,
    )
    a = decision_flip_probability(reflection_id="refl-seed-1", **kwargs)  # type: ignore[arg-type]
    b = decision_flip_probability(reflection_id="refl-seed-1", **kwargs)  # type: ignore[arg-type]
    c = decision_flip_probability(reflection_id="refl-seed-2", **kwargs)  # type: ignore[arg-type]
    assert a["p_flip"] == b["p_flip"]  # same id ⇒ identical
    assert a["p_flip"] != c["p_flip"]  # different id ⇒ different resample


def test_decision_flip_seed_independent_across_pairs() -> None:
    # N1: two different pairs under the SAME reflection_id draw independent
    # streams — folding the pair ids in decouples their seeds.
    corpus = [_obs("parent", "a", 5000 + rep, val) for rep, val in enumerate((0.0, 1.0, 2.0, 3.0))]
    corpus += [_obs("child", "a", 5000 + rep, val) for rep, val in enumerate((0.0, 1.0, 2.0, 3.0))]
    corpus += [_obs("other", "a", 5000 + rep, val) for rep, val in enumerate((0.0, 1.0, 2.0, 3.0))]
    pair_a = decision_flip_probability(
        corpus=corpus,
        reflection_id="r",
        parent_id="parent",
        child_id="child",
        promote_margin=0.5,
        b=1500,
    )
    pair_b = decision_flip_probability(
        corpus=corpus,
        reflection_id="r",
        parent_id="parent",
        child_id="other",
        promote_margin=0.5,
        b=1500,
    )
    # Same underlying scalars, but the seed differs by child id ⇒ distinct draws.
    assert pair_a["p_flip"] != pair_b["p_flip"]


def test_decision_flip_none_when_unit_has_single_replicate() -> None:
    # S2: a contributing unit with <2 replicates ⇒ p_flip=None + reason,
    # never a fabricated 0.0.
    corpus = [_obs("parent", "a", 5000, 1.0), _obs("child", "a", 5000, 2.0)]  # 1 replicate each
    out = decision_flip_probability(
        corpus=corpus,
        reflection_id="refl-x",
        parent_id="parent",
        child_id="child",
        promote_margin=0.5,
    )
    assert out["p_flip"] is None
    assert "fewer than two replicates" in out["reason"]
    assert out["base_decision"] is None


def test_decision_flip_none_when_candidate_has_no_observations() -> None:
    # S2: a candidate with zero observations ⇒ p_flip=None + reason.
    corpus = [_obs("parent", "a", 5000, 1.0), _obs("parent", "a", 5001, 2.0)]  # no child at all
    out = decision_flip_probability(
        corpus=corpus,
        reflection_id="refl-x",
        parent_id="parent",
        child_id="child",
        promote_margin=0.5,
    )
    assert out["p_flip"] is None
    assert "no observations" in out["reason"]


# ---------------------------------------------------------------------------
# Pillar 1 — judge self-consistency feeds detect_noisy_judge unchanged
# ---------------------------------------------------------------------------


def _judge_decision(name: str, fired: bool) -> dict:
    return {"judge_name": name, "fired": fired, "severity": "warning", "claim": "x"}


def test_judge_self_consistency_fires_for_flip_flopping_judge() -> None:
    # Same (candidate, entry) unit re-judged across replicates, verdict flips.
    v = FIDELITY_VERBATIM
    corpus = [
        _obs("v0", "a", 5000, 0.0, judge_decisions=(_judge_decision("flip", True),), fidelity=v),
        _obs("v0", "a", 5001, 0.0, judge_decisions=(_judge_decision("flip", False),), fidelity=v),
        _obs("v0", "a", 5002, 0.0, judge_decisions=(_judge_decision("flip", True),), fidelity=v),
        _obs("v0", "a", 5003, 0.0, judge_decisions=(_judge_decision("flip", False),), fidelity=v),
    ]
    out = judge_self_consistency(corpus=corpus)
    assert out["judges"][0]["judge_name"] == "flip"
    assert out["judges"][0]["disagreement_rate"] > 0.25
    assert [f["code"] for f in out["noisy_judge_findings"]] == ["noisy_judge"]
    assert out["fidelity_tiers"] == [FIDELITY_VERBATIM]


def test_judge_self_consistency_silent_for_stable_judge() -> None:
    corpus = [
        _obs("v0", "a", 5000, 0.0, judge_decisions=(_judge_decision("steady", True),)),
        _obs("v0", "a", 5001, 0.0, judge_decisions=(_judge_decision("steady", True),)),
    ]
    out = judge_self_consistency(corpus=corpus)
    assert out["judges"][0]["disagreement_rate"] == 0.0
    assert out["noisy_judge_findings"] == []


def test_judge_self_consistency_pools_disagreement_across_units() -> None:
    # S3: one flip-flopping unit + many stable units ⇒ the POOLED rate is small
    # (diluted by the stable pairs), while worst_unit stays high. The old
    # worst-unit maximum would have read the whole judge as ~100% noisy.
    corpus: list[ObservationRun] = []
    # Unit (v0, flipflop): [T, F] — 1 disagreeing pair of 1 total ⇒ worst 1.0.
    corpus.append(_obs("v0", "flipflop", 5000, 0.0, judge_decisions=(_judge_decision("j", True),)))
    corpus.append(_obs("v0", "flipflop", 5001, 0.0, judge_decisions=(_judge_decision("j", False),)))
    # Four stable units (v1..v4, all silent): [F, F] each ⇒ 0 disagreeing of 1.
    for i in range(1, 5):
        corpus.append(
            _obs(f"v{i}", "steady", 5000, 0.0, judge_decisions=(_judge_decision("j", False),))
        )
        corpus.append(
            _obs(f"v{i}", "steady", 5001, 0.0, judge_decisions=(_judge_decision("j", False),))
        )
    out = judge_self_consistency(corpus=corpus)
    card = out["judges"][0]
    # Pooled: 1 disagreeing pair / 5 total pairs = 0.2; worst unit = 1.0.
    assert card["disagreement_rate"] == 0.2
    assert card["worst_unit_disagreement"] == 1.0


# ---------------------------------------------------------------------------
# Pillar 1 — placebo outcomes (cited, not reinvented)
# ---------------------------------------------------------------------------


def test_placebo_outcomes_surfaces_promoted_placebo() -> None:
    experiments = [
        {
            "generation_id": "vp",
            "hypothesis": {"core_idea": f"{PLACEBO_HYPOTHESIS_MARKER} no-op baseline"},
            "outcome": {"tournament_decision": "promoted", "scalar_score_delta": 0.0},
        }
    ]
    out = placebo_outcomes(corpus=[], experiments=experiments)
    assert [f["code"] for f in out["placebo_promoted_findings"]] == ["placebo_promoted"]


def test_placebo_outcomes_silent_without_promoted_placebo() -> None:
    out = placebo_outcomes(corpus=[], experiments=[])
    assert out["placebo_promoted_findings"] == []


# ---------------------------------------------------------------------------
# Pillar 2 — entry differentiation + matrix
# ---------------------------------------------------------------------------


def test_entry_differentiation_flags_flat_entry() -> None:
    corpus = [
        _obs("v0", "moves", 0, 1.0),
        _obs("v1", "moves", 0, 5.0),
        _obs("v0", "flat", 0, 2.0),
        _obs("v1", "flat", 0, 2.0),
    ]
    out = entry_differentiation(corpus=corpus)
    by_entry = {row["entry_id"]: row for row in out["entries"]}
    assert by_entry["moves"]["differentiates"] is True
    assert by_entry["moves"]["spread"] == 4.0
    assert by_entry["flat"]["differentiates"] is False
    assert by_entry["flat"]["spread"] == 0.0


def test_entry_candidate_matrix_shape() -> None:
    corpus = [
        _obs("v0", "a", 0, 1.0),
        _obs("v1", "a", 0, 2.0),
        _obs("v0", "b", 0, 3.0),
        _obs("v1", "b", 0, 4.0),
    ]
    out = entry_candidate_matrix(corpus=corpus)
    assert out["entries"] == ["a", "b"]
    assert out["candidates"] == ["v0", "v1"]
    assert out["matrix"] == [[1.0, 2.0], [3.0, 4.0]]


# ---------------------------------------------------------------------------
# Pillar 2 — Pearson + redundancy clustering (hand-rolled, no scipy)
# ---------------------------------------------------------------------------


def test_pearson_known_values() -> None:
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0  # perfectly correlated
    assert pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0  # anti-correlated
    assert pearson([2.0, 2.0, 2.0], [2.0, 2.0, 2.0]) == 1.0  # identical constants
    assert pearson([2.0, 2.0, 2.0], [1.0, 2.0, 3.0]) == 0.0  # one flat ⇒ undefined


def test_redundancy_clusters_group_duplicates_not_orthogonals() -> None:
    corpus: list[ObservationRun] = []
    for cand, base in (("v0", 1.0), ("v1", 2.0), ("v2", 3.0)):
        corpus.append(_obs(cand, "dup1", 0, base))
        corpus.append(_obs(cand, "dup2", 0, base))  # identical vector to dup1
        # orthogonal: a permuted, non-monotone vector
        corpus.append(_obs(cand, "orth", 0, {"v0": 3.0, "v1": 1.0, "v2": 2.0}[cand]))
    out = redundancy_clusters(corpus=corpus, threshold=0.95)
    assert ["dup1", "dup2"] in out["redundant_clusters"]
    # orth is its own singleton — not redundant with the duplicated pair.
    assert ["orth"] in out["clusters"]
    assert all("orth" not in c for c in out["redundant_clusters"])


# ---------------------------------------------------------------------------
# Pillar 2 — closed-form power analysis
# ---------------------------------------------------------------------------


def test_power_analysis_closed_form_spot_checks() -> None:
    # MDE = z * sigma * sqrt(2 / (k * n)); z_.95 = 1.959963985.
    assert math.isclose(
        power_analysis(sigma=1.0, k=2, n=1)["min_detectable_delta"],
        1.959963984540054,
        rel_tol=1e-9,
    )
    assert math.isclose(
        power_analysis(sigma=1.0, k=8, n=1)["min_detectable_delta"],
        1.959963984540054 / 2.0,
        rel_tol=1e-9,
    )
    # Halving via n mirrors halving via k (both enter as k*n).
    assert math.isclose(
        power_analysis(sigma=2.0, k=2, n=4)["min_detectable_delta"],
        power_analysis(sigma=2.0, k=8, n=1)["min_detectable_delta"],
        rel_tol=1e-12,
    )


def test_power_analysis_degenerate_is_infinite() -> None:
    assert power_analysis(sigma=1.0, k=0, n=1)["min_detectable_delta"] == math.inf
    assert power_analysis(sigma=1.0, k=2, n=0)["min_detectable_delta"] == math.inf


def test_sigma_from_noise_floor_is_per_unit_pstdev() -> None:
    # S4: σ = pstdev(scalars), NOT delta_std (√2-scaled) or max_abs_delta (range).
    import statistics as _stats

    scalars = [1.0, 2.0, 3.0, 4.0]
    floor = {
        "scalars": scalars,
        "delta_std": _stats.pstdev(scalars) * math.sqrt(2.0),  # the WRONG input
        "max_abs_delta": 3.0,  # also WRONG (a range)
    }
    sigma = sigma_from_noise_floor(floor)
    assert sigma == _stats.pstdev(scalars)
    # It is emphatically not either mislabeled field.
    assert sigma != floor["delta_std"]
    assert sigma != floor["max_abs_delta"]


def test_sigma_from_noise_floor_none_when_undefined() -> None:
    assert sigma_from_noise_floor(None) is None
    assert sigma_from_noise_floor({}) is None
    assert sigma_from_noise_floor({"scalars": [1.0]}) is None  # <2 scalars ⇒ undefined


# ---------------------------------------------------------------------------
# Pillar 2 — coverage (untested judges + uncovered kinds)
# ---------------------------------------------------------------------------


def test_coverage_flags_never_fired_judge_untested_and_uncovered_kind() -> None:
    corpus = [
        _obs(
            "v0",
            "a",
            0,
            0.0,
            drift_events=({"kind": "off_topic", "severity": "info", "judge_name": "", "count": 1},),
            judge_decisions=(_judge_decision("fires", True),),
        ),
    ]
    out = coverage(
        corpus=corpus,
        board_kinds=["off_topic", "hallucination"],
        board_judges=["fires", "never_fires"],
    )
    assert out["exercised_kinds"] == ["off_topic"]
    assert out["uncovered_kinds"] == ["hallucination"]
    assert out["untested_judges"] == ["never_fires"]
    judges = {j["judge_name"]: j for j in out["judges"]}
    assert judges["fires"]["exercised"] is True
    assert judges["never_fires"]["untested"] is True


def test_coverage_custom_kind_judge_marked_exercised() -> None:
    corpus = [
        _obs(
            "v0",
            "a",
            0,
            0.0,
            drift_events=(
                {
                    "kind": "custom:tone_judge",
                    "severity": "warning",
                    "judge_name": "tone_judge",
                    "count": 2,
                },
            ),
        ),
    ]
    out = coverage(corpus=corpus, board_kinds=[], board_judges=["tone_judge"])
    assert out["untested_judges"] == []
