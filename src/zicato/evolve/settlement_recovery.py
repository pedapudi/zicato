"""Commit and recover one field settlement from a durable receipt.

Field settlement crosses several canonical files. Before the first outcome
write, this module persists the complete resolved decision as a pending
receipt. It then applies that decision in a fixed, idempotent order. The same
record remains after completion with ``state="committed"`` so operators and
recovery code retain the evidence needed to audit every write.

This is a transaction boundary for field settlement only. It is intentionally
not a generic transaction or record-storage abstraction.
"""

from __future__ import annotations

import copy
import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from zicato.core.types import OutcomeRecord
from zicato.core.workspace import field_tournament_path
from zicato.epoch.journal import (
    append_journal_entry_once,
    outcome_from_dict,
    read_experiment,
    update_experiment_outcome,
)
from zicato.epoch.lineage import (
    resolve_pending_generations,
    validate_generation_resolutions,
)
from zicato.evolve import generation_phase
from zicato.evolve.dashboard_projection import _write_field_tournament_record
from zicato.storage import workspace_backend
from zicato.workspace import WorkspaceLayout

log = logging.getLogger("zicato.orchestrator")

SETTLEMENT_INTENT_FORMAT_VERSION = 3
SETTLEMENT_INTENT_FILENAME = "field_settlement.json"
CrashCheckpoint = Callable[[str], None]
HookDeliveryState = Literal[
    "not_applicable",
    "pending",
    "succeeded",
    "failed",
    "delivery_unknown",
]

_HOOK_DELIVERY_STATES = frozenset(
    {"not_applicable", "pending", "succeeded", "failed", "delivery_unknown"}
)
_INDEX_PROJECTION_STATES = frozenset({"pending", "succeeded", "repair_required", "repaired"})


@dataclass(frozen=True, slots=True)
class _ValidatedSettlement:
    """Replay facts after every redundant representation agrees."""

    epoch_id: str
    round_index: int
    settlement_id: str
    parent_id: str
    first_challenger_id: str
    primary_id: str | None
    candidates: tuple[tuple[dict[str, Any], OutcomeRecord], ...]
    field_record: dict[str, Any] | None


def field_settlement_intent_key(epoch_id: str, round_index: int) -> str:
    """Return the canonical storage key for one round's settlement receipt."""
    return f"epochs/{epoch_id}/rounds/{int(round_index)}/{SETTLEMENT_INTENT_FILENAME}"


def field_settlement_intent_path(
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
) -> Path:
    """Return the filesystem path for one round's settlement receipt."""
    return (
        WorkspaceLayout.from_root(workspace_root).rounds_dir(epoch_id)
        / str(int(round_index))
        / SETTLEMENT_INTENT_FILENAME
    )


def commit_field_settlement(
    workspace_root: Path,
    intent: dict[str, Any],
    *,
    crash_checkpoint: CrashCheckpoint | None = None,
) -> None:
    """Persist a new decision or idempotently finish the same receipt."""
    validated = _validate_settlement(workspace_root, intent)
    if intent.get("state") != "pending":
        raise RuntimeError("a new field settlement must start in pending state")
    backend = workspace_backend(workspace_root, start=False)
    key = field_settlement_intent_key(validated.epoch_id, validated.round_index)
    existing = backend.read_json(key)
    if existing is not None:
        if not isinstance(existing, dict):
            raise RuntimeError(f"existing field settlement receipt {key!r} is malformed")
        _validate_settlement(
            workspace_root,
            existing,
            expected_epoch_id=validated.epoch_id,
            expected_round_index=validated.round_index,
        )
        if _immutable_receipt_facts(existing) != _immutable_receipt_facts(intent):
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
    backend.write_json(key, intent)
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
    settlement = _validate_settlement(
        workspace_root,
        intent,
        expected_epoch_id=expected_epoch_id,
        expected_round_index=expected_round_index,
    )
    if intent["state"] == "committed":
        return

    receipt = copy.deepcopy(intent)
    finalised: dict[str, Any] = {}
    for raw, outcome in settlement.candidates:
        generation_id = raw["generation_id"]
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
        _lineage_resolutions(settlement.parent_id, settlement.round_index, settlement.candidates),
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

    for raw, _outcome in settlement.candidates:
        generation_id = raw["generation_id"]
        append_journal_entry_once(
            workspace_root,
            settlement.epoch_id,
            finalised[generation_id],
            settlement_identity=f"{settlement.settlement_id}:{generation_id}",
        )
        _checkpoint(crash_checkpoint, f"journal:{generation_id}")

    if settlement.field_record is not None:
        _write_field_tournament_record(
            workspace_root,
            epoch_id=settlement.epoch_id,
            first_challenger_id=settlement.first_challenger_id,
            record=settlement.field_record,
        )
        _checkpoint(crash_checkpoint, "settled_bracket")

    if receipt["index_projection"]["state"] == "pending":
        _project_settlement_index(workspace_root, settlement, receipt)
        workspace_backend(workspace_root, start=False).write_json(
            field_settlement_intent_key(settlement.epoch_id, settlement.round_index),
            receipt,
        )
        _checkpoint(crash_checkpoint, "index_projection")

    receipt["state"] = "committed"
    workspace_backend(workspace_root, start=False).write_json(
        field_settlement_intent_key(settlement.epoch_id, settlement.round_index),
        receipt,
    )
    _checkpoint(crash_checkpoint, "receipt_committed")


