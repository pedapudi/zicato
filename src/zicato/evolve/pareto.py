"""Round-pipeline **frontier** stage — record the Pareto frontier at settle.

The thin wiring between the two evolve pipelines and the per-epoch record in
:mod:`zicato.epoch.pareto`. One call per settled round, placed after the
round's decision is final (post holdout confirmation, post integrity block,
post operator override) so the record is always evaluated against the
champion the round actually ended with.

**Best-effort by contract.** Recording an observation must never fail a
round: the canonical stores stay authoritative and every exception here is
swallowed, exactly the discipline the live index dual-write and the RoundLog
emitter established. Swallowed is not the same as silent — see
:func:`_log_skip`: a transient defect is ``debug`` like those precedents,
but an unreadable canonical record is warned once per epoch, because unlike
a projection it has no rebuild path to quietly fix it.

The record itself changes nothing about the loop — see
``docs/design/PARETO-FRONTIER.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
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

    Surfaces exactly one INFO line and one ``frontier_updated`` round-log
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


#: Epochs already warned about an unreadable record. A malformed file does not
#: heal itself, so without this the same warning fires every remaining round of
#: the epoch and trains the operator to ignore it.
_WARNED_EPOCHS: set[str] = set()


def _log_skip(epoch_id: str, exc: Exception) -> None:
    """Log a swallowed recorder failure at the level its cause deserves.

    A transient defect — a busy index, a failed round-log emit — is noise at
    ``debug``, exactly like the dual-write and RoundLog precedents this
    module follows.

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


__all__ = ["record_round_frontier"]
