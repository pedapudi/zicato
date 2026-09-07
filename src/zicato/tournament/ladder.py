"""Budget and release rules for adaptive holdout confirmation.

One crowning comparison consumes one query, reserved before execution. The
train-side improvement must clear the configured threshold for its holdout
confirmation bit to be released. Withheld queries retain the previous released
best as historical feedback; that prior result cannot confirm another candidate.

The governor limits feedback but supplies no distribution-free guarantee for
arbitrary adaptive reuse. Its threshold comes from ``promote_margin`` unless
configured explicitly; ``noise_scale`` adds a fixed band without random noise.
This module owns pure decisions. Governance owns durable reservations and maps
withheld, exhausted, or incomplete confirmation to a deferred promotion.
An absent holdout disables confirmation. Disabling the governor still requires
an unmediated holdout confirmation whenever the contract has a holdout slice.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from zicato.core import ScoringWeights
from zicato.core.tournament import ConfirmationStatus
from zicato.core.types import LadderConfig

#: The minimum budget remaining for a query to be answerable. The budget is
#: charged *before* a release decision, so a budget of ``0`` releases nothing.
_BUDGET_FLOOR = 0


@dataclass(frozen=True, slots=True)
class LadderState:
    """Per-epoch Ladder state — the small object the runner persists across rounds.

    Fields
    ------
    budget_total:
        The configured per-epoch budget the state was seeded with
        (:attr:`LadderConfig.budget`). Constant for the epoch; recorded so
        the dashboard can render "k of N queries used".
    budget_remaining:
        Holdout queries still affordable this epoch. Decrements by one each
        round the holdout is consulted; never goes below ``0``.
    best_holdout_scalar:
        The best (lowest, since the scalar is a loss) holdout scalar
        *released* so far this epoch — the Ladder's "previous best". ``None``
        before the first release. Within the noise band the Ladder re-reports
        this value rather than the round's raw holdout scalar.
    best_confirmed:
        The confirmation bit from the query that supplied
        ``best_holdout_scalar``. The pair is re-reported when the Ladder
        withholds. ``None`` until the first release.
    """

    budget_total: int
    budget_remaining: int
    best_holdout_scalar: float | None = None
    best_confirmed: bool | None = None

    @classmethod
    def seed(cls, cfg: LadderConfig) -> LadderState:
        """A fresh per-epoch state from the config's budget."""
        return cls(budget_total=cfg.budget, budget_remaining=cfg.budget)


@dataclass(frozen=True, slots=True)
class LadderRelease:
    """The outcome of one Ladder-mediated holdout query.

    Fields
    ------
    released:
        ``True`` when the holdout signal was released this round (the
        train-measured improvement cleared the threshold and the budget was
        not exhausted). When ``False`` the holdout result does NOT count: the
        runner defers the promotion and retains the previous released best
        as historical feedback.
    confirmed:
        The threshold-gated confirmation bit fed back downstream: ``True`` =
        the train-win held on the holdout, ``False`` = it did not. On a
        *withheld* query this is the previous best confirmation
        (:attr:`LadderState.best_confirmed`), or ``None`` if nothing was ever
        released. The proposer is only ever shown this bit, never the raw
        per-entry holdout result.
    holdout_scalar:
        The holdout scalar associated with :attr:`confirmed`: the round's raw
        holdout scalar on a release, or the previous best on a withhold.
        ``None`` when nothing has been released.
    threshold:
        The effective release threshold this query used (see
        :func:`effective_threshold`: ``LadderConfig.threshold``, else
        ``promote_margin`` — plus ``noise_scale``).
    state:
        The new per-epoch state to persist (budget charged, best updated).
    """

    released: bool
    confirmed: bool | None
    holdout_scalar: float | None
    threshold: float
    state: LadderState


def effective_threshold(cfg: LadderConfig, weights: ScoringWeights) -> float:
    """Return the train-side release threshold plus the configured fixed band.

    An unset threshold uses ``promote_margin`` because release tests the training
    improvement. ``holdout_margin`` controls allowed holdout regression after
    release and is measured on a different board slice. Raising the release
    threshold can defer a challenger; it cannot authorize an unconfirmed one.
    """
    base = weights.promote_margin if cfg.threshold is None else cfg.threshold
    return base + cfg.noise_scale


def query_holdout(
    state: LadderState,
    *,
    cfg: LadderConfig,
    weights: ScoringWeights,
    train_parent_scalar: float,
    train_child_scalar: float,
    holdout_scalar: float,
    holdout_confirmed: bool,
) -> LadderRelease:
    """Mediate one holdout query through the Ladder. Pure; returns the new state.

    The caller has already (a) decided the train rules would promote and
    (b) computed the raw holdout confirmation bit (``holdout_confirmed`` —
    the :func:`zicato.tournament.gate` confirmation run on the holdout slice)
    and the holdout scalar. This function decides whether that bit is
    *released* this round.

    The release rule (OVERFITTING.md §4): the *train-measured* improvement
    over the champion is ``train_parent_scalar - train_child_scalar`` (the
    scalar is a loss, so a positive value is an improvement). It clears the
    bar when it is ``>= effective_threshold``. Only then is the holdout
    signal released; within the band the Ladder withholds and re-reports the
    previous best confirmation.

    Either way, consulting the holdout charges one unit of budget — UNLESS
    the budget is already exhausted, in which case nothing is released and
    the state is returned unchanged.
    """
    charged = reserve_holdout_query(state)
    if charged is None:
        return LadderRelease(
            released=False,
            confirmed=state.best_confirmed,
            holdout_scalar=state.best_holdout_scalar,
            threshold=effective_threshold(cfg, weights),
            state=state,
        )
    return decide_reserved_holdout(
        charged,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=train_parent_scalar,
        train_child_scalar=train_child_scalar,
        holdout_scalar=holdout_scalar,
        holdout_confirmed=holdout_confirmed,
    )