def recover_field_settlements(workspace_root: Path, epoch_id: str) -> int:
    """Complete every pending receipt for ``epoch_id`` in round order."""
    if not epoch_id:
        return 0
    recovered = 0
    for backend, key, _stored_epoch, expected_round, raw in _stored_receipts(
        workspace_root, epoch_id
    ):
        if not isinstance(raw, dict):
            raise RuntimeError(f"{key} must contain a JSON object")
        settlement = _validate_settlement(
            workspace_root,
            raw,
            expected_epoch_id=epoch_id,
            expected_round_index=expected_round,
        )
        if raw["state"] == "pending":
            replay_field_settlement(
                workspace_root,
                raw,
                expected_epoch_id=settlement.epoch_id,
                expected_round_index=settlement.round_index,
            )
            recovered += 1
            raw = backend.read_json(key)
            if not isinstance(raw, dict):
                raise RuntimeError(f"replayed field settlement receipt {key!r} is missing")
        if raw["promotion_hook"]["state"] == "pending":
            updated = copy.deepcopy(raw)
            updated["promotion_hook"]["state"] = "delivery_unknown"
            backend.write_json(key, updated)
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
    backend = workspace_backend(workspace_root, start=False)
    raw = backend.read_json(key)
    if not isinstance(raw, dict):
        raise RuntimeError(f"field settlement receipt {key!r} is missing or malformed")
    settlement = _validate_settlement(
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
    backend.write_json(key, updated)


def acknowledge_repaired_settlement_indexes(workspace_root: Path) -> int:
    """Mark committed failed projections repaired after a successful rebuild.

    A full index rebuild derives every settlement row from canonical files.
    Each committed ``repair_required`` receipt is validated before its status
    advances to ``repaired``. The original exception type remains in the
    receipt for auditability.
    """
    pending_updates: list[tuple[Any, str, dict[str, Any]]] = []
    for backend, key, epoch_id, round_index, raw in _stored_receipts(workspace_root):
        if not isinstance(raw, dict):
            raise RuntimeError(f"{key} must contain a JSON object")
        _validate_settlement(
            workspace_root,
            raw,
            expected_epoch_id=epoch_id,
            expected_round_index=round_index,
        )
        if raw["state"] != "committed" or raw["index_projection"]["state"] != "repair_required":
            continue
        updated = copy.deepcopy(raw)
        updated["index_projection"]["state"] = "repaired"
        pending_updates.append((backend, key, updated))
    for backend, key, updated in pending_updates:
        backend.write_json(key, updated)
    return len(pending_updates)


def settlement_index_repair_required(workspace_root: Path) -> bool:
    """Return whether a committed settlement requires index reconstruction."""
    return any(
        isinstance(raw, dict)
        and raw.get("state") == "committed"
        and isinstance(raw.get("index_projection"), dict)
        and raw["index_projection"].get("state") == "repair_required"
        for _backend, _key, _epoch_id, _round_index, raw in _stored_receipts(workspace_root)
    )


def scan_field_settlement_receipts(
    workspace_root: Path,
    epoch_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, str], ...]]:
    """Return valid receipts and one diagnostic record per invalid receipt."""
    receipts: list[dict[str, Any]] = []
    corruptions: list[dict[str, str]] = []
    try:
        locations = tuple(_stored_receipt_locations(workspace_root, epoch_id))
    except Exception as exc:  # namespace enumeration itself failed
        return (), (_receipt_corruption(epoch_id, "", exc),)
    for backend, key, stored_epoch, round_index in locations:
        try:
            raw = backend.read_json(key)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise RuntimeError(f"{key} must contain a JSON object")
            _validate_settlement(
                workspace_root,
                raw,
                expected_epoch_id=stored_epoch,
                expected_round_index=round_index,
            )
        except Exception as exc:  # noqa: BLE001 — returned as operator-visible corruption
            corruptions.append(_receipt_corruption(stored_epoch, key, exc))
        else:
            receipts.append(raw)
    return tuple(receipts), tuple(corruptions)


