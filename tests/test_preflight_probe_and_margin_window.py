"""Triage pins for the pre-flight cluster (#106, #112).

Both defects live one level above the measurement: pre-flight measures the
right thing but asks too narrow a question of it.

* **#106** — ``run_contract_preflight`` degrades exactly ``points[0]``. When
  that point is inert under the current contract the measured signal is ~0
  and a healthy board gets a deterministic (never flaky) ``REFUSE``. There
  is no way to choose the point, no fallback, and no way to tell "the probe
  was inert" from "the contract is noise-limited".
* **#112** — pre-flight answers "can the contract out-signal its own noise?"
  but never "is ``promote_margin`` reachable?". The window the loop needs is
  ``noise < margin < achievable``; only the lower bound is ever checked, so
  a guaranteed-null run passes.

Every failing pin is ``xfail(strict=True)``: it must XPASS once fixed, at
which point the marker is removed.
"""

from __future__ import annotations

import inspect

import pytest

from zicato.epoch.preflight import (
    VERDICT_OK,
    VERDICT_REFUSE,
    preflight_verdict,
)

# ---------------------------------------------------------------------------
# Issue #106 — one inert probe point must not veto a contract
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #106: run_contract_preflight hardcodes points[0]; there is no "
        "way to select the probed mutation point and no fallback when it is inert"
    ),
)
def test_preflight_accepts_a_chosen_mutation_point() -> None:
    """The probed point must be selectable (``--degrade-mutation-id``)."""
    from zicato.epoch.preflight import run_contract_preflight

    params = inspect.signature(run_contract_preflight).parameters
    assert "degrade_mutation_id" in params


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #106: only one point is ever probed, so PreflightReport records "
        "a single (id, signal) and cannot express max-over-probes"
    ),
)
def test_preflight_report_records_every_probe() -> None:
    """The report must carry the per-point signals it took the max over.

    Probing several points and reporting only the winner would hide the
    diagnosis #106 asks for; the operator needs to see that point A was
    inert and point B was not.
    """
    from zicato.epoch.preflight import PreflightReport

    fields = set(PreflightReport.__dataclass_fields__)
    assert "probed_points" in fields


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #106: preflight_verdict cannot distinguish an INERT probe "
        "(degraded tree scored identically to the champion) from a "
        "noise-limited contract — both render as the same REFUSE"
    ),
)
def test_inert_probe_is_diagnosed_distinctly_from_noise_limited() -> None:
    """Two zero-signal causes, two different operator fixes.

    Champion draws that genuinely differ (a real, non-zero noise floor) plus
    a degraded scalar identical to their mean means the PROBE moved nothing —
    which is not the same as "your board is noise-limited", and sends an
    operator to a different fix.
    """
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.60),
        degraded_scalar=0.55,  # exactly the champion mean ⇒ the probe was inert
        floor_max_abs_delta=0.10,
    )
    assert signal == pytest.approx(0.0)
    assert verdict != VERDICT_REFUSE, "an inert probe is not a noise-limited contract"


def test_genuinely_noise_limited_contract_still_refuses() -> None:
    """The protection the #106 fix must not weaken: real signal under the floor."""
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.60),
        degraded_scalar=0.58,
        floor_max_abs_delta=0.10,
    )
    assert signal == pytest.approx(0.03)
    assert verdict == VERDICT_REFUSE


def test_clear_signal_still_passes() -> None:
    """And the happy path stays OK."""
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.52),
        degraded_scalar=0.95,
        floor_max_abs_delta=0.05,
    )
    assert signal > 0.05
    assert verdict == VERDICT_OK


# ---------------------------------------------------------------------------
# Issue #112 — assert the whole window, not just its lower bound
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #112: nothing compares the measured achievable signal against "
        "promote_margin, so a margin above achievable — a guaranteed-null run "
        "— passes pre-flight"
    ),
)
def test_margin_above_achievable_signal_is_flagged() -> None:
    """``noise < margin < achievable`` — the UPPER bound is never checked today.

    The campaign numbers from #112: floor 0.080-0.106, best achievable
    +0.041, configured margin 0.10 ⇒ 71 of 72 duels rejected by construction.
    """
    from zicato.epoch.preflight import preflight_window_verdict

    verdict, which_side = preflight_window_verdict(
        noise_floor=0.08,
        promote_margin=0.10,
        achievable_signal=0.30,
    )
    assert verdict == VERDICT_OK
    assert which_side is None

    verdict, which_side = preflight_window_verdict(
        noise_floor=0.08,
        promote_margin=0.30,
        achievable_signal=0.20,
    )
    assert verdict != VERDICT_OK
    assert which_side == "margin_above_achievable"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #112: the empty-window case (achievable <= noise) is reported "
        "identically to a mis-set margin, sending operators to tune a number "
        "that has no valid value"
    ),
)
def test_empty_window_is_flagged_distinctly() -> None:
    """When ``achievable <= noise`` NO margin is defensible — say exactly that."""
    from zicato.epoch.preflight import preflight_window_verdict

    _verdict, which_side = preflight_window_verdict(
        noise_floor=0.10,
        promote_margin=0.10,
        achievable_signal=0.041,
    )
    assert which_side == "empty_window"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #112 (floor statistic): the margin recommendation is based on "
        "max|delta|, a RANGE statistic that drifts upward with more "
        "calibration draws, pushing the margin toward the achievable signal"
    ),
)
def test_margin_recommendation_uses_a_draw_count_stable_statistic() -> None:
    """The recommendation must not degrade as calibration improves.

    ``max |delta|`` grows with the number of draws on an unchanged board;
    ``delta_std`` (already computed and reported alongside) does not. Basing
    the recommendation on the range statistic is the trap #112 describes.
    """
    from zicato.tournament.calibration import recommended_promote_margin

    two_draws = recommended_promote_margin(scalars=(0.50, 0.60))
    ten_draws = recommended_promote_margin(
        scalars=(0.50, 0.60, 0.51, 0.59, 0.52, 0.58, 0.49, 0.61, 0.53, 0.57)
    )
    assert (
        ten_draws <= two_draws * 1.5
    ), "the recommendation must be stable as draws accumulate, not drift upward"
