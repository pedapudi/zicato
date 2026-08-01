"""Tests for the pure SPRT module (:mod:`zicato.selection.sprt`) and the
:class:`SprtConfig` contract wiring.

Structure
---------
* Section 1 — pure module: params validation, boundary conditions, one-shot
  terminal cases, the burn-in floor, the hard cap, the ``off`` sentinel, and
  the frozen-state discipline (advance never mutates inputs).
* Section 2 — empirical calibration: Monte-Carlo error rates on synthetic
  Gaussian streams, spot-checked against the docstring's expected ranges.
  Deterministic (seeded) so a CI run is reproducible.
* Section 3 — contract wiring: :class:`SprtConfig` defaults, preset validation,
  :meth:`resolve_params`, and the racing cross-field guard in
  :meth:`ScoringWeights.__post_init__`.

All synthetic — no live runs, no I/O, no zicato orchestrator import.
"""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError

import pytest

from zicato.core.scoring_config import ScoringWeights, SprtConfig
from zicato.core.tournament import TournamentStructure
from zicato.selection.sprt import (
    SPRT_PRESETS,
    SprtDecision,
    SprtParams,
    advance,
    initial_state,
    mean_and_ci,
    resolve_preset,
)

# ---------------------------------------------------------------------------
# Section 1 — pure module semantics
# ---------------------------------------------------------------------------


def _params(**overrides: float | int) -> SprtParams:
    """Default params used across the boundary tests (balanced-like)."""
    base = {
        "alpha": 0.05,
        "beta": 0.05,
        "promote_margin": 0.01,
        "min_replicates": 5,
        "max_replicates": 30,
    }
    base.update(overrides)
    return SprtParams(**base)  # type: ignore[arg-type]


class TestSprtParamsValidation:
    def test_alpha_beta_in_open_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            _params(alpha=0.0)
        with pytest.raises(ValueError, match="alpha"):
            _params(alpha=0.5)
        with pytest.raises(ValueError, match="beta"):
            _params(beta=0.0)
        with pytest.raises(ValueError, match="beta"):
            _params(beta=0.5)

    def test_promote_margin_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="promote_margin"):
            _params(promote_margin=0.0)
        with pytest.raises(ValueError, match="promote_margin"):
            _params(promote_margin=-0.01)

    def test_min_replicates_floor(self) -> None:
        with pytest.raises(ValueError, match="min_replicates"):
            _params(min_replicates=1)

    def test_max_below_min_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_replicates"):
            _params(min_replicates=5, max_replicates=3)


class TestPresetResolution:
    def test_off_preset_refuses(self) -> None:
        with pytest.raises(ValueError, match="'off'"):
            resolve_preset("off", promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]

    def test_unknown_preset_rejects(self) -> None:
        with pytest.raises(ValueError, match="unknown SPRT preset"):
            resolve_preset("wild", promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]

    def test_preset_defaults_match_table(self) -> None:
        for name, (alpha, beta, min_r) in SPRT_PRESETS.items():
            p = resolve_preset(name, promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]
            assert p.alpha == alpha
            assert p.beta == beta
            assert p.min_replicates == min_r
            assert p.max_replicates == 30
            assert p.promote_margin == 0.01

    def test_piecewise_override(self) -> None:
        p = resolve_preset("balanced", promote_margin=0.01, max_replicates=30, min_replicates=10)
        assert p.alpha == SPRT_PRESETS["balanced"][0]
        assert p.beta == SPRT_PRESETS["balanced"][1]
        assert p.min_replicates == 10


class TestSprtStateFlow:
    def test_initial_state_is_zero(self) -> None:
        s = initial_state()
        assert s.n == 0
        assert s.sum_x == 0.0
        assert s.sum_x2 == 0.0
        assert s.decision is SprtDecision.CONTINUE

    def test_advance_returns_new_state_leaves_input_unchanged(self) -> None:
        p = _params()
        s0 = initial_state()
        s1 = advance(p, s0, -0.01)
        # New state advanced
        assert s1 is not s0
        assert s1.n == 1
        # Original untouched (frozen dataclass — this must hold semantically
        # too, not just by reference).
        assert s0.n == 0
        assert s0.sum_x == 0.0

    def test_state_and_params_are_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            initial_state().n = 5  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            _params().alpha = 0.1  # type: ignore[misc]

    def test_burn_in_never_decides_early(self) -> None:
        p = _params(min_replicates=5)
        state = initial_state()
        # Feed strongly-H1 evidence — even so, no decision until burn-in
        # is complete.
        for i in range(4):
            state = advance(p, state, -1.0)
            assert state.decision is SprtDecision.CONTINUE, f"decided at n={i+1}"
        state = advance(p, state, -1.0)
        # At n=5 (== min_replicates) the LLR check runs.
        assert state.decision is SprtDecision.H1

    def test_hard_cap_forces_terminal(self) -> None:
        # Force the cap by setting max_replicates == min_replicates: the first
        # post-burn-in check IS the cap-settle, so the observation stream can
        # be arbitrary without racing an LLR crossing.
        p = _params(min_replicates=5, max_replicates=5)
        state = initial_state()
        for _ in range(4):
            state = advance(p, state, 0.0)
            assert state.decision is SprtDecision.CONTINUE
        state = advance(p, state, 0.0)
        assert state.decision is not SprtDecision.CONTINUE
        assert state.n == 5

    def test_cap_settles_by_sample_mean(self) -> None:
        # Sample mean of 0.0 is above the midpoint (−0.005) → H0.
        p = _params(min_replicates=3, max_replicates=3)
        state = initial_state()
        for _ in range(3):
            state = advance(p, state, 0.0)
        assert state.decision is SprtDecision.H0
        assert state.n == 3

        # Sample mean well below the midpoint → H1 on cap.
        state = initial_state()
        for _ in range(3):
            state = advance(p, state, -0.02)
        assert state.decision is SprtDecision.H1
        assert state.n == 3

    def test_advance_on_terminated_state_raises(self) -> None:
        p = _params()
        state = initial_state()
        for _ in range(6):
            state = advance(p, state, -0.5)
            if state.decision is not SprtDecision.CONTINUE:
                break
        assert state.decision is not SprtDecision.CONTINUE
        with pytest.raises(ValueError, match="already terminated"):
            advance(p, state, -0.5)


