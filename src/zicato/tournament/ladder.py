"""The Ladder: a noisy, budgeted governor over the holdout query (OVERFITTING.md §4, §12 #2).

Phase A built the train/holdout split (:mod:`zicato.board.split`) and a
holdout-*confirmation* step in the gate (:func:`zicato.tournament.gate.evaluate_gate`):
a train-measured win must also hold on a held-out slice the proposer never
sees. That makes a *single* holdout query trustworthy. It does nothing about
the deeper failure this note is written for: the proposer queries the *same*
holdout every round of an epoch, adaptively, and a reused holdout "gets used
up" — its confirmations become an optimistically-biased signal the optimizer
can climb.

[Blum & Hardt 2015][ladder] give the mechanism for exactly this "submit, see
score, submit again" loop. The Ladder releases a new holdout-based signal only
when the submission improves on its previous best *beyond a noise threshold*;
within the band it re-reports the previous best, so the analyst cannot chase
fluctuations. Mediating every query this way keeps a reused holdout valid
against an unbounded — even adversarial — number of submissions.

This module is the parameter-free Ladder (Blum–Hardt's tuning-free variant):
the noise threshold seeds from the gate's existing ``promote_margin`` and the
default ``noise_scale`` is ``0`` (no DP-grade noise calibration yet). It owns
the *pure* mechanism only — no filesystem, no clock, no randomness. The
per-epoch state is passed in and a fresh state is returned (the runner
persists it; see :func:`zicato.tournament.runner`).

The two rules, applied per holdout query:

1. **Release rule.** A new holdout-based signal is *released* (it can flip a
   train-win to confirmed / rejected) only when the *train-measured*
   improvement over the champion clears the threshold beyond the noise band.
   Within the band, the Ladder withholds — it re-reports the previous best
   confirmation and the holdout result does NOT count this round.
2. **Budget.** Every query that *consults the holdout* charges one unit of the
   per-epoch budget. When the budget is exhausted, no further holdout signals
   are released — the loop degrades to "champion stands": a train-win is no
   longer holdout-gated, so it promotes on the train rules alone (exactly as
   Phase A behaves with no holdout).

When the holdout is empty (a small board, the split disabled, no tagged
entry) there is nothing to govern and the Ladder is never consulted —
behaviour is byte-identical to Phase A. When ``LadderConfig.enabled`` is
``False`` the runner runs the raw Phase-A confirmation (no budget, no release
rule) directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from zicato.core import ScoringWeights
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
        until the first release. Within the noise band the Ladder re-reports
        this value rather than the round's raw holdout scalar.
    best_confirmed:
        The confirmation bit of the most recent *released* query — what is
        re-reported when the Ladder withholds. ``None`` until the first
        release.
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
        runner falls back to the train decision and re-reports the previous
        best confirmation.
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
    """The release threshold: ``cfg.threshold`` (or ``promote_margin``) + noise band.

    Parameter-free by default — ``cfg.threshold is None`` reuses the gate's
    existing ``promote_margin`` noise threshold, and ``cfg.noise_scale`` is
    ``0`` so the band collapses to that bar. An operator can pin the bar or
    widen the band explicitly.

    ``promote_margin`` and NOT :attr:`ScoringWeights.holdout_margin`, even
    though this sits on the holdout path. What :func:`query_holdout` compares
    against this bar is the TRAIN-measured improvement
    (``train_parent_scalar - train_child_scalar``), so the train-calibrated
    bound is the commensurable one; ``holdout_margin`` is calibrated against
    the holdout slice's own coarser quantization and would be the same
    category error on this line that issue #118 fixed inside the gate.

    Substituting it here would also invert the guard it belongs to. A
    WITHHELD query does not gate: the train promote stands and the holdout's
    veto is skipped for that round. Under the documented rule of thumb
    ``holdout_margin ≈ promote_margin × N_train / N_holdout`` is the LARGER
    number, so the substitution raises the release bar and every challenger
    whose train improvement falls in ``[promote_margin, holdout_margin)`` —
    which is exactly the marginal band Rule 1 admits — would promote without
    the holdout ever being consulted. An operator who separates the two
    bounds to unblock a promotable board would silently switch off
    board-memorization confirmation for the promotions they just unblocked.
    Widening the release band is a deliberate act; pin
    :attr:`LadderConfig.threshold` to do it.
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
    Phase A's :func:`zicato.tournament.gate` confirmation on the holdout
    slice) and the holdout scalar. This function decides whether that bit is
    *released* this round.

    The release rule (OVERFITTING.md §4): the *train-measured* improvement
    over the champion is ``train_parent_scalar - train_child_scalar`` (the
    scalar is a loss, so a positive value is an improvement). It clears the
    bar when it is ``>= effective_threshold``. Only then is the holdout
    signal released; within the band the Ladder withholds and re-reports the
    previous best confirmation.

    Either way, consulting the holdout charges one unit of budget — UNLESS
    the budget is already exhausted, in which case nothing is released (the
    loop degrades to "champion stands") and the state is returned unchanged.
    """
    threshold = effective_threshold(cfg, weights)

    # Budget exhausted: stop releasing holdout signals. Nothing is charged
    # (there is nothing left to charge) and no signal is released — the runner
    # degrades to the train-only decision ("champion stands"). The re-reported
    # bit is the last released best, so the dashboard still shows it.
    if state.budget_remaining <= _BUDGET_FLOOR:
        return LadderRelease(
            released=False,
            confirmed=state.best_confirmed,
            holdout_scalar=state.best_holdout_scalar,
            threshold=threshold,
            state=state,
        )

    # Charge the query (we are consulting the holdout this round).
    charged = replace(state, budget_remaining=state.budget_remaining - 1)

    improvement = train_parent_scalar - train_child_scalar
    if improvement >= threshold:
        # Release: the train-win cleared the bar, so the holdout result counts
        # this round. Update the best released holdout scalar (lower is better)
        # and the best confirmation bit.
        prev_best = charged.best_holdout_scalar
        new_best = holdout_scalar if prev_best is None else min(prev_best, holdout_scalar)
        new_state = replace(
            charged,
            best_holdout_scalar=new_best,
            best_confirmed=holdout_confirmed,
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
    released: bool,
    budget_total: int,
    budget_remaining: int,
    threshold: float,
) -> dict[str, object]:
    """Assemble the stable ``record.holdout`` block the dashboard reads.

    The shape is fixed (OVERFITTING.md §12 #2) so a parallel dashboard agent
    can consume it without coupling to this module's internals::

        {
            "confirmed": bool | None,
            "train_scalar": float | None,
            "holdout_scalar": float | None,
            "ladder_released": bool,
            "ladder_budget_total": int,
            "ladder_budget_remaining": int,
            "threshold": float,
        }

    ``None`` for ``confirmed`` / ``train_scalar`` / ``holdout_scalar`` carries
    "no value this round" (e.g. the Ladder withheld before any release). The
    runner writes ``record.holdout = None`` entirely when there was no holdout
    to consult, so a populated block always means a holdout existed.
    """
    return {
        "confirmed": confirmed,
        "train_scalar": train_scalar,
        "holdout_scalar": holdout_scalar,
        "ladder_released": released,
        "ladder_budget_total": budget_total,
        "ladder_budget_remaining": budget_remaining,
        "threshold": threshold,
    }


__all__ = [
    "LadderRelease",
    "LadderState",
    "effective_threshold",
    "holdout_record",
    "query_holdout",
]
