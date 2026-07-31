"""Tournament governance: the promote-gate envelope and the Ladder governor.

Pure decision helpers split out of :mod:`zicato.tournament.runner`. They
take already-computed loss profiles / aggregate dicts and turn them into
:class:`~zicato.tournament.gate.GateOutcome` decisions plus the holdout /
Ladder evidence block. Nothing here spawns a worker, touches the cache,
or schedules a board unit — these are the train/holdout slicing
(:func:`_train_aggs` / :func:`_holdout_aggs` / :func:`_losses_for`), the
Ladder-mediated holdout confirmation (:func:`_ladder_mediated_outcome`
plus its state I/O :func:`_load_ladder_state` / :func:`_save_ladder_state`),
and the regression-rejection builder (:func:`_regression_rejection`).

The runner re-exports every name here, so the historical import surface
(``zicato.tournament.runner._ladder_mediated_outcome`` and the rest) is
unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    LossProfile,
    ScoringWeights,
    TournamentDecision,
)
from zicato.tournament.gate import GateOutcome
from zicato.tournament.regression import RegressionResult
from zicato.tournament.scoring import aggregate_generation_score

log = logging.getLogger("zicato.tournament.runner")


def _losses_for(
    board: list[BoardEntry],
    id_set: set[str],
    losses: dict[str, LossProfile],
) -> list[LossProfile]:
    """Return the loss profiles for ``id_set``, in board order.

    A slice id with no recorded loss on this side is simply skipped — the
    aggregator and the gate compare whatever overlaps.
    """
    return [losses[e.id] for e in board if e.id in id_set and e.id in losses]


def _holdout_aggs(
    board: list[BoardEntry],
    parent_losses: dict[str, LossProfile],
    child_losses: dict[str, LossProfile],
    weights: ScoringWeights,
    epoch_id: str | None = None,
    child_diff_size: dict[str, int] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the holdout parent/child aggregates, or ``(None, None)``.

    Splits the board into train / holdout ids via
    :func:`zicato.board.split.split_board`. When the holdout is empty (the
    board is too small to split, or the split is disabled, or no entry is
    tagged), returns ``(None, None)`` so the gate's holdout-confirmation
    step is skipped and behaviour is byte-identical to today.

    Otherwise aggregates the parent and child loss profiles restricted to
    the holdout ids — the confirmation-only slice the proposer never sees.
    A holdout id with no recorded loss on a side is simply omitted from
    that side's aggregate (the gate compares whatever overlaps).

    ``child_diff_size`` is the opt-in parsimony / MDL input threaded ONLY into
    the CHALLENGER (child) aggregate so the diff-complexity term measures the
    challenger's diff against a champion baseline that pays no parsimony cost.
    ``None`` (the default, and any contract with ``diff_complexity_weight ==
    0.0``) leaves both aggregates byte-identical to today.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    seed = rotation_seed(weights.overfitting, epoch_id)
    _train_ids, holdout_ids = split_board(board, weights.overfitting, seed=seed)
    if not holdout_ids:
        return None, None
    holdout_set = set(holdout_ids)
    # Preserve board order in each slice (split_board already returns ids in
    # board order, but iterating the board keeps a single source of truth).
    parent_holdout = _losses_for(board, holdout_set, parent_losses)
    child_holdout = _losses_for(board, holdout_set, child_losses)
    return (
        aggregate_generation_score(parent_holdout, weights),
        aggregate_generation_score(child_holdout, weights, diff_size=child_diff_size),
    )


def _train_aggs(
    board: list[BoardEntry],
    parent_losses: dict[str, LossProfile],
    child_losses: dict[str, LossProfile],
    weights: ScoringWeights,
    epoch_id: str | None = None,
    child_diff_size: dict[str, int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the TRAIN-slice parent/child aggregates.

    The train slice drives the three gate rules and steers selection /
    standings. When the holdout is empty the train slice IS the full board,
    so these aggregates are byte-identical to the pre-split full-board
    aggregates — the back-compat invariant.

    ``child_diff_size`` is the opt-in parsimony / MDL input threaded ONLY into
    the CHALLENGER (child) aggregate (see :func:`_holdout_aggs`). ``None`` (the
    default / a ``diff_complexity_weight == 0.0`` contract) is byte-identical.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    seed = rotation_seed(weights.overfitting, epoch_id)
    train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=seed)
    train_set = set(train_ids)
    parent_train = _losses_for(board, train_set, parent_losses)
    child_train = _losses_for(board, train_set, child_losses)
    return (
        aggregate_generation_score(parent_train, weights),
        aggregate_generation_score(child_train, weights, diff_size=child_diff_size),
    )


def _load_ladder_state(workspace_root: Path, epoch_id: str, cfg: Any) -> Any:
    """Read the persisted per-epoch Ladder state, or seed a fresh one.

    Best-effort: a missing / unreadable / shape-changed state file (e.g. a
    budget bump rolled the epoch) seeds a fresh state from the config's
    budget. The Ladder state is runtime-only — it never enters the contract
    hash — so re-seeding on a parse failure is always safe.
    """
    import json  # noqa: PLC0415

    from zicato.core.workspace import ladder_state_path  # noqa: PLC0415
    from zicato.tournament.ladder import LadderState  # noqa: PLC0415

    path = ladder_state_path(workspace_root, epoch_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LadderState(
            budget_total=int(raw["budget_total"]),
            budget_remaining=int(raw["budget_remaining"]),
            best_holdout_scalar=(
                None
                if raw.get("best_holdout_scalar") is None
                else float(raw["best_holdout_scalar"])
            ),
            best_confirmed=(
                None if raw.get("best_confirmed") is None else bool(raw["best_confirmed"])
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return LadderState.seed(cfg)


def _save_ladder_state(workspace_root: Path, epoch_id: str, state: Any) -> None:
    """Persist the per-epoch Ladder state. Best-effort — never aborts a round."""
    import json  # noqa: PLC0415

    from zicato.core.workspace import ladder_state_path  # noqa: PLC0415

    path = ladder_state_path(workspace_root, epoch_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "budget_total": state.budget_total,
                    "budget_remaining": state.budget_remaining,
                    "best_holdout_scalar": state.best_holdout_scalar,
                    "best_confirmed": state.best_confirmed,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.debug("ladder: failed to persist state for epoch %s", epoch_id, exc_info=True)


def _ladder_mediated_outcome(
    *,
    train_outcome: GateOutcome,
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    holdout_parent_agg: dict[str, Any] | None,
    holdout_child_agg: dict[str, Any] | None,
    weights: ScoringWeights,
    workspace_root: Path,
    epoch_id: str,
) -> tuple[GateOutcome, dict[str, Any] | None]:
    """Apply the Ladder governor to a train-decided gate outcome.

    ``train_outcome`` is :func:`~zicato.tournament.gate.evaluate_gate` run on
    the TRAIN slice only (no holdout threaded). This function adds the
    Ladder-mediated holdout confirmation on top and returns
    ``(final_outcome, holdout_record)``:

    * **No holdout** (both holdout aggs ``None``): the holdout step is skipped
      entirely; the train outcome is returned with ``holdout=None`` — exactly
      Phase A / pre-split behaviour, byte-identical.
    * **Holdout, train rejected**: a train reject already fires with its
      specific reason; the holdout is not consulted (no budget charged) and
      no Ladder state moves. The block records ``confirmed=None``,
      ``ladder_released=False``, the current budget unchanged.
    * **Holdout, train promotes, Ladder disabled**: run the raw Phase-A
      confirmation (``holdout_confirms``) directly — every query counts, no
      budget. The block reflects that (``ladder_released`` mirrors whether the
      bit was applied, budget left at its total since nothing is charged).
    * **Holdout, train promotes, Ladder enabled**: mediate through
      :func:`zicato.tournament.ladder.query_holdout`. A *released*
      non-confirmation flips the promote to a holdout reject; a released
      confirmation (or a withheld / budget-exhausted query — "champion
      stands") leaves the train promote intact. The proposer is fed back only
      the threshold-gated bit via the journal, never the raw per-entry result.
    """
    from zicato.tournament.gate import holdout_confirms  # noqa: PLC0415
    from zicato.tournament.ladder import (  # noqa: PLC0415
        effective_threshold,
        holdout_record,
        query_holdout,
    )

    # No holdout slice to consult → byte-identical to Phase A.
    if holdout_parent_agg is None or holdout_child_agg is None:
        return train_outcome, None

    cfg = weights.overfitting.ladder
    train_parent_scalar = float(parent_agg["scalar"])
    train_child_scalar = float(child_agg["scalar"])
    holdout_scalar = float(holdout_child_agg["scalar"])
    threshold = effective_threshold(cfg, weights)

    # A train reject fires first with its specific reason; we never consult the
    # holdout (no budget charged, no state move). The block still records the
    # current budget so the dashboard can render it.
    if train_outcome.decision != "promoted":
        state = _load_ladder_state(workspace_root, epoch_id, cfg) if cfg.enabled else None
        budget_total = state.budget_total if state is not None else cfg.budget
        budget_remaining = state.budget_remaining if state is not None else cfg.budget
        block = holdout_record(
            confirmed=None,
            train_scalar=train_child_scalar,
            holdout_scalar=None,
            released=False,
            budget_total=budget_total,
            budget_remaining=budget_remaining,
            threshold=threshold,
        )
        return train_outcome, block

    # The raw Phase-A confirmation bit (computed out of band; the Ladder
    # decides whether it is released this round).
    raw_reason = holdout_confirms(holdout_parent_agg, holdout_child_agg, weights)
    raw_confirmed = not raw_reason

    # Ladder disabled → raw Phase-A confirmation: every query counts, no budget.
    if not cfg.enabled:
        if raw_reason:
            final = GateOutcome(
                decision=TournamentDecision.REJECTED,
                reason=raw_reason,
                delta_scalar=train_outcome.delta_scalar,
                delta_pass_rate=train_outcome.delta_pass_rate,
                # The per-entry regression report is an observation about the
                # TRAIN duel; flipping the verdict on the holdout does not
                # unmake it, so it travels with the rebuilt outcome.
                attributable_regressions=train_outcome.attributable_regressions,
            )
        else:
            final = train_outcome
        block = holdout_record(
            confirmed=raw_confirmed,
            train_scalar=train_child_scalar,
            holdout_scalar=holdout_scalar,
            released=True,
            budget_total=cfg.budget,
            budget_remaining=cfg.budget,
            threshold=threshold,
        )
        return final, block

    # Ladder enabled → mediate the query.
    state = _load_ladder_state(workspace_root, epoch_id, cfg)
    release = query_holdout(
        state,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=train_parent_scalar,
        train_child_scalar=train_child_scalar,
        holdout_scalar=holdout_scalar,
        holdout_confirmed=raw_confirmed,
    )
    _save_ladder_state(workspace_root, epoch_id, release.state)

    # A RELEASED non-confirmation flips the promote to a holdout reject. A
    # released confirmation, or any withheld / budget-exhausted query
    # ("champion stands" — the holdout no longer gates), leaves the train
    # promote intact.
    if release.released and not raw_confirmed:
        final = GateOutcome(
            decision=TournamentDecision.REJECTED,
            reason=raw_reason,
            delta_scalar=train_outcome.delta_scalar,
            delta_pass_rate=train_outcome.delta_pass_rate,
            attributable_regressions=train_outcome.attributable_regressions,
        )
    else:
        final = train_outcome

    block = holdout_record(
        confirmed=release.confirmed,
        train_scalar=train_child_scalar,
        holdout_scalar=release.holdout_scalar,
        released=release.released,
        budget_total=release.state.budget_total,
        budget_remaining=release.state.budget_remaining,
        threshold=release.threshold,
    )
    return final, block


def _regression_rejection(
    parent_agg: dict[str, Any],
    child_agg: dict[str, Any],
    regression: RegressionResult,
) -> GateOutcome:
    """Build the ``rejected`` :class:`GateOutcome` for a regression failure.

    The reason string is short enough to fit on one journal line:
    ``"regression suite failed: <N> tests"`` for ordinary failures or
    ``"regression suite failed: <summary>"`` for timeouts / exit-code-
    only failures. Deltas are computed from the aggregate dicts so the
    rejection record still carries the scoring evidence.
    """
    parent_scalar = float(parent_agg.get("scalar", 0.0))
    child_scalar = float(child_agg.get("scalar", 0.0))
    parent_pass = float(parent_agg.get("pass_rate", 1.0))
    child_pass = float(child_agg.get("pass_rate", 1.0))
    if regression.failed_tests:
        reason = f"regression suite failed: {len(regression.failed_tests)} tests"
    else:
        reason = f"regression suite failed: {regression.summary}"
    return GateOutcome(
        decision=TournamentDecision.REJECTED,
        reason=reason,
        delta_scalar=child_scalar - parent_scalar,
        delta_pass_rate=child_pass - parent_pass,
    )


__all__ = [
    "_holdout_aggs",
    "_ladder_mediated_outcome",
    "_load_ladder_state",
    "_losses_for",
    "_regression_rejection",
    "_save_ladder_state",
    "_train_aggs",
]