def _receipt_corruption(epoch_id: str, key: str, exc: Exception) -> dict[str, str]:
    return {
        "epoch_id": epoch_id,
        "storage_key": key,
        "exception_type": type(exc).__name__,
        "message": str(exc),
    }


def _stored_receipts(
    workspace_root: Path,
    epoch_id: str | None = None,
) -> Iterator[tuple[Any, str, str, int, Any]]:
    """Yield backend, key, namespace identity, and raw receipt in round order."""
    for backend, key, stored_epoch, round_index in _stored_receipt_locations(
        workspace_root, epoch_id
    ):
        raw = backend.read_json(key)
        if raw is not None:
            yield backend, key, stored_epoch, round_index, raw


def _stored_receipt_locations(
    workspace_root: Path,
    epoch_id: str | None = None,
) -> Iterator[tuple[Any, str, str, int]]:
    """Yield each receipt key with the epoch and round encoded by its namespace."""
    backend = workspace_backend(workspace_root, start=False)
    epoch_namespaces = (
        (f"epochs/{epoch_id}",) if epoch_id else tuple(backend.list_namespaces("epochs"))
    )
    for epoch_namespace in epoch_namespaces:
        parts = epoch_namespace.split("/")
        if len(parts) != 2 or parts[0] != "epochs" or not parts[1]:
            continue
        stored_epoch = parts[1]
        namespaces = sorted(
            backend.list_namespaces(f"{epoch_namespace}/rounds"),
            key=_round_namespace_order,
        )
        for namespace in namespaces:
            round_index = _validate_containing_namespace(namespace, stored_epoch)
            key = f"{namespace}/{SETTLEMENT_INTENT_FILENAME}"
            yield backend, key, stored_epoch, round_index


