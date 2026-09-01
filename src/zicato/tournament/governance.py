"""Tournament governance: promote-gate decisions and durable Ladder state.

The helpers take already-computed aggregates and produce
:class:`~zicato.tournament.gate.GateOutcome` decisions plus the holdout
evidence block. The Ladder store atomically reserves and settles the finite
query budget. Nothing here spawns a worker, touches the unit cache, or
schedules a board unit.

The runner imports the governance helpers used at its scheduling boundary.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, TypeGuard

from zicato.core import ScoringWeights, TournamentDecision
from zicato.core.types import LadderConfig
from zicato.core.workspace import ladder_state_path
from zicato.storage import atomic_write_json, read_json
from zicato.tournament.gate import GateOutcome
from zicato.tournament.ladder import (
    LadderRelease,
    LadderState,
    decide_reserved_holdout,
    effective_threshold,
    holdout_record,
    reserve_holdout_query,
)
from zicato.tournament.regression import RegressionResult

_LADDER_STATE_FORMAT = 2
_ladder_locks_guard = threading.Lock()
_ladder_locks: dict[Path, threading.Lock] = {}


class LadderStateError(RuntimeError):
    """The durable Ladder query budget could not be trusted or updated."""


@dataclass(frozen=True, slots=True)
class LadderQueryReservation:
    """Identity of one durable, unsettled holdout-query charge."""

    state_path: str
    state_id: str
    epoch_id: str
    reservation_id: str


@dataclass(frozen=True, slots=True)
class _PendingLadderReservation:
    """Durable audit facts for one charged, unsettled query."""

    reservation_id: str
    budget_before_query: int


@dataclass(frozen=True, slots=True)
class _DurableLadderState:
    """Statistical state plus the identities of charged, unsettled queries."""

    state_id: str
    state: LadderState
    pending_reservations: tuple[_PendingLadderReservation, ...] = ()


def _cross_process_lock_module() -> ModuleType:
    """Load the required advisory-lock primitive or fail closed."""
    try:
        import fcntl  # noqa: PLC0415
    except ImportError as exc:
        raise LadderStateError(
            "durable Ladder reservations require cross-process file locking"
        ) from exc
    return fcntl


@contextmanager
def _ladder_state_lock(path: Path) -> Iterator[None]:
    """Serialize one epoch's Ladder state within and across processes.

    The workspace lock already prevents two production orchestrators from
    writing one workspace.  The advisory file lock also protects direct
    library callers. The process-local lock protects threads. A platform
    without ``fcntl`` fails closed because it cannot provide the promised
    cross-process serialization.
    """
    with _ladder_locks_guard:
        local_lock = _ladder_locks.setdefault(path, threading.Lock())
    with local_lock:
        fcntl_module = _cross_process_lock_module()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise LadderStateError(f"cannot lock Ladder state {path}: {exc}") from exc
        try:
            fcntl_module.flock(fd, fcntl_module.LOCK_EX)
            yield
        except OSError as exc:
            raise LadderStateError(f"cannot lock Ladder state {path}: {exc}") from exc
        finally:
            try:
                fcntl_module.flock(fd, fcntl_module.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _ladder_marker_path(path: Path) -> Path:
    return path.with_name("ladder_state.initialized.json")


def _new_identity() -> str:
    """Return an opaque identity used only to bind durable reservations."""
    return uuid.uuid4().hex


def _valid_identity(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        return uuid.UUID(hex=value).hex == value
    except ValueError:
        return False


def _ladder_state_dict(record: _DurableLadderState) -> dict[str, object]:
    state = record.state
    return {
        "format_version": _LADDER_STATE_FORMAT,
        "state_id": record.state_id,
        "budget_total": state.budget_total,
        "budget_remaining": state.budget_remaining,
        "best_holdout_scalar": state.best_holdout_scalar,
        "best_confirmed": state.best_confirmed,
        "pending_reservations": [
            {
                "reservation_id": pending.reservation_id,
                "budget_before_query": pending.budget_before_query,
            }
            for pending in record.pending_reservations
        ],
    }


def _decode_ladder_state(raw: object, cfg: LadderConfig, path: Path) -> _DurableLadderState:
    expected = {
        "format_version",
        "state_id",
        "budget_total",
        "budget_remaining",
        "best_holdout_scalar",
        "best_confirmed",
        "pending_reservations",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise LadderStateError(f"Ladder state {path} has an unsupported shape")
    version = raw["format_version"]
    state_id = raw["state_id"]
    budget_total = raw["budget_total"]
    budget_remaining = raw["budget_remaining"]
    best_scalar = raw["best_holdout_scalar"]
    best_confirmed = raw["best_confirmed"]
    pending_raw = raw["pending_reservations"]
    if (
        type(version) is not int
        or version != _LADDER_STATE_FORMAT
        or not _valid_identity(state_id)
        or type(budget_total) is not int
        or budget_total != cfg.budget
        or type(budget_remaining) is not int
        or not 0 <= budget_remaining <= budget_total
        or not (
            best_scalar is None
            or (type(best_scalar) in (int, float) and math.isfinite(best_scalar))
        )
        or not (best_confirmed is None or type(best_confirmed) is bool)
        or ((best_scalar is None) != (best_confirmed is None))
        or not isinstance(pending_raw, list)
    ):
        raise LadderStateError(f"Ladder state {path} has invalid or mismatched values")
    pending: list[_PendingLadderReservation] = []
    for item in pending_raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"reservation_id", "budget_before_query"}
            or not _valid_identity(item["reservation_id"])
            or type(item["budget_before_query"]) is not int
            or not budget_remaining < item["budget_before_query"] <= budget_total
        ):
            raise LadderStateError(f"Ladder state {path} has invalid or mismatched values")
        pending.append(
            _PendingLadderReservation(
                reservation_id=item["reservation_id"],
                budget_before_query=item["budget_before_query"],
            )
        )
    reservation_ids = [item.reservation_id for item in pending]
    budget_positions = [item.budget_before_query for item in pending]
    if (
        len(set(reservation_ids)) != len(reservation_ids)
        or len(set(budget_positions)) != len(budget_positions)
        or len(pending) > budget_total - budget_remaining
    ):
        raise LadderStateError(f"Ladder state {path} has invalid or mismatched values")
    return _DurableLadderState(
        state_id=state_id,
        state=LadderState(
            budget_total=budget_total,
            budget_remaining=budget_remaining,
            best_holdout_scalar=None if best_scalar is None else float(best_scalar),
            best_confirmed=best_confirmed,
        ),
        pending_reservations=tuple(pending),
    )


def _write_ladder_state(path: Path, record: _DurableLadderState) -> None:
    try:
        atomic_write_json(path, _ladder_state_dict(record))
    except (OSError, TypeError, ValueError) as exc:
        raise LadderStateError(f"cannot persist Ladder state {path}: {exc}") from exc


def _write_ladder_marker(path: Path, state_id: str) -> None:
    try:
        atomic_write_json(
            _ladder_marker_path(path),
            {"format_version": _LADDER_STATE_FORMAT, "state_id": state_id},
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LadderStateError(f"cannot mark Ladder state initialized at {path}: {exc}") from exc


def _load_ladder_state_locked(path: Path, cfg: LadderConfig) -> _DurableLadderState:
    """Load strictly, atomically initializing only a never-used epoch."""
    marker_path = _ladder_marker_path(path)
    try:
        raw = read_json(path)
        marker = read_json(marker_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise LadderStateError(f"cannot read Ladder state {path}: {exc}") from exc

    if raw is None:
        if marker is not None:
            raise LadderStateError(
                f"Ladder state {path} is missing after the epoch budget was initialized"
            )
        record = _DurableLadderState(state_id=_new_identity(), state=LadderState.seed(cfg))
        _write_ladder_state(path, record)
        _write_ladder_marker(path, record.state_id)
        return record

    record = _decode_ladder_state(raw, cfg, path)
    if marker is None:
        # Completing the second half of initialization is safe: the state was
        # already atomically published, so no capacity can be restored here.
        _write_ladder_marker(path, record.state_id)
    elif marker != {
        "format_version": _LADDER_STATE_FORMAT,
        "state_id": record.state_id,
    }:
        raise LadderStateError(f"Ladder initialization marker {marker_path} is malformed")
    return record


def _load_ladder_state(workspace_root: Path, epoch_id: str, cfg: LadderConfig) -> LadderState:
    """Strictly read or initialize the epoch-local Ladder state."""
    path = ladder_state_path(workspace_root, epoch_id)
    with _ladder_state_lock(path):
        return _load_ladder_state_locked(path, cfg).state


def _reserve_ladder_query(
    workspace_root: Path, epoch_id: str, cfg: LadderConfig
) -> tuple[LadderState, LadderQueryReservation | None]:
    """Durably charge one query before holdout work can start."""
    path = ladder_state_path(workspace_root, epoch_id)
    with _ladder_state_lock(path):
        record = _load_ladder_state_locked(path, cfg)
        charged = reserve_holdout_query(record.state)
        if charged is None:
            return record.state, None
        reservation_id = _new_identity()
        charged_record = _DurableLadderState(
            state_id=record.state_id,
            state=charged,
            pending_reservations=(
                *record.pending_reservations,
                _PendingLadderReservation(
                    reservation_id=reservation_id,
                    budget_before_query=record.state.budget_remaining,
                ),
            ),
        )
        _write_ladder_state(path, charged_record)
        return charged, LadderQueryReservation(
            state_path=str(path.resolve()),
            state_id=record.state_id,
            epoch_id=epoch_id,
            reservation_id=reservation_id,
        )


def _settle_ladder_query(
    *,
    workspace_root: Path,
    epoch_id: str,
    reservation: LadderQueryReservation,
    cfg: LadderConfig,
    weights: ScoringWeights,
    train_parent_scalar: float,
    train_child_scalar: float,
    holdout_scalar: float,
    holdout_confirmed: bool,
) -> tuple[LadderRelease, int]:
    """Publish a reserved query's release decision without charging twice."""
    path = ladder_state_path(workspace_root, epoch_id)
    if reservation.epoch_id != epoch_id or reservation.state_path != str(path.resolve()):
        raise LadderStateError(f"Ladder reservation no longer matches {path}")
    with _ladder_state_lock(path):
        record = _load_ladder_state_locked(path, cfg)
        if record.state_id != reservation.state_id:
            raise LadderStateError(f"Ladder reservation no longer matches {path}")
        pending = next(
            (
                item
                for item in record.pending_reservations
                if item.reservation_id == reservation.reservation_id
            ),
            None,
        )
        if pending is None:
            raise LadderStateError(
                f"Ladder reservation was already settled or is unknown at {path}"
            )
        release = decide_reserved_holdout(
            record.state,
            cfg=cfg,
            weights=weights,
            train_parent_scalar=train_parent_scalar,
            train_child_scalar=train_child_scalar,
            holdout_scalar=holdout_scalar,
            holdout_confirmed=holdout_confirmed,
        )
        remaining_pending = tuple(
            item
            for item in record.pending_reservations
            if item.reservation_id != reservation.reservation_id
        )
        _write_ladder_state(
            path,
            _DurableLadderState(
                state_id=record.state_id,
                state=release.state,
                pending_reservations=remaining_pending,
            ),
        )
        return release, pending.budget_before_query


