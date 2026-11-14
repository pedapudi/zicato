"""Round-pipeline **frontier** stage — record the Pareto frontier at settle.

The thin wiring between the two evolve pipelines and the per-epoch record in
:mod:`zicato.epoch.pareto`. One call per settled round, placed after the
round's decision is final (post holdout confirmation, post integrity block,
post operator override) so the record is always evaluated against the
champion the round actually ended with.

**Best-effort by contract.** Recording an observation must never fail a
round: the canonical stores stay authoritative and every exception here is
swallowed, the same discipline the live index dual-write and the round-log
emitter follow. Swallowed is not the same as silent — see
:func:`_log_skip`: a transient defect is ``debug`` like those precedents,
but an unreadable canonical record is warned once per epoch, because unlike
a projection it has no rebuild path to quietly fix it.

The record decides nothing about the loop — see
``docs/design/PARETO-FRONTIER.md``. :func:`confirm_frontier_member` is the
one step here that runs board units: it re-measures a member against the
current champion before a caller uses it, and reports what that cost.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

log = logging.getLogger("zicato.orchestrator")


def _summarize(update: Any) -> str:
    """Render one update's movements for the operator-facing INFO line."""
    parts: list[str] = []
    if update.admitted:
        parts.append("admitted " + ", ".join(update.admitted))
    if update.retired:
        reasons = {
            entry.member.generation_id: entry.reason
            for entry in update.frontier.retired
            if entry.round_retired == update.frontier.updated_round
        }
        parts.append(
            "retired "
            + ", ".join(
                f"{gid} ({reasons[gid]})" if gid in reasons else gid for gid in update.retired
            )
        )
    return "; ".join(parts)


def record_round_frontier(
    *,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    weights: Any,
    champion_generation_id: str,
    aggregates: Mapping[str, Mapping[str, Any]],
    placebo_generation_ids: Collection[str] = (),
    round_log: Any | None = None,
) -> None:
    """Fold one settled round into the epoch's Pareto frontier record.

    ``aggregates`` maps generation id → the ``aggregate_generation_score``
    output the gate decided on, for every generation the round scored —
    champion included. ``champion_generation_id`` names which of them is the
    champion AFTER the decision (the promoted generation on a promote, the
    incumbent otherwise); every other entry is offered as a candidate.

    ``placebo_generation_ids`` names the round's random-baseline arms. The
    multi-challenger path fields the placebo inside the slate, so this is
    load-bearing: a no-op re-emission of the champion would otherwise sit on
    the record forever as a permanent tie.

    Surfaces one INFO line and one ``frontier_updated`` round-log
    event, and only when membership actually moved. A round that changes
    nothing is silent and leaves the record file untouched.
    """
    try:
        from zicato.epoch.pareto import FrontierCandidate, record_frontier  # noqa: PLC0415

        champion_agg = aggregates.get(champion_generation_id)
        if champion_agg is None:
            # No aggregate for the champion means nothing to compare against;
            # a record with no reference point would be meaningless.
            return
        placebos = set(placebo_generation_ids)
        if champion_generation_id in placebos:
            # The gate crowned the random-baseline arm. A placebo is a no-op
            # copy of the champion, so it is numerically the champion under a
            # different id: every admission this round would attribute its
            # provenance to a generation that exists only to test the gate.
            # The round is already raising the CRITICAL ``placebo_promoted``
            # health finding; the record stays out of it (``update_frontier``
            # refuses the same case, so this is the legible half of one rule).
            log.debug(
                "frontier: epoch %s round %d — skipped, the crowned champion "
                "%s is the random-baseline placebo arm",
                epoch_id,
                round_index,
                champion_generation_id,
            )
            return
        champion = FrontierCandidate(
            generation_id=champion_generation_id,
            aggregate=champion_agg,
            is_placebo=champion_generation_id in placebos,
        )
        candidates = [
            FrontierCandidate(
                generation_id=gid,
                aggregate=agg,
                is_placebo=gid in placebos,
            )
            for gid, agg in aggregates.items()
            if gid != champion_generation_id
        ]
        update = record_frontier(
            workspace_root,
            epoch_id,
            champion=champion,
            candidates=candidates,
            weights=weights,
            round_index=round_index,
        )
        if not update.changed:
            return
        log.info(
            "frontier: epoch %s round %d — %s; size %d",
            epoch_id,
            round_index,
            _summarize(update),
            len(update.frontier.members),
        )
        if round_log is not None:
            round_log.emit(
                "frontier_updated",
                {
                    "admitted": tuple(update.admitted),
                    "retired": tuple(update.retired),
                    "size": len(update.frontier.members),
                },
            )
        _ingest_frontier_into_index(workspace_root, epoch_id)
    except Exception as exc:  # noqa: BLE001 — a record must never fail a round
        _log_skip(epoch_id, exc)


