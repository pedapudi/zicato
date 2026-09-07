"""Ordered, idempotent publication and recovery of accepted settlement receipts."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zicato.epoch.journal import (
    append_journal_entry_once,
    read_experiment,
    update_experiment_outcome,
)
from zicato.epoch.lineage import resolve_pending_generations
from zicato.epoch.settlement_receipt import (
    HookDeliveryState,
    SettlementReceipt,
    decode_settlement_receipt,
    field_settlement_intent_key,
    iter_settlement_receipts,
    lineage_resolutions,
    read_settlement_receipt,
    validate_workspace_settlement,
    write_settlement_receipt,
)
from zicato.evolve import generation_phase
from zicato.runtime.lock import WorkspaceLock
from zicato.tournament.records import decode_field_tournament_record, write_field_tournament_record

log = logging.getLogger("zicato.orchestrator")
CrashCheckpoint = Callable[[str], None]


def commit_field_settlement(
    workspace_root: Path,
    intent: dict[str, Any],
    *,
    crash_checkpoint: CrashCheckpoint | None = None,
) -> None:
    """Persist a new decision or idempotently finish the same receipt."""
    validated, _parent_id = validate_workspace_settlement(workspace_root, intent)
    if intent.get("state") != "pending":
        raise RuntimeError("a new field settlement must start in pending state")
    key = field_settlement_intent_key(validated.epoch_id, validated.round_index)
    recorded = read_settlement_receipt(workspace_root, validated.epoch_id, validated.round_index)
    if recorded is not None:
        existing = recorded.to_dict()
        validate_workspace_settlement(
            workspace_root,
            existing,
            expected_epoch_id=validated.epoch_id,
            expected_round_index=validated.round_index,
        )
        if decode_settlement_receipt(existing).immutable_facts() != validated.immutable_facts():
            raise RuntimeError(
                f"field settlement receipt {key!r} conflicts with the recorded decision"
            )
        if existing["state"] == "pending":
            replay_field_settlement(
                workspace_root,
                existing,
                expected_epoch_id=validated.epoch_id,
                expected_round_index=validated.round_index,
                crash_checkpoint=crash_checkpoint,
            )
        return
    write_settlement_receipt(workspace_root, validated)
    _checkpoint(crash_checkpoint, "receipt_persisted")
    replay_field_settlement(
        workspace_root,
        intent,
        expected_epoch_id=validated.epoch_id,
        expected_round_index=validated.round_index,
        crash_checkpoint=crash_checkpoint,
    )


def replay_field_settlement(
    workspace_root: Path,
    intent: dict[str, Any],
    *,
    expected_epoch_id: str | None = None,
    expected_round_index: int | None = None,
    crash_checkpoint: CrashCheckpoint | None = None,
) -> None:
    """Idempotently complete every canonical write in a pending receipt."""
    settlement, parent_id = validate_workspace_settlement(
        workspace_root,
        intent,
        expected_epoch_id=expected_epoch_id,
        expected_round_index=expected_round_index,
    )
    if intent["state"] == "committed":
        return

    receipt = copy.deepcopy(intent)
    finalised: dict[str, Any] = {}
    for candidate in settlement.candidates:
        generation_id = candidate.generation_id
        outcome = candidate.outcome
        experiment = read_experiment(workspace_root, settlement.epoch_id, generation_id)
        if experiment.outcome is None:
            experiment = update_experiment_outcome(
                workspace_root,
                settlement.epoch_id,
                generation_id,
                outcome,
            )
        finalised[generation_id] = experiment
        _checkpoint(crash_checkpoint, f"outcome:{generation_id}")

    resolve_pending_generations(
        workspace_root,
        settlement.epoch_id,
        lineage_resolutions(parent_id, settlement),
    )
    _checkpoint(crash_checkpoint, "lineage")

    if settlement.primary_id is not None:
        generation_phase.set_current_generation(
            workspace_root,
            settlement.epoch_id,
            settlement.primary_id,
        )
        if (
            generation_phase.current_generation(workspace_root, settlement.epoch_id)
            != settlement.primary_id
        ):
            raise RuntimeError(
                "crowning invariant violated: current_generation did not advance to "
                f"{settlement.primary_id!r}"
            )
        _checkpoint(crash_checkpoint, "champion_marker")

    for candidate in settlement.candidates:
        generation_id = candidate.generation_id
        append_journal_entry_once(
            workspace_root,
            settlement.epoch_id,
            finalised[generation_id],
            settlement_identity=f"{settlement.settlement_id}:{generation_id}",
        )
        _checkpoint(crash_checkpoint, f"journal:{generation_id}")

    if settlement.field_record is not None:
        write_field_tournament_record(
            workspace_root,
            epoch_id=settlement.epoch_id,
            first_challenger_id=settlement.first_challenger_id,
            record=decode_field_tournament_record(settlement.field_record),
        )
        _checkpoint(crash_checkpoint, "settled_bracket")

    if receipt["index_projection"]["state"] == "pending":
        _project_settlement_index(workspace_root, settlement, receipt)
        write_settlement_receipt(workspace_root, decode_settlement_receipt(receipt))
        _checkpoint(crash_checkpoint, "index_projection")

    receipt["state"] = "committed"
    write_settlement_receipt(workspace_root, decode_settlement_receipt(receipt))
    _checkpoint(crash_checkpoint, "receipt_committed")


def recover_field_settlements(
    workspace_root: Path, epoch_id: str, *, writer: WorkspaceLock | None = None
) -> int:
    """Complete every pending receipt for ``epoch_id`` under its workspace writer."""
    from zicato.runtime.lock import acquire_workspace_lock, validate_workspace_lock  # noqa: PLC0415

    if writer is None:
        with acquire_workspace_lock(workspace_root, "settlement-recovery") as owned_writer:
            return recover_field_settlements(workspace_root, epoch_id, writer=owned_writer)
    validate_workspace_lock(writer, workspace_root)
    if not epoch_id:
        return 0
    recovered = 0
    for stored in iter_settlement_receipts(workspace_root, epoch_id):
        raw = stored.to_dict()
        settlement, _parent_id = validate_workspace_settlement(
            workspace_root,
            raw,
            expected_epoch_id=epoch_id,
            expected_round_index=stored.round_index,
        )
        if raw["state"] == "pending":
            replay_field_settlement(
                workspace_root,
                raw,
                expected_epoch_id=settlement.epoch_id,
                expected_round_index=settlement.round_index,
            )
            recovered += 1
            committed = read_settlement_receipt(workspace_root, epoch_id, settlement.round_index)
            if committed is None:
                raise RuntimeError(
                    f"replayed field settlement {settlement.settlement_id!r} is missing"
                )
            raw = committed.to_dict()
        if raw["promotion_hook"]["state"] == "pending":
            updated = copy.deepcopy(raw)
            updated["promotion_hook"]["state"] = "delivery_unknown"
            write_settlement_receipt(workspace_root, decode_settlement_receipt(updated))
            log.warning(
                "field settlement %s has an unknown promotion-hook delivery after "
                "restart; zicato will not retry it, so reconcile the adapter's "
                "external state manually",
                settlement.settlement_id,
            )
    return recovered


def record_promotion_hook_delivery(
    workspace_root: Path,
    *,
    epoch_id: str,
    round_index: int,
    settlement_id: str,
    state: HookDeliveryState,
    adapter_name: str = "",
    failure_type: str = "",
) -> None:
    """Advance the retained receipt's promotion-hook delivery state.

    ``delivery_unknown`` is written before an external hook is invoked. A
    process death after that write is therefore explicit and recovery never
    retries the side effect. Only the live caller may resolve it to
    ``succeeded`` or ``failed`` after the awaited call returns.
    """
    key = field_settlement_intent_key(epoch_id, round_index)
    recorded = read_settlement_receipt(workspace_root, epoch_id, round_index)
    if recorded is None:
        raise RuntimeError(f"field settlement receipt {key!r} is missing")
    raw = recorded.to_dict()
    settlement, _parent_id = validate_workspace_settlement(
        workspace_root,
        raw,
        expected_epoch_id=epoch_id,
        expected_round_index=round_index,
    )
    if raw["state"] != "committed" or settlement.settlement_id != settlement_id:
        raise RuntimeError("promotion-hook delivery does not match a committed settlement receipt")
    current = raw["promotion_hook"]["state"]
    permitted = {
        "pending": {"not_applicable", "delivery_unknown"},
        "delivery_unknown": {"succeeded", "failed"},
    }
    if state not in permitted.get(current, set()):
        raise RuntimeError(f"invalid promotion-hook delivery transition {current!r} -> {state!r}")
    updated = copy.deepcopy(raw)
    updated["promotion_hook"] = {
        "state": state,
        "adapter_name": adapter_name,
        "failure_type": failure_type,
    }
    write_settlement_receipt(workspace_root, decode_settlement_receipt(updated))


def acknowledge_repaired_settlement_indexes(workspace_root: Path) -> int:
    """Mark committed failed projections repaired after a successful rebuild.

    A full index rebuild derives every settlement row from canonical files.
    Each committed ``repair_required`` receipt is validated before its status
    advances to ``repaired``. The original exception type remains in the
    receipt for auditability.
    """
    pending_updates: list[SettlementReceipt] = []
    for stored in iter_settlement_receipts(workspace_root):
        raw = stored.to_dict()
        validate_workspace_settlement(
            workspace_root,
            raw,
            expected_epoch_id=stored.epoch_id,
            expected_round_index=stored.round_index,
        )
        if raw["state"] != "committed" or raw["index_projection"]["state"] != "repair_required":
            continue
        updated = copy.deepcopy(raw)
        updated["index_projection"]["state"] = "repaired"
        pending_updates.append(decode_settlement_receipt(updated))
    for accepted in pending_updates:
        write_settlement_receipt(workspace_root, accepted)
    return len(pending_updates)


def _project_settlement_index(
    workspace_root: Path,
    settlement: SettlementReceipt,
    receipt: dict[str, Any],
) -> None:
    """Run the settlement's derived-index refresh as one reported operation."""
    try:
        from zicato.evolve.ingest import _index_db_path  # noqa: PLC0415
        from zicato.index.ingest import ingest_field_settlement  # noqa: PLC0415

        db_path = _index_db_path(workspace_root)
        ingest_field_settlement(
            workspace_root,
            db_path,
            settlement.epoch_id,
            [candidate.generation_id for candidate in settlement.candidates],
            settlement.field_record,
        )
    except Exception as exc:  # noqa: BLE001 — the canonical commit remains valid
        receipt["index_projection"] = {
            "state": "repair_required",
            "error_type": type(exc).__name__,
        }
        log.warning(
            "field settlement %s committed canonical files but its derived index "
            "refresh failed (%s); run `zicato repair index`",
            settlement.settlement_id,
            type(exc).__name__,
            exc_info=exc,
        )
    else:
        receipt["index_projection"] = {"state": "succeeded", "error_type": ""}


def _checkpoint(callback: CrashCheckpoint | None, boundary: str) -> None:
    if callback is not None:
        callback(boundary)
