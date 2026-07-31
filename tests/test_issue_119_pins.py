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
improvement headroom). The guard used to REFUSE such a run outright.

The fix keeps the measurement and drops the claim: the quantity is persisted as
``degradation_signal`` (with the legacy ``signal`` key retained), and the
window verdict is a warning that can no longer hard-refuse a run even under
``preflight_gate="refuse"``. The floor-based refusals, which measure honestly,
are untouched. Improvement headroom stays UNMEASURED — see
:func:`test_preflight_record_names_the_headroom_it_measured` for why the
issue's own suggested number could not be produced honestly.

Every pin builds the report the way ``run_contract_preflight`` does — the same
``preflight_window_verdict(floor.max_abs_delta, margin, signal)`` call, the same
:class:`~zicato.epoch.preflight.PreflightReport` construction — so they follow
whatever the production path decides rather than restating it.
"""

from __future__ import annotations

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


def test_floor_anchored_champion_is_not_refused_for_a_reachable_margin() -> None:
    """A margin inside the improvement headroom must not stop the run.

    The champion's loss is 0.80 and a perfect score is 0.0, so a challenger has
    0.80 of loss available to win back — nearly three times the 0.28 margin.
    Degrading a mutation point only moves the scalar 0.20, because there is
    little left to break. Refusing here is the guard reporting the one number
    it has, not the one the decision needs.

    The fix keeps the measurement and drops the claim: the window verdict is a
    WARNING now, so this board is warned about and allowed to run.
    """
    assert effective_gate_verdict(_report().to_json()) != VERDICT_REFUSE


def test_a_legacy_persisted_refusal_no_longer_stops_a_run() -> None:
    """Records written before the demotion must not keep hard-stopping runs.

    ``effective_gate_verdict`` reads the PERSISTED record, so an epoch
    pre-flighted under the old code carries ``window_verdict: "refuse"`` on
    ``margin_above_achievable`` forever. Honouring it would keep refusing on
    exactly the finding this fix retracted, on every resumed round.
    """
    legacy = {
        **_report().to_json(),
        "window_verdict": VERDICT_REFUSE,
        "window_failure": "margin_above_achievable",
    }
    assert effective_gate_verdict(legacy) != VERDICT_REFUSE


def test_preflight_record_names_the_headroom_it_measured() -> None:
    """The record must say WHICH headroom ``signal`` is.

    The issue asked for improvement headroom beside the degradation signal.
    That number is not available: the scalar's reachable floor is not ``0``
    once a namespace carries a negative weight, so "champion_mean - 0" would
    be a fabricated bound, and deriving a real one from the namespace weights
    is registered rather than built. What the record can do — and now does —
    is name the quantity it actually holds, so an operator reading
    ``degradation_signal: 0.2`` knows it measures what there is to LOSE and
    does not read it as what there is to gain.
    """
    record = _report().to_json()
    assert "signal" in record, "the legacy key stays so existing readers keep working"
    assert record["degradation_signal"] == record["signal"]