# ---------------------------------------------------------------------------
# Section 2 — empirical calibration on synthetic Gaussian streams
# ---------------------------------------------------------------------------


def _simulate(
    params: SprtParams, true_mean: float, sigma: float, trials: int
) -> tuple[dict[SprtDecision, int], float]:
    """Run ``trials`` seeded SPRT sequences, tallying decisions and mean n."""
    counts: dict[SprtDecision, int] = {d: 0 for d in SprtDecision if d is not SprtDecision.CONTINUE}
    total_n = 0
    for seed in range(trials):
        rng = random.Random(seed)
        state = initial_state()
        for _ in range(params.max_replicates):
            state = advance(params, state, rng.gauss(true_mean, sigma))
            if state.decision is not SprtDecision.CONTINUE:
                break
        counts[state.decision] += 1
        total_n += state.n
    return counts, total_n / trials


class TestEmpiricalCalibration:
    """Spot-checks the SPRT calibration matches the docstring's claims.

    These are NOT tight bounds — they guard against a large regression in the
    presets (e.g. someone flipping alpha and beta, or removing the variance
    floor). Wide-enough thresholds to keep the tests non-flaky under the
    fixed seeds ``range(trials)``.
    """

    def test_true_h1_is_declared_h1(self) -> None:
        # Clear H1 (true mean == 2×promote_margin): should almost always
        # declare H1 across all presets, and stop well below the cap.
        for preset in ("conservative", "balanced", "aggressive"):
            p = resolve_preset(preset, promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]
            counts, avg_n = _simulate(p, true_mean=-0.02, sigma=0.02, trials=300)
            h1_rate = counts[SprtDecision.H1] / 300
            assert h1_rate >= 0.90, f"preset={preset}: H1 rate {h1_rate:.2f} below 0.90 on clear H1"
            assert avg_n <= 15.0, f"preset={preset}: avg_n {avg_n:.1f} too high"

    def test_blowout_stops_at_burn_in(self) -> None:
        # Blowout (true mean == 10×promote_margin): avg n should be right at
        # min_replicates, since LLR crosses the boundary on the first check.
        for preset in ("conservative", "balanced", "aggressive"):
            p = resolve_preset(preset, promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]
            counts, avg_n = _simulate(p, true_mean=-0.10, sigma=0.02, trials=300)
            assert (
                counts[SprtDecision.H1] == 300
            ), f"preset={preset}: {counts[SprtDecision.H0]} false H0 on blowout"
            assert avg_n <= p.min_replicates + 1.0

    def test_lopsided_loss_stops_at_burn_in(self) -> None:
        # Symmetric: challenger obviously worse → declare H0 immediately.
        for preset in ("conservative", "balanced", "aggressive"):
            p = resolve_preset(preset, promote_margin=0.01, max_replicates=30)  # type: ignore[arg-type]
            counts, avg_n = _simulate(p, true_mean=0.10, sigma=0.02, trials=300)
            assert (
                counts[SprtDecision.H0] == 300
            ), f"preset={preset}: {counts[SprtDecision.H1]} false H1 on lopsided loss"
            assert avg_n <= p.min_replicates + 1.0