def frontier_aggregates(result: Any, weights: Any) -> dict[str, dict[str, Any]]:
    """One duel's two aggregates by generation id, each with its dimensions.

    The aggregates are the ones the gate decided on. The per-dimension
    contributions the frontier's finer axes read are computed from the same
    train-slice loss profiles and added under ``dimension_aggregates``; they
    are omitted for a side whose profiles do not cover exactly the entries
    its aggregate scored, and on any failure, so the namespace axes still
    stand. ``gen_score.json`` is written from the plain aggregate.
    """
    from zicato.epoch.pareto import DIMENSION_AGGREGATES_KEY  # noqa: PLC0415
    from zicato.tournament.scoring import aggregate_dimension_metrics  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    sides = (
        (result.parent_generation_id, result.parent_agg),
        (result.child_generation_id, result.child_agg),
    )
    for index, (generation_id, aggregate) in enumerate(sides):
        out[generation_id] = aggregate
        try:
            if set(aggregate.get("per_entry", {})) != set(result.per_entry_losses):
                continue
            losses = [pair[index] for pair in result.per_entry_losses.values()]
            dimensions = aggregate_dimension_metrics(losses, weights)
        except Exception as exc:  # noqa: BLE001 — a finer axis must never fail a round
            log.debug("frontier dimensions skipped for %s: %s", generation_id, exc)
            continue
        if dimensions:
            out[generation_id] = {**aggregate, DIMENSION_AGGREGATES_KEY: dimensions}
    return out