def reserve_holdout_query(state: LadderState) -> LadderState | None:
    """Return the state after charging one query, or ``None`` when exhausted.

    Persistence code writes this returned state before it starts work that can
    observe holdout evidence.  Keeping the charge pure here lets the durable
    governor enforce that ordering without putting filesystem concerns in the
    statistical mechanism.
    """
    if state.budget_remaining <= _BUDGET_FLOOR:
        return None
    return replace(state, budget_remaining=state.budget_remaining - 1)


def decide_reserved_holdout(
    charged: LadderState,
    *,
    cfg: LadderConfig,
    weights: ScoringWeights,
    train_parent_scalar: float,
    train_child_scalar: float,
    holdout_scalar: float,
    holdout_confirmed: bool,
) -> LadderRelease:
    """Apply the release rule to a query whose charge is already durable.

    ``charged`` already reflects the one-unit debit.  This function must not
    charge again; it only publishes the release/withhold decision and updates
    the best released confirmation.
    """
    threshold = effective_threshold(cfg, weights)

    improvement = train_parent_scalar - train_child_scalar
    if improvement >= threshold:
        # Release: the train-win cleared the bar, so the holdout result counts
        # this round. Update the best released holdout scalar (lower is better)
        # and the best confirmation bit.
        prev_best = charged.best_holdout_scalar
        improves_best = prev_best is None or holdout_scalar <= prev_best
        new_state = replace(
            charged,
            best_holdout_scalar=holdout_scalar if improves_best else prev_best,
            best_confirmed=(holdout_confirmed if improves_best else charged.best_confirmed),
        )
        return LadderRelease(
            released=True,
            confirmed=holdout_confirmed,
            holdout_scalar=holdout_scalar,
            threshold=threshold,
            state=new_state,
        )

    # Withhold: the improvement is within the noise band. Re-report the
    # previous best confirmation so the proposer cannot chase the fluctuation;
    # the holdout result does NOT count this round. The query is still charged
    # (we consulted the holdout to find the gap was within the band).
    return LadderRelease(
        released=False,
        confirmed=charged.best_confirmed,
        holdout_scalar=charged.best_holdout_scalar,
        threshold=threshold,
        state=charged,
    )


def holdout_record(
    *,
    confirmed: bool | None,
    train_scalar: float | None,
    holdout_scalar: float | None,
    consulted: bool,
    released: bool,
    budget_total: int,
    budget_before_query: int | None,
    budget_remaining: int,
    query_reserved: bool,
    threshold: float,
    confirmation_status: ConfirmationStatus | None = None,
    reason: str = "",
) -> dict[str, object]:
    """Serialize holdout confirmation and its durable query accounting.

    ``confirmation_status`` distinguishes disabled, satisfied, failed, and
    incomplete requirements. Only a released confirmation satisfies the current
    candidate. Withholding may repeat a historical ``confirmed`` bit and scalar;
    ``ladder_released=False`` and ``confirmation_status=incomplete`` identify
    that case. The reason never reveals an unreleased negative result.

    A missing holdout slice produces an explicit disabled block. A training
    rejection skips the conditional confirmation and has no holdout block.
    """
    return {
        "confirmation_status": confirmation_status
        or (
            ConfirmationStatus.SATISFIED
            if released and confirmed
            else ConfirmationStatus.FAILED
            if released
            else ConfirmationStatus.INCOMPLETE
        ),
        "reason": reason,
        "confirmed": confirmed,
        "train_scalar": train_scalar,
        "holdout_scalar": holdout_scalar,
        "holdout_consulted": consulted,
        "ladder_released": released,
        "ladder_budget_total": budget_total,
        "ladder_budget_before_query": budget_before_query,
        "ladder_budget_remaining": budget_remaining,
        "ladder_query_reserved": query_reserved,
        "threshold": threshold,
    }


def disabled_holdout_record() -> dict[str, object]:
    """Record that the evaluation contract has no holdout slice."""
    return holdout_record(
        confirmed=None,
        train_scalar=None,
        holdout_scalar=None,
        consulted=False,
        released=False,
        budget_total=0,
        budget_before_query=None,
        budget_remaining=0,
        query_reserved=False,
        threshold=0.0,
        confirmation_status=ConfirmationStatus.DISABLED,
        reason="no holdout slice in the evaluation contract",
    )


__all__ = [
    "LadderRelease",
    "LadderState",
    "decide_reserved_holdout",
    "effective_threshold",
    "holdout_record",
    "disabled_holdout_record",
    "query_holdout",
    "reserve_holdout_query",
]
