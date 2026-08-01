"""Sequential probability ratio test for early duel stopping (opt-in).

Wald SPRT on the mean of paired scalar deltas — the compute-savings knob for
duel-based tournament structures (gauntlet, single/double elim, swiss). See
``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` for the motivating discussion
and the design-conversation record of why the sign-only Bernoulli formulation
was rejected in favour of testing the gate's actual magnitude threshold.

Given paired observations ``d_i = scalar_child_i − scalar_parent_i`` per
replicate (same seed on the same board — pairing eliminates entry-difficulty
variance), we test the gate's own question:

    H0: E[d] = 0                    (no promote-worthy improvement)
    H1: E[d] = −promote_margin      (clearly beats the gate)

Under the Gaussian assumption ``d_i ~ N(μ, σ²)`` the log-likelihood ratio
accumulates linearly:

    LLR_n = ((μ_1 − μ_0) / σ²) · (S_n − n·(μ_0 + μ_1)/2)

with stopping boundaries

    LLR_n ≥ log((1−β)/α)   → declare H1 (promote)
    LLR_n ≤ log(β/(1−α))   → declare H0 (do not promote)
    otherwise               → continue

σ² is estimated online from the running sample (Bessel-corrected) after a
burn-in of ``min_replicates`` observations; before burn-in the state always
returns ``CONTINUE``. The estimator is floored on ``(promote_margin/2)²`` so
a tight cluster of near-zero early observations cannot under-estimate σ into
a runaway LLR. This is a plug-in approximation to a proper sequential
t-test — the ``max_replicates`` hard cap bounds any residual pathology.

Empirical calibration
---------------------
The Wald error-rate guarantees hold asymptotically. In practice, on a duel
with signal-to-noise ratio near 1 (σ ≈ ``promote_margin``) the H0 acceptance
rate under a truly-null challenger runs ~5-10 pp below the nominal
``1 − α``; the H1 acceptance rate on true blowouts is at or above nominal.
Average replicates used drops to ~5-8 on clear signals and ~15-20 on
marginal ones (vs a fixed schedule of, say, 30). The presets are calibrated
against these numbers on synthetic streams with σ = ``2·promote_margin``.

Style
-----
Everything here is PURE and FROZEN — no zicato imports, no I/O, no mutable
state (mirroring :mod:`zicato.selection.evidence_gate` and
:mod:`zicato.selection.rating`). The invariants of a sequence
(:class:`SprtParams`) and its accumulating record (:class:`SprtState`) are
separate frozen dataclasses; the sole update rule (:func:`advance`) takes
both plus one observation and returns a new state. Opt-in via
``ScoringWeights.sprt`` (default ``off``). Racing is refused fail-fast at
contract load — best-arm identification (LUCB / Successive Rejects), not
paired sequential testing, is the right sequential rule for a racing rung.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

__all__ = [
    "SPRT_PRESETS",
    "SprtDecision",
    "SprtParams",
    "SprtPreset",
    "SprtState",
    "advance",
    "initial_state",
    "mean_and_ci",
    "resolve_preset",
]


# ---------------------------------------------------------------------------
# Decision + presets
# ---------------------------------------------------------------------------


class SprtDecision(StrEnum):
    """Terminal state of one SPRT sequence.

    * :attr:`CONTINUE` — insufficient evidence; the caller runs one more
      replicate and calls :func:`advance` again.
    * :attr:`H0` — accept the null (no promote-worthy improvement). The
      caller's loop exits; downstream scoring proceeds on the replicates
      collected so far.
    * :attr:`H1` — accept the alternative (clearly beats
      ``promote_margin``). Same exit + downstream flow as H0.
    """

    CONTINUE = "continue"
    H0 = "h0"
    H1 = "h1"


#: The four preset tokens the operator picks in the contract's ``sprt`` block.
#: ``"off"`` is the disabled sentinel (no state is ever constructed); the
#: other three map to ``(alpha, beta, min_replicates)`` via
#: :data:`SPRT_PRESETS`. The effect size is always derived from
#: :attr:`ScoringWeights.promote_margin` since SPRT tests directly against
#: the gate's own threshold — there is no separate ``delta`` knob to keep
#: in sync.
SprtPreset = Literal["off", "conservative", "balanced", "aggressive"]

#: Preset → ``(alpha, beta, min_replicates)``.
#:
#: * ``conservative`` rarely stops early — cuts only clear blowouts.
#: * ``balanced`` is the recommended default when opting in.
#: * ``aggressive`` cuts aggressively; occasionally miscalls the marginal
#:   band inside the indifference zone ``(−promote_margin, 0)``.
SPRT_PRESETS: dict[str, tuple[float, float, int]] = {
    "conservative": (0.02, 0.02, 8),
    "balanced": (0.05, 0.05, 5),
    "aggressive": (0.10, 0.10, 3),
}


# ---------------------------------------------------------------------------
# Frozen params + frozen running state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SprtParams:
    """Invariants of one SPRT sequence.

    A :class:`SprtParams` is constructed once per duel from the contract
    (via :func:`resolve_preset` plus the caller's ``promote_margin`` and
    ``max_replicates``) and is threaded verbatim through every
    :func:`advance` call — the running-state update never mutates it.

    Fields
    ------
    alpha:
        Type-I error rate (probability of falsely declaring H1 when H0 is
        true). Must be in ``(0, 0.5)``.
    beta:
        Type-II error rate (probability of falsely declaring H0 when H1 is
        true). Must be in ``(0, 0.5)``.
    promote_margin:
        The gate's ``promote_margin`` (a positive number). SPRT tests
        ``H1: μ = −promote_margin`` against ``H0: μ = 0`` — the gate's
        actual question, not a sign-only proxy.
    min_replicates:
        Burn-in floor. Before this many observations :func:`advance`
        always returns ``CONTINUE`` (a plug-in variance estimate on a
        tiny sample is ill-behaved). Must be ``>= 2``.
    max_replicates:
        Hard cap. On reaching this many observations without an LLR
        crossing, the sequence settles via :func:`_settle_at_cap` so the
        caller's loop has a deterministic exit. Must be ``>= min_replicates``.
    """

    alpha: float
    beta: float
    promote_margin: float
    min_replicates: int
    max_replicates: int

    def __post_init__(self) -> None:
        if not (0.0 < self.alpha < 0.5):
            raise ValueError(f"alpha must be in (0, 0.5), got {self.alpha}")
        if not (0.0 < self.beta < 0.5):
            raise ValueError(f"beta must be in (0, 0.5), got {self.beta}")
        if self.promote_margin <= 0.0:
            raise ValueError(
                f"promote_margin must be > 0 (SPRT tests μ = −promote_margin "
                f"as H1); got {self.promote_margin}"
            )
        if self.min_replicates < 2:
            raise ValueError(
                f"min_replicates must be >= 2 for online sigma estimation, "
                f"got {self.min_replicates}"
            )
        if self.max_replicates < self.min_replicates:
            raise ValueError(
                f"max_replicates ({self.max_replicates}) must be >= "
                f"min_replicates ({self.min_replicates})"
            )


@dataclass(frozen=True, slots=True)
class SprtState:
    """Running record of one SPRT sequence — folded by :func:`advance`.

    A fresh state is constructed via :func:`initial_state`; each
    :func:`advance` returns a NEW state (nothing is mutated in place),
    matching :mod:`zicato.selection.evidence_gate` /
    :mod:`zicato.selection.rating`. The caller stops as soon as
    :attr:`decision` is not :attr:`SprtDecision.CONTINUE`.

    Fields
    ------
    n:
        Number of observations folded so far. Also the count the caller
        reports back as ``replicates_used``.
    sum_x:
        Running sum of observations.
    sum_x2:
        Running sum of squared observations (for the Bessel-corrected
        online variance estimate).
    decision:
        Current terminal state (or :attr:`SprtDecision.CONTINUE`).
    """

    n: int = 0
    sum_x: float = 0.0
    sum_x2: float = 0.0
    decision: SprtDecision = SprtDecision.CONTINUE


# ---------------------------------------------------------------------------
# Preset resolution
# ---------------------------------------------------------------------------


def resolve_preset(
    preset: SprtPreset,
    *,
    promote_margin: float,
    max_replicates: int,
    alpha: float | None = None,
    beta: float | None = None,
    min_replicates: int | None = None,
) -> SprtParams:
    """Build an :class:`SprtParams` from a preset name with optional overrides.

    Presets resolve to ``(alpha, beta, min_replicates)`` via
    :data:`SPRT_PRESETS`. Explicit keyword params override any preset default
    piecewise — pass a preset plus, say, ``min_replicates=5`` and only that
    field is overridden. The effect size is always derived from
    ``promote_margin`` since SPRT tests against the gate's own threshold.

    ``preset="off"`` raises — the config layer handles the disabled case by
    not constructing params at all, so a reached call means the caller has
    already committed to running SPRT.
    """
    if preset == "off":
        raise ValueError(
            "cannot resolve params for the 'off' preset — the config layer "
            "should skip SPRT construction entirely when disabled"
        )
    if preset not in SPRT_PRESETS:
        valid = ", ".join(sorted(SPRT_PRESETS))
        raise ValueError(f"unknown SPRT preset {preset!r}; valid: {valid}")
    p_alpha, p_beta, p_min = SPRT_PRESETS[preset]
    return SprtParams(
        alpha=p_alpha if alpha is None else alpha,
        beta=p_beta if beta is None else beta,
        promote_margin=promote_margin,
        min_replicates=p_min if min_replicates is None else min_replicates,
        max_replicates=max_replicates,
    )


# ---------------------------------------------------------------------------
# State construction + update
# ---------------------------------------------------------------------------


def initial_state() -> SprtState:
    """The fresh, zeroed state one duel starts from.

    A trivial factory kept explicit so the caller never constructs a
    :class:`SprtState` directly — parallel to how the evidence gate hands out
    verdicts through named helpers rather than exposing its fit records.
    """
    return SprtState()


def advance(
    params: SprtParams,
    state: SprtState,
    observation: float,
) -> SprtState:
    """Fold one paired-delta observation into ``state`` and return a NEW state.

    Pure — ``params`` and the input ``state`` are never mutated. The caller
    loops until the returned state's :attr:`SprtState.decision` is not
    :attr:`SprtDecision.CONTINUE`:

    ::

        state = initial_state()
        for _ in range(params.max_replicates):
            d = run_paired_replicate()               # child_scalar − parent_scalar
            state = advance(params, state, d)
            if state.decision is not SprtDecision.CONTINUE:
                break

    Calling :func:`advance` on an already-terminated state raises — a fresh
    state is required per duel, so a stale record can never silently corrupt
    the next duel's decision.
    """
    if state.decision is not SprtDecision.CONTINUE:
        raise ValueError(
            "SPRT already terminated; construct a fresh state per duel via " "initial_state()"
        )

    n = state.n + 1
    sum_x = state.sum_x + observation
    sum_x2 = state.sum_x2 + observation * observation

    # Hard cap: on reaching ``max_replicates`` without an LLR crossing,
    # settle the decision from the sample mean vs the midpoint of the
    # indifference zone. Preserves a deterministic exit for the caller
    # ("cap-hit close cases go to the majority of evidence") without
    # pretending SPRT was decisive.
    if n >= params.max_replicates:
        decision = _settle_at_cap(params=params, n=n, sum_x=sum_x)
        return replace(state, n=n, sum_x=sum_x, sum_x2=sum_x2, decision=decision)

    # Burn-in: need enough samples to estimate sigma before running LLR. Before
    # burn-in, always CONTINUE — the LLR under a plug-in variance is
    # ill-behaved on tiny samples (it would trigger on the first outlier).
    if n < params.min_replicates:
        return replace(state, n=n, sum_x=sum_x, sum_x2=sum_x2)

    decision = _llr_decision(params=params, n=n, sum_x=sum_x, sum_x2=sum_x2)
    return replace(state, n=n, sum_x=sum_x, sum_x2=sum_x2, decision=decision)


# ---------------------------------------------------------------------------
# Internal helpers — kept module-private so the seam a caller programs
# against is the (params, state, advance) trio above.
# ---------------------------------------------------------------------------


def _llr_decision(
    *,
    params: SprtParams,
    n: int,
    sum_x: float,
    sum_x2: float,
) -> SprtDecision:
    """LLR-boundary check after burn-in but before the hard cap.

    Split out so :func:`advance` stays a thin driver — the two terminal
    branches (cap, LLR) each own a small pure helper.
    """
    # Sample variance (Bessel-corrected), floored on a PRIOR: the noise scale
    # cannot honestly be much smaller than the effect size we are trying to
    # detect. The floor is ``(promote_margin/2)^2`` — noise standard deviation
    # >= half the effect size, i.e. a signal-to-noise ratio of at most 2. On
    # small samples the raw sample variance is a poor estimator (a tight
    # cluster of 3–5 near-zero observations under-estimates σ dramatically,
    # inflating LLR and causing early false stops); the floor tames the
    # small-sample plug-in pathology without touching the well-estimated case
    # (large-n runs move away from the floor as sample variance grows above
    # it). Also handles the deterministic-constant-stream case (sample var
    # exactly zero) without a separate branch.
    mean = sum_x / n
    sample_var = (sum_x2 - n * mean * mean) / (n - 1)
    var_floor = (params.promote_margin / 2.0) ** 2
    var = max(sample_var, var_floor)

    # H0: μ_0 = 0 (no improvement). H1: μ_1 = −promote_margin (child wins
    # clearly). Sign convention: child wins ⇒ observation < 0.
    mu0 = 0.0
    mu1 = -params.promote_margin
    llr = ((mu1 - mu0) / var) * (sum_x - n * (mu0 + mu1) / 2.0)

    upper = math.log((1.0 - params.beta) / params.alpha)  # accept H1 at ≥
    lower = math.log(params.beta / (1.0 - params.alpha))  # accept H0 at ≤

    if llr >= upper:
        return SprtDecision.H1
    if llr <= lower:
        return SprtDecision.H0
    return SprtDecision.CONTINUE


def mean_and_ci(
    state: SprtState,
    params: SprtParams,
    *,
    z: float = 1.96,
) -> tuple[float, float]:
    """Sample mean and normal-approx CI half-width for a settled sequence.

    Returned as ``(mean, half_width)`` — the 95% CI (``z = 1.96``, the default)
    is then ``mean ± half_width``. Used by downstream rating consumers to
    inform their update weight (e.g. the Elo margin-K-weighting story in
    ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §5 — a wider CI ⇒ a
    less-confident magnitude ⇒ a smaller rating update than the point
    estimate alone would justify).

    The variance is the same Bessel-corrected sample variance floored on
    ``(promote_margin/2)²`` that drives :func:`advance`'s LLR — so a
    consumer that gates on ``half_width`` sees the same "the plug-in noise
    cannot be tinier than the effect we are testing" prior the stopping
    rule already applies. Returns ``(0.0, inf)`` for a state that has not
    collected any observations (``n == 0``); returns ``(mean, inf)`` for a
    state with a single observation (variance undefined).
    """
    n = state.n
    if n == 0:
        return 0.0, float("inf")
    mean = state.sum_x / n
    if n < 2:
        return mean, float("inf")
    sample_var = (state.sum_x2 - n * mean * mean) / (n - 1)
    var_floor = (params.promote_margin / 2.0) ** 2
    var = max(sample_var, var_floor)
    half_width = z * math.sqrt(var / n)
    return mean, half_width


def _settle_at_cap(
    *,
    params: SprtParams,
    n: int,
    sum_x: float,
) -> SprtDecision:
    """Terminal decision when the sequence hits ``max_replicates``.

    Neither boundary was crossed within budget. Fall back to a point estimate:
    sample mean vs the midpoint of the indifference zone
    ``(−promote_margin, 0)``. Below the midpoint → H1 (majority of evidence
    favours promote); at or above → H0.
    """
    if n == 0:
        return SprtDecision.H0
    mean = sum_x / n
    midpoint = -params.promote_margin / 2.0
    return SprtDecision.H1 if mean <= midpoint else SprtDecision.H0
