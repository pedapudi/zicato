"""The minimum detectable effect and its inverse, from the measured noise floor.

The two-sample minimum detectable effect is the smallest difference between
two arm means that a two-sided test at a false-positive rate ``alpha``
detects with probability ``power``, when each arm holds ``n`` independent
observations of standard deviation ``sd``:

    MDE = (t_{α/2,df} + t_{β,df}) · sd · √(2/n),   df = 2(n−1)

:func:`minimum_detectable_effect` computes it; :func:`replicates_for_margin`
inverts it over the replicate ladder, returning the smallest count whose
effect is within a promote margin. Every surface that serves or acts on a
detectable effect reads these two functions: the instrument-health ladder
(:func:`zicato.query.eval_view.mde_ladder`), the replicate count an epoch
runs (:func:`zicato.selection.replicates.resolve_replicates`), and the racing
rung cut (:class:`zicato.selection.strategies.racing.RacingStrategy`).

The module imports only the standard library, so the selection layer can
read it without reaching the execution stack that
:mod:`zicato.tournament.calibration` drives.
"""

from __future__ import annotations

import math

#: The operating characteristics of the detectable-effect formula: the
#: two-sided false-positive rate, a relaxed rate the instrument panel shows
#: beside it, and the power. At ``n = 6`` (``df = 10``) the formula gives
#: ``1.79 × sd`` at α 0.05 and ``1.55 × sd`` at α 0.10, the reference numbers
#: ``docs/design/CAMPAIGN.md`` §3 pins.
MDE_ALPHA: float = 0.05
MDE_ALPHA_RELAXED: float = 0.10
MDE_POWER: float = 0.80
#: The formula, as every surface that serves a detectable effect prints it.
MDE_FORMULA: str = (
    "MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n),  sd = the floor's delta_std,  df = 2·(n−1)"
)

#: The largest replicate count :func:`replicates_for_margin` sizes. It equals
#: the evidence gate's scaffolded ``promote_confidence_replicates`` budget,
#: the largest replicate spend the shipped contracts already budget for one
#: decision. At 32 replicates the formula resolves a margin of about
#: ``0.71 × delta_std``; a margin below that sits inside the floor's own range
#: for a calibration of five or more draws, where the margin check already
#: recommends raising the margin, so a larger count would buy replicates for a
#: margin the calibration says to raise instead.
REPLICATE_SIZING_CAP: int = 32


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion of the incomplete beta (Lentz's method).

    The Numerical-Recipes ``betacf`` — used by :func:`_reg_incomplete_beta` in
    the region where the fraction converges quickly. Pure standard-library math.
    """
    max_iter = 200
    eps = 3.0e-16
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _reg_incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta ``I_x(a, b)`` (Numerical Recipes ``betai``)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def students_t_upper_quantile(upper_tail: float, df: int) -> float:
    """The Student-t value ``t`` with ``P(T > t) = upper_tail`` for ``df`` (df ≥ 1).

    Inverts the two-tailed survival ``P(|T| > t) = I_{df/(df+t²)}(df/2, 1/2)`` by
    bisection — pure standard-library math (no SciPy runtime dependency). Exact to
    machine precision against the standard t-tables; unit-tested against them.
    """
    p2 = 2.0 * upper_tail
    lo, hi = 0.0, 1.0e7
    for _ in range(160):
        mid = (lo + hi) / 2.0
        # Survival is monotone decreasing in t; walk toward the target tail mass.
        if _reg_incomplete_beta(df / 2.0, 0.5, df / (df + mid * mid)) > p2:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def minimum_detectable_effect(
    sd: float, n: int, *, alpha: float = MDE_ALPHA, power: float = MDE_POWER
) -> float | None:
    """The two-sample minimum detectable effect at ``n`` observations per arm. Pure.

    ``(t_{α/2,df} + t_{β,df}) · sd · √(2/n)`` with ``df = 2(n−1)``: the
    smallest difference between two arm means that a two-sided test at
    ``alpha`` detects with probability ``power``, when each arm holds ``n``
    independent observations of standard deviation ``sd``. ``None`` when
    ``n < 2``: the two-sample form has no degrees of freedom below two
    observations per arm, and a bound must never be fabricated for it.

    The callers agree on what one observation is. The epoch-level ladder and
    :func:`replicates_for_margin` take one full-board replicate as the
    observation and the floor's ``delta_std`` as its ``sd``. That statistic is
    the deviation of one A/A duel's difference, ``√2`` times the deviation of
    one arm's scalar, so the count it sizes errs toward more replicates rather
    than fewer. A racing rung takes one entry-replicate as the observation
    (:meth:`zicato.selection.strategies.racing.RacingStrategy`).
    """
    if n < 2:
        return None
    df = 2 * (n - 1)
    t_alpha = students_t_upper_quantile(alpha / 2.0, df)
    t_beta = students_t_upper_quantile(1.0 - power, df)
    return (t_alpha + t_beta) * float(sd) * math.sqrt(2.0 / n)


def replicates_for_margin(
    delta_std: float,
    margin: float,
    *,
    alpha: float = MDE_ALPHA,
    power: float = MDE_POWER,
    cap: int = REPLICATE_SIZING_CAP,
) -> int | None:
    """The smallest replicate count whose detectable effect is within ``margin``. Pure.

    The inverse of :func:`minimum_detectable_effect` over the replicate ladder:
    counts ``n = 2, 3, …, cap`` are tried in order and the first whose effect
    at ``delta_std`` is at or below ``margin`` is returned. The effect falls
    monotonically in ``n`` (the t quantiles and ``√(2/n)`` both fall), so for
    every count on the ladder ``replicates_for_margin(sd, effect(n)) == n``.
    ``None`` when no count up to ``cap`` resolves the margin, and for a margin
    at or below zero, which no positive effect can reach. A floor of ``0.0``
    (a deterministic system under test) resolves any positive margin at two.
    """
    if margin <= 0.0:
        return None
    for n in range(2, int(cap) + 1):
        effect = minimum_detectable_effect(delta_std, n, alpha=alpha, power=power)
        if effect is not None and effect <= margin:
            return n
    return None


__all__ = [
    "MDE_ALPHA",
    "MDE_ALPHA_RELAXED",
    "MDE_FORMULA",
    "MDE_POWER",
    "REPLICATE_SIZING_CAP",
    "minimum_detectable_effect",
    "replicates_for_margin",
    "students_t_upper_quantile",
]