def _validate_settlement(
    workspace_root: Path,
    intent: dict[str, Any],
    *,
    expected_epoch_id: str | None = None,
    expected_round_index: int | None = None,
) -> _ValidatedSettlement:
    """Validate every replay fact before interpreting state or mutating files."""
    if intent.get("format_version") != SETTLEMENT_INTENT_FORMAT_VERSION:
        raise RuntimeError(
            "field settlement receipt has unsupported format_version "
            f"{intent.get('format_version')!r}"
        )
    epoch_id = _required_string(intent, "epoch_id")
    round_index = _required_int(intent, "round_index")
    if expected_epoch_id is not None and epoch_id != expected_epoch_id:
        raise RuntimeError(
            f"field settlement receipt names epoch {epoch_id!r} inside {expected_epoch_id!r}"
        )
    if expected_round_index is not None and round_index != expected_round_index:
        raise RuntimeError(
            f"field settlement receipt names round {round_index} inside round "
            f"{expected_round_index}"
        )
    state = intent.get("state")
    if state not in {"pending", "committed"}:
        raise RuntimeError(f"field settlement receipt has invalid state {state!r}")
    recorded_primary = intent.get("primary_promoted_generation_id")
    if recorded_primary is not None and (
        not isinstance(recorded_primary, str) or not recorded_primary
    ):
        raise RuntimeError(
            "field settlement receipt primary_promoted_generation_id must be a "
            "non-empty string or null"
        )

    settlement_id = _required_string(intent, "settlement_id")
    hook = intent.get("promotion_hook")
    if not isinstance(hook, dict) or hook.get("state") not in _HOOK_DELIVERY_STATES:
        raise RuntimeError("field settlement receipt has invalid promotion_hook state")
    if not isinstance(hook.get("adapter_name"), str) or not isinstance(
        hook.get("failure_type"), str
    ):
        raise RuntimeError("field settlement receipt has invalid promotion_hook details")
    hook_state = hook["state"]
    adapter_name = hook["adapter_name"]
    failure_type = hook["failure_type"]
    if hook_state == "not_applicable" and (adapter_name or failure_type):
        raise RuntimeError("field settlement receipt has premature promotion_hook details")
    if hook_state in {"pending", "succeeded", "delivery_unknown"} and (
        not adapter_name or failure_type
    ):
        raise RuntimeError("field settlement receipt has inconsistent promotion_hook result")
    if hook_state == "failed" and (not adapter_name or not failure_type):
        raise RuntimeError("field settlement receipt has incomplete promotion_hook failure")
    index_projection = intent.get("index_projection")
    if (
        not isinstance(index_projection, dict)
        or index_projection.get("state") not in _INDEX_PROJECTION_STATES
        or not isinstance(index_projection.get("error_type"), str)
    ):
        raise RuntimeError("field settlement receipt has invalid index_projection state")
    index_state = index_projection["state"]
    index_error = index_projection["error_type"]
    if (index_state in {"repair_required", "repaired"}) != bool(index_error):
        raise RuntimeError("field settlement receipt has inconsistent index_projection result")
    if state == "committed" and index_state == "pending":
        raise RuntimeError("committed field settlement receipt has a pending index projection")

    raw_candidates = intent.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise RuntimeError("field settlement receipt has no candidate list")
    candidates: list[tuple[dict[str, Any], OutcomeRecord]] = []
    generation_ids: set[str] = set()
    parent_ids: set[str] = set()
    promoted_ids: set[str] = set()
    structure = ""
    for raw in raw_candidates:
        if not isinstance(raw, dict) or not isinstance(raw.get("outcome"), dict):
            raise RuntimeError("field settlement receipt contains an invalid candidate record")
        experiment_id = _required_string(raw, "experiment_id")
        generation_id = _required_string(raw, "generation_id")
        if generation_id in generation_ids:
            raise RuntimeError(
                f"field settlement receipt contains duplicate generation {generation_id!r}"
            )
        _required_string(raw, "created_at")
        _validate_optional_number(raw.get("parent_scalar"), "parent_scalar")
        _validate_optional_number(raw.get("child_scalar"), "child_scalar")
        outcome = outcome_from_dict(raw["outcome"])
        if structure and outcome.structure != structure:
            raise RuntimeError(
                f"field settlement candidate {generation_id!r} names structure "
                f"{outcome.structure!r}, expected {structure!r}"
            )
        structure = outcome.structure
        experiment = read_experiment(workspace_root, epoch_id, generation_id)
        if (
            experiment.id != experiment_id
            or experiment.epoch_id != epoch_id
            or experiment.generation_id != generation_id
            or experiment.round_index != round_index
        ):
            raise RuntimeError(
                f"field settlement candidate {generation_id!r} does not match its experiment"
            )
        if state == "committed" and experiment.outcome != outcome:
            raise RuntimeError(
                f"committed field settlement candidate {generation_id!r} lacks its outcome"
            )
        if state == "pending" and experiment.outcome is not None and experiment.outcome != outcome:
            raise RuntimeError(
                f"field settlement candidate {generation_id!r} already has a different outcome"
            )
        generation_ids.add(generation_id)
        parent_id = experiment.parent_generation_id
        if not isinstance(parent_id, str) or not parent_id:
            raise RuntimeError(
                f"field settlement candidate {generation_id!r} has no parent generation"
            )
        parent_ids.add(parent_id)
        if outcome.tournament_decision == "promoted":
            promoted_ids.add(generation_id)
        candidates.append((raw, outcome))

    if len(parent_ids) != 1:
        raise RuntimeError("field settlement candidates do not share one parent generation")
    parent_id = parent_ids.pop()

    first_challenger = candidates[0][0]["generation_id"]
    if len(settlement_id) != 32 or any(
        character not in "0123456789abcdef" for character in settlement_id
    ):
        raise RuntimeError("field settlement receipt has inconsistent round identity")

    decision = (
        "promoted"
        if promoted_ids
        else (
            "deferred"
            if any(outcome.tournament_decision == "deferred" for _raw, outcome in candidates)
            else "rejected"
        )
    )
    field_record, primary = _validate_field_record(
        intent.get("field_tournament_record"),
        epoch_id=epoch_id,
        first_challenger=first_challenger,
        parent_id=parent_id,
        candidate_ids=generation_ids,
        promoted_ids=promoted_ids,
        structure=structure,
        decision=decision,
    )
    if primary != recorded_primary:
        raise RuntimeError(
            "field settlement receipt primary champion conflicts with its tournament record"
        )
    _validate_existing_field_record(
        workspace_root,
        epoch_id=epoch_id,
        first_challenger=first_challenger,
        expected=field_record,
    )
    validate_generation_resolutions(
        workspace_root,
        epoch_id,
        _lineage_resolutions(parent_id, round_index, tuple(candidates)),
        require_resolved=state == "committed",
    )
    if state == "pending":
        _validate_existing_marker(workspace_root, epoch_id, parent_id, primary)
    return _ValidatedSettlement(
        epoch_id=epoch_id,
        round_index=round_index,
        settlement_id=settlement_id,
        parent_id=parent_id,
        first_challenger_id=first_challenger,
        primary_id=primary,
        candidates=tuple(candidates),
        field_record=field_record,
    )