def _ladder_exhausted_outcome(
    *,
    train_outcome: GateOutcome,
    train_child_agg: dict[str, Any],
    state: LadderState,
    weights: ScoringWeights,
) -> tuple[GateOutcome, dict[str, Any]]:
    """Return the train decision when no further holdout query is affordable."""
    return train_outcome, holdout_record(
        confirmed=None,
        train_scalar=float(train_child_agg["scalar"]),
        holdout_scalar=None,
        consulted=False,
        released=False,
        budget_total=state.budget_total,
        budget_before_query=state.budget_remaining,
        budget_remaining=state.budget_remaining,
        query_reserved=False,
        threshold=effective_threshold(weights.overfitting.ladder, weights),
    )


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
    reservation: LadderQueryReservation | None = None,
) -> tuple[GateOutcome, dict[str, Any] | None]:
    """Apply the Ladder governor to a train-decided gate outcome.

    ``train_outcome`` is :func:`~zicato.tournament.gate.evaluate_gate` run on
    the TRAIN slice only (no holdout threaded). This function adds the
    Ladder-mediated holdout confirmation on top and returns
    ``(final_outcome, holdout_record)``:

    * **No holdout** (both holdout aggs ``None``): the holdout step is skipped
      entirely; the train outcome is returned with ``holdout=None``, which is
      the decision the train rules alone reach.
    * **Holdout, train rejected**: this is invalid because production callers
      must reject before they execute the holdout.
    * **Holdout, train promotes, Ladder disabled**: run the raw Phase-A
      confirmation (``holdout_confirms``) directly — every query counts, no
      budget. The block reflects that (``ladder_released`` mirrors whether the
      bit was applied, budget left at its total since nothing is charged).
    * **Holdout, train promotes, Ladder enabled**: require the caller's prior
      durable reservation, then settle the release decision. A released
      non-confirmation flips the promotion to a holdout rejection; a released
      confirmation or withheld query leaves the training decision intact.
      Exhaustion is handled before the matchup starts. The proposer receives
      only the threshold-gated bit, never the raw per-entry result.
    """
    from zicato.tournament.gate import holdout_confirms  # noqa: PLC0415

    # No holdout slice to consult → the train rules decide alone.
    if holdout_parent_agg is None or holdout_child_agg is None:
        return train_outcome, None

    cfg = weights.overfitting.ladder
    train_parent_scalar = float(parent_agg["scalar"])
    train_child_scalar = float(child_agg["scalar"])
    threshold = effective_threshold(cfg, weights)

    # A train reject must stop scheduling before any holdout evidence exists.
    # Refuse an observed aggregate even if a caller reserved unnecessarily.
    if train_outcome.decision != "promoted":
        raise LadderStateError(
            "holdout evidence was observed after the training gate rejected the challenger"
        )

    holdout_scalar = float(holdout_child_agg["scalar"])

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
            consulted=True,
            released=True,
            budget_total=cfg.budget,
            budget_before_query=None,
            budget_remaining=cfg.budget,
            query_reserved=False,
            threshold=threshold,
        )
        return final, block

    # Ladder-enabled evidence is valid only after the scheduling boundary
    # durably reserved its query.  Refuse an unreserved aggregate rather than
    # trying to charge after the evidence has already been observed.
    if reservation is None:
        raise LadderStateError("holdout evidence was observed without a durable reservation")
    release, budget_before_query = _settle_ladder_query(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        reservation=reservation,
        cfg=cfg,
        weights=weights,
        train_parent_scalar=train_parent_scalar,
        train_child_scalar=train_child_scalar,
        holdout_scalar=holdout_scalar,
        holdout_confirmed=raw_confirmed,
    )

    # A released non-confirmation flips the promote to a holdout reject. A
    # released confirmation or a withheld query leaves the train promote
    # intact. Budget exhaustion is handled before holdout execution and never
    # reaches this decision function.
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
        consulted=True,
        released=release.released,
        budget_total=release.state.budget_total,
        budget_before_query=budget_before_query,
        budget_remaining=release.state.budget_remaining,
        query_reserved=True,
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
    "LadderQueryReservation",
    "LadderStateError",
    "_ladder_exhausted_outcome",
    "_ladder_mediated_outcome",
    "_load_ladder_state",
    "_regression_rejection",
    "_reserve_ladder_query",
]