class TestMeanAndCi:
    def test_empty_state(self) -> None:
        params = _params()
        mean, hw = mean_and_ci(initial_state(), params)
        assert mean == 0.0
        assert hw == float("inf")

    def test_single_observation(self) -> None:
        params = _params()
        state = advance(params, initial_state(), -0.02)
        mean, hw = mean_and_ci(state, params)
        assert mean == pytest.approx(-0.02)
        assert hw == float("inf")

    def test_ci_shrinks_with_more_samples(self) -> None:
        params = _params(promote_margin=0.01, min_replicates=3, max_replicates=100)
        # Constant stream after floor kicks in: half-width driven purely by
        # the variance floor and 1/√n.
        state = initial_state()
        widths = []
        for _ in range(20):
            state = advance(params, state, -0.5)
            # Break after enough samples to see the trend
            if state.n >= 2:
                _, hw = mean_and_ci(state, params)
                widths.append(hw)
            if state.decision is not SprtDecision.CONTINUE:
                break
        assert len(widths) >= 2
        assert widths[-1] < widths[0], "CI half-width did not shrink with more samples"

    def test_ci_floor_applies(self) -> None:
        # Sample variance is exactly zero on a constant stream; the CI
        # half-width must reflect the (promote_margin/2)² floor, not blow up
        # to zero or NaN.
        params = _params(promote_margin=0.04, min_replicates=3, max_replicates=100)
        state = initial_state()
        for _ in range(10):
            state = advance(params, state, -0.5)
            if state.decision is not SprtDecision.CONTINUE:
                break
        _, hw = mean_and_ci(state, params)
        # Floor sigma = promote_margin/2 = 0.02. Half-width = 1.96 * 0.02 / √n.
        # With n=state.n samples, half-width is 1.96*0.02/√n.
        import math as _math

        expected_min = 1.96 * 0.02 / _math.sqrt(state.n)
        assert hw >= expected_min - 1e-9


# ---------------------------------------------------------------------------
# Section 3 — contract wiring
# ---------------------------------------------------------------------------


class TestSprtConfig:
    def test_default_is_disabled(self) -> None:
        cfg = SprtConfig()
        assert cfg.preset == "off"
        assert not cfg.enabled
        assert cfg.alpha is None
        assert cfg.beta is None
        assert cfg.min_replicates is None

    def test_invalid_preset_rejected(self) -> None:
        with pytest.raises(ValueError, match="sprt.preset"):
            SprtConfig(preset="wild")

    def test_range_validation(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            SprtConfig(preset="balanced", alpha=0.0)
        with pytest.raises(ValueError, match="beta"):
            SprtConfig(preset="balanced", beta=0.5)
        with pytest.raises(ValueError, match="min_replicates"):
            SprtConfig(preset="balanced", min_replicates=1)

    def test_resolve_params_on_disabled_raises(self) -> None:
        with pytest.raises(ValueError, match="disabled"):
            SprtConfig().resolve_params(promote_margin=0.01, max_replicates=30)

    def test_resolve_params_uses_preset(self) -> None:
        cfg = SprtConfig(preset="balanced")
        params = cfg.resolve_params(promote_margin=0.01, max_replicates=30)
        assert params.alpha == SPRT_PRESETS["balanced"][0]
        assert params.beta == SPRT_PRESETS["balanced"][1]
        assert params.min_replicates == SPRT_PRESETS["balanced"][2]
        assert params.max_replicates == 30
        assert params.promote_margin == 0.01

    def test_resolve_params_honors_overrides(self) -> None:
        cfg = SprtConfig(preset="balanced", alpha=0.01, min_replicates=15)
        params = cfg.resolve_params(promote_margin=0.02, max_replicates=50)
        assert params.alpha == 0.01
        assert params.beta == SPRT_PRESETS["balanced"][1]  # not overridden
        assert params.min_replicates == 15
        assert params.promote_margin == 0.02
        assert params.max_replicates == 50


class TestRacingIncompatibility:
    def test_racing_plus_sprt_enabled_raises_at_contract_load(self) -> None:
        racing = TournamentStructure(structure="racing", params={})
        with pytest.raises(ValueError, match="racing"):
            ScoringWeights(
                tournament_structure=racing,
                sprt=SprtConfig(preset="balanced"),
            )

    def test_racing_plus_sprt_off_is_fine(self) -> None:
        racing = TournamentStructure(structure="racing", params={})
        # Default SprtConfig is off — no cross-field violation.
        w = ScoringWeights(tournament_structure=racing)
        assert w.tournament_structure.structure == "racing"
        assert not w.sprt.enabled

    def test_gauntlet_plus_sprt_enabled_is_fine(self) -> None:
        # Sanity: the guard is scoped to racing only, not "any non-gauntlet
        # structure". All duel-based structures must accept SPRT.
        for structure in ("gauntlet", "single_elim", "double_elim", "swiss"):
            w = ScoringWeights(
                tournament_structure=TournamentStructure(structure=structure, params={}),
                sprt=SprtConfig(preset="balanced"),
            )
            assert w.sprt.enabled


class TestContractHash:
    """SprtConfig folds into the contract hash but omits at default."""

    def test_default_sprt_does_not_change_scoring_canonical_form(self) -> None:
        from zicato.epoch.contract import scoring_to_canon

        # A ScoringWeights with default SprtConfig must not add an "sprt"
        # key to the canonical form — the omit-at-default guard.
        canon = scoring_to_canon(ScoringWeights())
        assert (
            "sprt" not in canon
        ), f"default SprtConfig should be omitted, got {canon.get('sprt')!r}"

    def test_enabled_sprt_appears_in_scoring_canonical_form(self) -> None:
        from zicato.epoch.contract import scoring_to_canon

        canon = scoring_to_canon(ScoringWeights(sprt=SprtConfig(preset="balanced")))
        assert "sprt" in canon
        assert canon["sprt"]["preset"] == "balanced"  # type: ignore[index]