def _validate_field_record(
    raw_record: Any,
    *,
    epoch_id: str,
    first_challenger: str,
    parent_id: str,
    candidate_ids: set[str],
    promoted_ids: set[str],
    structure: str,
    decision: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the durable bracket against independent receipt facts."""
    if raw_record is None:
        if len(candidate_ids) > 1:
            raise RuntimeError("multi-challenger settlement receipt has no field tournament record")
        return None, next(iter(promoted_ids), None)
    if not isinstance(raw_record, dict):
        raise RuntimeError("field settlement tournament record must be an object")
    expected_tournament_id = f"{epoch_id}:field:{first_challenger}"
    if raw_record.get("tournament_id") != expected_tournament_id:
        raise RuntimeError("field settlement tournament record has a different identity")
    if raw_record.get("epoch_id") != epoch_id or raw_record.get("state") != "settled":
        raise RuntimeError("field settlement tournament record has an invalid epoch or state")
    if raw_record.get("structure") != structure or not isinstance(
        raw_record.get("structure_params"), dict
    ):
        raise RuntimeError("field settlement tournament record has inconsistent structure")
    if raw_record.get("champion_generation_id") != parent_id:
        raise RuntimeError("field settlement tournament record names a different incumbent")

    competitors = raw_record.get("competitors")
    if not isinstance(competitors, list) or any(not isinstance(row, dict) for row in competitors):
        raise RuntimeError("field settlement tournament record has invalid competitors")
    competitor_ids = [row.get("generation_id") for row in competitors]
    expected_ids = {parent_id, *candidate_ids}
    if (
        any(not isinstance(item, str) or not item for item in competitor_ids)
        or len(competitor_ids) != len(set(competitor_ids))
        or set(competitor_ids) != expected_ids
    ):
        raise RuntimeError("field settlement tournament record has inconsistent competitors")
    roles = {str(row["generation_id"]): row.get("role") for row in competitors}
    if roles.get(parent_id) != "champion" or any(
        roles.get(generation_id) != "challenger" for generation_id in candidate_ids
    ):
        raise RuntimeError("field settlement tournament record has inconsistent competitor roles")

    record_primary = raw_record.get("promoted_generation_id") or None
    if record_primary is not None and (
        not isinstance(record_primary, str) or record_primary not in promoted_ids
    ):
        raise RuntimeError("field settlement tournament record names an invalid primary champion")
    raw_promoted = raw_record.get("promoted_generation_ids")
    if raw_promoted is None:
        record_promoted = {record_primary} if record_primary is not None else set()
    else:
        if (
            not isinstance(raw_promoted, list)
            or any(not isinstance(item, str) or not item for item in raw_promoted)
            or len(raw_promoted) != len(set(raw_promoted))
        ):
            raise RuntimeError("field settlement tournament record has invalid promoted ids")
        record_promoted = set(raw_promoted)
    if record_promoted != promoted_ids:
        raise RuntimeError("field settlement tournament record has a different promoted set")
    if (record_primary is None) != (not promoted_ids):
        raise RuntimeError("field settlement tournament record lacks its primary champion")
    if raw_record.get("decision") != decision or not isinstance(raw_record.get("reason"), str):
        raise RuntimeError("field settlement tournament record has a different decision or reason")
    for key in ("rounds", "standings", "field_status"):
        if not isinstance(raw_record.get(key), list):
            raise RuntimeError(f"field settlement tournament record has invalid {key}")
    if not isinstance(raw_record.get("ran_at"), str) or not raw_record["ran_at"]:
        raise RuntimeError("field settlement tournament record has invalid ran_at")
    _validate_optional_number(raw_record.get("delta_scalar"), "delta_scalar")
    return raw_record, record_primary


def _lineage_resolutions(
    parent_id: str,
    round_index: int,
    candidates: tuple[tuple[dict[str, Any], OutcomeRecord], ...],
) -> dict[str, dict[str, Any]]:
    return {
        raw["generation_id"]: {
            "parent_id": parent_id,
            "created_at": raw["created_at"],
            "round_index": round_index,
            "promoted": outcome.tournament_decision == "promoted",
            "rejection_reason": outcome.rejection_reason,
            "parent_scalar": _optional_float(raw.get("parent_scalar")),
            "child_scalar": _optional_float(raw.get("child_scalar")),
        }
        for raw, outcome in candidates
    }


def _validate_existing_field_record(
    workspace_root: Path,
    *,
    epoch_id: str,
    first_challenger: str,
    expected: dict[str, Any] | None,
) -> None:
    """Refuse to overwrite a conflicting canonical field snapshot."""
    if expected is None:
        return
    path = field_tournament_path(workspace_root, epoch_id, first_challenger)
    if not path.exists():
        return
    try:
        backend = workspace_backend(workspace_root, start=False)
        raw = backend.read_json(path.relative_to(workspace_root).as_posix())
    except Exception as exc:
        raise RuntimeError(
            f"existing field settlement tournament record {path} is unreadable"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"existing field settlement tournament record {path} must be an object")
    state = raw.get("state")
    if state == "settled":
        if raw != expected:
            raise RuntimeError(
                "existing settled field settlement tournament conflicts with its receipt"
            )
        return
    if state != "in_progress":
        raise RuntimeError(f"existing field settlement tournament has invalid state {state!r}")
    for key in (
        "tournament_id",
        "epoch_id",
        "structure",
        "structure_params",
        "competitors",
        "champion_generation_id",
    ):
        if raw.get(key) != expected.get(key):
            raise RuntimeError(
                f"existing in-progress field tournament conflicts on {key} "
                "(field settlement tournament record)"
            )


def _validate_existing_marker(
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    primary: str | None,
) -> None:
    if primary is None:
        return
    marker = generation_phase.current_marker(workspace_root, epoch_id)
    if not marker.exists():
        return
    current = marker.read_text(encoding="utf-8").strip()
    if current not in (parent_id, primary):
        raise RuntimeError(
            f"field settlement cannot replace current_generation {current!r}; "
            f"expected {parent_id!r} or {primary!r}"
        )


def _project_settlement_index(
    workspace_root: Path,
    settlement: _ValidatedSettlement,
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
            [candidate["generation_id"] for candidate, _outcome in settlement.candidates],
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


def _validate_containing_namespace(namespace: str, epoch_id: str) -> int:
    """Return the round encoded by an exact ``epochs/<epoch>/rounds/<n>`` key."""
    prefix = f"epochs/{epoch_id}/rounds/"
    if not namespace.startswith(prefix):
        raise RuntimeError(f"field settlement receipt has invalid namespace {namespace!r}")
    tail = namespace.removeprefix(prefix)
    if not tail.isdigit() or str(int(tail)) != tail:
        raise RuntimeError(f"field settlement receipt has invalid round namespace {namespace!r}")
    return int(tail)


def _immutable_receipt_facts(receipt: dict[str, Any]) -> dict[str, Any]:
    """Return the decision payload without commit-progress fields."""
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"state", "index_projection", "promotion_hook"}
    }


def _round_namespace_order(namespace: str) -> tuple[int, str]:
    tail = namespace.rsplit("/", 1)[-1]
    return (int(tail), namespace) if tail.isdigit() else (2**31 - 1, namespace)


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"field settlement receipt {key} must be a non-empty string")
    return value


def _required_int(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"field settlement receipt {key} must be a non-negative integer")
    return value


def _validate_optional_number(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise RuntimeError(f"field settlement receipt {name} must be finite or null")


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _checkpoint(callback: CrashCheckpoint | None, boundary: str) -> None:
    if callback is not None:
        callback(boundary)


__all__ = [
    "SETTLEMENT_INTENT_FILENAME",
    "SETTLEMENT_INTENT_FORMAT_VERSION",
    "commit_field_settlement",
    "field_settlement_intent_path",
    "scan_field_settlement_receipts",
    "record_promotion_hook_delivery",
    "settlement_index_repair_required",
    "recover_field_settlements",
    "replay_field_settlement",
]
