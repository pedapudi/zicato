"""Pins for issue #119 — the margin window measures the wrong headroom.

:func:`zicato.epoch.preflight.run_contract_preflight` measures

    ``signal = abs(degraded_scalar - champion_mean)``

(``preflight.py``, the probe loop) — how much the scalar moves when a mutation
point is DESTROYED — and then feeds that straight into
:func:`~zicato.epoch.preflight.preflight_window_verdict` as the
``achievable_signal`` a challenger has to clear in the IMPROVING direction.

The two quantities are unrelated in general and diverge hardest exactly where
the loop is most often started: a champion seeded near the failing end has
little left to lose (small degradation headroom) and everything to gain (large
improvement headroom). The guard then refuses a margin the board can clear.

Both pins build the report the way ``run_contract_preflight`` does — the same
``preflight_window_verdict(floor.max_abs_delta, margin, signal)`` call, the same
:class:`~zicato.epoch.preflight.PreflightReport` construction — so they follow
whatever the production path decides rather than restating it.
"""

from __future__ import annotations

import pytest

from zicato.epoch.preflight import (
    VERDICT_OK,
    VERDICT_REFUSE,
    PreflightReport,
    effective_gate_verdict,
    preflight_window_verdict,
)

# The floor-anchored board from the issue, as measured: a champion whose loss
# sits at 0.8 (so 0.8 of loss remains to be won back, with 0.0 the perfect
# score), a degraded copy that reaches the worst possible 1.0, and an A/A noise
# floor well below both. The operator's margin of 0.28 is comfortably inside
# the 0.8 of improvement headroom and comfortably outside the 0.2 of
# degradation headroom.
_CHAMPION_SCALARS: tuple[float, ...] = (0.80, 0.80, 0.80)
_CHAMPION_MEAN = 0.80
_DEGRADED_SCALAR = 1.00
_NOISE_FLOOR = 0.02
_MARGIN = 0.28
_MEASURED_SIGNAL = abs(_DEGRADED_SCALAR - _CHAMPION_MEAN)  # 0.2 — degradation headroom


def _report() -> PreflightReport:
    """A pre-flight report for the floor-anchored board, built the production way."""
    window_verdict, window_failure = preflight_window_verdict(
        _NOISE_FLOOR, _MARGIN, _MEASURED_SIGNAL
    )
    return PreflightReport(
        epoch_id="e0",
        generation_id="v0",
        verdict=VERDICT_OK,
        noise_floor_max_abs_delta=_NOISE_FLOOR,
        noise_floor_runs=len(_CHAMPION_SCALARS),
        champion_scalars=_CHAMPION_SCALARS,
        degraded_scalar=_DEGRADED_SCALAR,
        signal=_MEASURED_SIGNAL,
        degraded_mutation_id="m0",
        degraded_mutation_kind="span",
        degraded_file="/tmp/harness/prompt.py",
        measured_at="2026-07-29T00:00:00+00:00",
        promote_margin=_MARGIN,
        window_verdict=window_verdict,
        window_failure=window_failure,
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #119: the window compares promote_margin against DEGRADATION "
        "headroom (0.2), so a floor-anchored champion with 0.8 of improvement "
        "headroom is refused for a margin of 0.28 the board can clear"
    ),
)
def test_floor_anchored_champion_is_not_refused_for_a_reachable_margin() -> None:
    """A margin inside the improvement headroom must not stop the run.

    The champion's loss is 0.80 and a perfect score is 0.0, so a challenger has
    0.80 of loss available to win back — nearly three times the 0.28 margin.
    Degrading a mutation point only moves the scalar 0.20, because there is
    little left to break. Refusing here is the guard reporting the one number
    it has, not the one the decision needs.
    """
    assert effective_gate_verdict(_report().to_json()) != VERDICT_REFUSE


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #119: the persisted pre-flight record carries only the "
        "degradation signal, so an operator cannot see that improvement "
        "headroom was 4x the measured signal and tell whether to believe the "
        "refusal"
    ),
)
def test_preflight_record_reports_both_headrooms() -> None:
    """The record must surface improvement headroom alongside ``signal``.

    Even if the verdict were left alone, an operator reading
    ``signal: 0.2`` next to ``improvement_headroom: 0.8`` learns immediately
    which bound fired and why it is or is not the right one. Today only the
    first is written, so the refusal is unexplainable from the artifact.
    """
    record = _report().to_json()
    assert "signal" in record, "precondition: the degradation signal is persisted today"
    assert "improvement_headroom" in record