async def confirm_frontier_member(
    *,
    champion: Any,
    member: Any,
    board: Sequence[Any],
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    round_log: Any | None = None,
    **matchup: Any,
) -> Any | None:
    """Re-measure a frontier member against the current champion before use.

    ``champion`` and ``member`` are :class:`~zicato.core.Generation` records;
    ``board`` is the epoch's FULL board, which is split here so that only
    TRAIN entries are ever run — the holdout slice never reaches the record.
    ``matchup`` is the rest of what
    :func:`~zicato.tournament.runner.run_matchup` needs: ``adapter`` and
    ``config``, and optionally ``writer``, ``disable_drift``, ``judge_only``,
    and ``unit_semaphore``.

    Both generations are measured at draw 0 of the ``evidence_confirmation``
    purpose, cache-first, through the same board-unit runner as every duel.
    That purpose is separate from the tournament draws that put the member on
    the record, and the unit cache is keyed by generation, so each generation
    is paid for once per epoch: the first confirmation of a member runs the
    member's train units, plus the champion's unless the evidence gate has
    already measured that champion at this draw, and a confirmation against a
    later champion runs only the new champion's.

    :func:`~zicato.epoch.pareto.confirm_member` then refreshes the member
    against this champion or retires it as ``unconfirmed``, and the record is
    saved. An incomplete measurement leaves the record as it was. The INFO
    line and the ``frontier_updated`` round-log event state the board units
    run and the board units reused, whatever the result.

    Returns the refreshed :class:`~zicato.epoch.pareto.FrontierMember`, or
    ``None`` when the caller must not use this generation: it is not a
    member, it was retired, the measurement was incomplete, or the
    confirmation failed. A member already confirmed against this champion is
    returned without running anything.
    """
    try:
        from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
        from zicato.core.measurement import MeasurementDraw, MeasurementPurpose  # noqa: PLC0415
        from zicato.epoch.pareto import (  # noqa: PLC0415
            FrontierCandidate,
            FrontierUpdate,
            confirm_member,
            load_frontier,
            save_frontier,
        )
        from zicato.tournament.runner import run_matchup  # noqa: PLC0415

        frontier = load_frontier(workspace_root, epoch_id)
        current = next((m for m in frontier.members if m.generation_id == member.id), None)
        if current is None:
            return None
        if current.confirmed_round is not None and current.champion_generation_id == champion.id:
            return current
        train_ids, _holdout_ids = split_board(
            board, weights.overfitting, seed=rotation_seed(weights.overfitting, epoch_id)
        )
        result = await run_matchup(
            left_gen=champion,
            right_gen=member,
            board=list(board),
            weights=weights,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            board_subset=tuple(train_ids),
            first_measurement=MeasurementDraw(MeasurementPurpose.CONFIRMATION, 0),
            fast=True,
            round_index=round_index,
            match_id=f"frontier-confirm:{champion.id}:{member.id}",
            **matchup,
        )
        update = FrontierUpdate(frontier)
        if result.measurement_draw is not None:
            aggregates = frontier_aggregates(result, weights)
            update = confirm_member(
                frontier,
                champion=FrontierCandidate(champion.id, aggregates[champion.id]),
                member=FrontierCandidate(member.id, aggregates[member.id]),
                weights=weights,
                round_index=round_index,
            )
            save_frontier(workspace_root, epoch_id, update.frontier)
            _ingest_frontier_into_index(workspace_root, epoch_id)
        fresh = sum(p.fresh for p in result.unit_provenance.values())
        cached = sum(p.cached for p in result.unit_provenance.values())
        log.info(
            "frontier: epoch %s round %d — %s against %s: %s; %d board unit(s) run, %d reused",
            epoch_id,
            round_index,
            member.id,
            champion.id,
            "confirmed"
            if update.confirmed
            else "retired (unconfirmed)"
            if update.retired
            else "measurement incomplete, record unchanged",
            fresh,
            cached,
        )
        if round_log is not None:
            round_log.emit(
                "frontier_updated",
                {
                    "retired": update.retired,
                    "confirmed": update.confirmed,
                    "size": len(update.frontier.members),
                    "units_fresh": fresh,
                    "units_cached": cached,
                },
            )
        return next(
            (
                m
                for m in update.frontier.members
                if m.generation_id == member.id and update.confirmed
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — an unconfirmed member is simply not used
        _log_skip(epoch_id, exc)
        return None


def frontier_confirmer(
    *, champion: Any, workspace_root: Path, epoch_id: str, **options: Any
) -> Callable[[str], Awaitable[Any]]:
    """Bind :func:`confirm_frontier_member` to one round, by generation id.

    The returned callable materializes the member's source tree from the
    generation store and confirms it against ``champion``. A member whose
    tree the store cannot supply is not usable, and costs nothing.
    """

    async def _confirm(generation_id: str) -> Any | None:
        from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

        try:
            store = default_generation_store(workspace_root)
            snapshot = store.materialize_snapshot(epoch_id, generation_id)
        except (FileNotFoundError, ValueError) as exc:
            log.debug("frontier: %s has no source tree to confirm (%s)", generation_id, exc)
            return None
        member = replace(champion, id=generation_id, snapshot_root=snapshot, promoted=False)
        return await confirm_frontier_member(
            champion=champion,
            member=member,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            **options,
        )

    return _confirm


def unimproved_dimensions(
    workspace_root: Path, epoch_id: str, champion_id: str, dimensions: Sequence[str]
) -> tuple[str, ...]:
    """Of ``dimensions``, those no CONFIRMED frontier member improves.

    Only members a fresh train measurement confirmed against ``champion_id``
    count; a member still resting on the draws that selected it does not.
    A dimension is improved when a member beats the champion on it, or on
    its namespace where that member carries no separate axis for it. Empty
    when no member is confirmed against this champion — with nothing
    measured there is nothing to report — and on any read failure.
    """
    try:
        from zicato.epoch.pareto import load_frontier  # noqa: PLC0415

        members = [
            m
            for m in load_frontier(workspace_root, epoch_id).members
            if m.confirmed_round is not None and m.champion_generation_id == champion_id
        ]
    except Exception as exc:  # noqa: BLE001 — steering must never fail a round
        _log_skip(epoch_id, exc)
        return ()

    def improved(name: str) -> bool:
        namespace = name.partition(":")[0] + ":"
        return any(
            name in m.beats_champion_on
            or (namespace in m.beats_champion_on and name not in m.axis_values)
            for m in members
        )

    return tuple(name for name in dimensions if members and not improved(name))


#: Epochs already warned about an unreadable record. A malformed file does not
#: heal itself, so without this the same warning fires every remaining round of
#: the epoch and trains the operator to ignore it.
_WARNED_EPOCHS: set[str] = set()


def _log_skip(epoch_id: str, exc: Exception) -> None:
    """Log a swallowed recorder failure at the level its cause deserves.

    A transient defect — a busy index, a failed round-log emit — is noise at
    ``debug``, like the dual-write and round-log emitters this module follows.

    A MALFORMED CANONICAL RECORD is not. The precedents swallow silently
    because what they write is a projection that the next rebuild re-derives;
    ``pareto_frontier.json`` IS the canonical copy and has no rebuild path, so
    a silent skip means the epoch quietly stops recording and nothing ever
    says so. Warned once per epoch, naming the epoch, so the operator can
    delete or repair the file.
    """
    import json  # noqa: PLC0415

    from zicato.epoch._storage import RecordFormatError  # noqa: PLC0415

    if isinstance(exc, json.JSONDecodeError | RecordFormatError):
        if epoch_id not in _WARNED_EPOCHS:
            _WARNED_EPOCHS.add(epoch_id)
            log.warning(
                "frontier: epoch %s — the record is unreadable, so this epoch "
                "records no further frontier movement until it is repaired or "
                "removed (%s). The loop is unaffected: the record is an "
                "observation, never a decision.",
                epoch_id,
                exc,
            )
        return
    log.debug("pareto frontier record skipped: %s", exc)


def _ingest_frontier_into_index(workspace_root: Path, epoch_id: str) -> None:
    """Best-effort live index dual-write of the epoch's frontier projection.

    The workspace file is canonical and the index table is a pure projection
    of it — ``zicato repair index`` re-derives every row — so a failure here costs
    a stale table until the next rebuild, never any evidence.
    """
    try:
        from zicato.index.ingest import ingest_pareto_frontier  # noqa: PLC0415

        ingest_pareto_frontier(workspace_root, None, epoch_id)
    except Exception as exc:  # noqa: BLE001 — the index is derived, never canonical
        log.debug("pareto frontier index ingest skipped: %s", exc)


__all__ = [
    "confirm_frontier_member",
    "frontier_aggregates",
    "frontier_confirmer",
    "record_round_frontier",
    "unimproved_dimensions",
]
