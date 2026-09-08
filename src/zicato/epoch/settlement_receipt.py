"""Settlement receipt facts, strict decoding, and read-only consistency checks.

The format-3 receipt owns the resolved decision. Replay progress may advance;
recorded outcomes, candidate order, optional fields, and numeric types remain
unchanged. Inspection never invokes replay or external promotion hooks.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from zicato.core.types import Experiment, OutcomeRecord
from zicato.epoch._storage import (
    RecordError,
    check_record_format,
    epoch_prefix,
    epochs_prefix,
    rounds_prefix,
)
from zicato.epoch.journal import outcome_from_dict, read_experiment
from zicato.epoch.lineage import Lineage, load_lineage, validate_generation_resolution_rows
from zicato.storage import workspace_backend
from zicato.tournament.records import (
    FieldTournamentRecord,
    read_field_tournament_record,
    validate_settlement_field_record,
)
from zicato.workspace import WorkspaceLayout
from zicato.workspace.layout import WORKSPACE_RELATIVE_LAYOUT, storage_key

SETTLEMENT_INTENT_FORMAT_VERSION = 3
HookDeliveryState = Literal["not_applicable", "pending", "succeeded", "failed", "delivery_unknown"]
_HOOK_DELIVERY_STATES = frozenset(
    {"not_applicable", "pending", "succeeded", "failed", "delivery_unknown"}
)
_INDEX_PROJECTION_STATES = frozenset({"pending", "succeeded", "repair_required", "repaired"})


@dataclass(frozen=True, slots=True)
class SettlementCandidate:
    """One immutable candidate identity and its recorded outcome representation."""

    experiment_id: str
    generation_id: str
    created_at: str
    parent_scalar: int | float | None
    child_scalar: int | float | None
    _outcome_json: str = field(repr=False)
    _json: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_outcome(
        cls,
        *,
        experiment_id: str,
        generation_id: str,
        created_at: str,
        parent_scalar: int | float | None,
        child_scalar: int | float | None,
        outcome: OutcomeRecord,
    ) -> SettlementCandidate:
        return cls(
            experiment_id,
            generation_id,
            created_at,
            parent_scalar,
            child_scalar,
            json.dumps(asdict(outcome), allow_nan=False),
        )

    def to_dict(self) -> dict[str, Any]:
        fields = {
            "experiment_id": self.experiment_id,
            "generation_id": self.generation_id,
            "created_at": self.created_at,
            "parent_scalar": self.parent_scalar,
            "child_scalar": self.child_scalar,
            "outcome": json.loads(self._outcome_json),
        }
        if self._json is None:
            return fields
        body: dict[str, Any] = json.loads(self._json)
        body.update(
            (key, value) for key, value in fields.items() if key in body or value is not None
        )
        return body

    @property
    def outcome(self) -> OutcomeRecord:
        # The outcome owner normalizes optional fields for interpretation.
        # Its mutable nested evidence must never alias retained receipt facts.
        return outcome_from_dict(json.loads(self._outcome_json))


@dataclass(frozen=True, slots=True)
class PromotionHook:
    state: str
    adapter_name: str
    failure_type: str


@dataclass(frozen=True, slots=True)
class IndexProjection:
    state: str
    error_type: str


@dataclass(frozen=True, slots=True)
class SettlementReceipt:
    """Accepted facts and progress with a lossless, detached JSON representation.

    The retained representation preserves omitted optional fields and integer
    numbers that the outcome codec normalizes for interpretation. Consumers
    receive copies; none can change the accepted facts through nested values.
    """

    epoch_id: str
    round_index: int
    settlement_id: str
    state: str
    primary_id: str | None
    candidates: tuple[SettlementCandidate, ...]
    promotion_hook: PromotionHook
    index_projection: IndexProjection
    _json: str = field(repr=False)

    @property
    def first_challenger_id(self) -> str:
        return self.candidates[0].generation_id

    @property
    def field_record(self) -> dict[str, Any] | None:
        return self.to_dict().get("field_tournament_record")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self._json)
        body.update(
            epoch_id=self.epoch_id,
            round_index=self.round_index,
            settlement_id=self.settlement_id,
            state=self.state,
            candidates=[candidate.to_dict() for candidate in self.candidates],
        )
        if "primary_promoted_generation_id" in body or self.primary_id is not None:
            body["primary_promoted_generation_id"] = self.primary_id
        body["promotion_hook"].update(asdict(self.promotion_hook))
        body["index_projection"].update(asdict(self.index_projection))
        return body

    def immutable_facts(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.to_dict().items()
            if key not in {"state", "index_projection", "promotion_hook"}
        }


def field_settlement_intent_key(epoch_id: str, round_index: int) -> str:
    """Return the canonical storage key for one round's settlement receipt."""
    return storage_key(WORKSPACE_RELATIVE_LAYOUT.field_settlement(epoch_id, round_index))


def field_settlement_intent_path(workspace_root: Path, epoch_id: str, round_index: int) -> Path:
    """Return the canonical receipt path from the workspace layout."""
    return WorkspaceLayout.from_root(workspace_root).field_settlement(epoch_id, round_index)


def decode_settlement_receipt(
    intent: Any,
    *,
    expected_epoch_id: str | None = None,
    expected_round_index: int | None = None,
) -> SettlementReceipt:
    """Decode receipt-local facts without reading other records or opening files."""
    if not isinstance(intent, dict):
        raise RecordError("field settlement receipt must contain a JSON object")
    check_record_format(
        intent,
        "field settlement receipt",
        expected_version=SETTLEMENT_INTENT_FORMAT_VERSION,
    )
    epoch_id = _required_string(intent, "epoch_id")
    round_index = _required_int(intent, "round_index")
    if expected_epoch_id is not None and epoch_id != expected_epoch_id:
        raise RecordError(
            f"field settlement receipt names epoch {epoch_id!r} inside {expected_epoch_id!r}"
        )
    if expected_round_index is not None and round_index != expected_round_index:
        raise RecordError(
            f"field settlement receipt names round {round_index} inside round "
            f"{expected_round_index}"
        )
    state = intent.get("state")
    if not isinstance(state, str) or state not in {"pending", "committed"}:
        raise RecordError(f"field settlement receipt has invalid state {state!r}")
    recorded_primary = intent.get("primary_promoted_generation_id")
    if recorded_primary is not None and (
        not isinstance(recorded_primary, str) or not recorded_primary
    ):
        raise RecordError(
            "field settlement receipt primary_promoted_generation_id must be a "
            "non-empty string or null"
        )

    settlement_id = _required_string(intent, "settlement_id")
    hook = intent.get("promotion_hook")
    if (
        not isinstance(hook, dict)
        or not isinstance(hook.get("state"), str)
        or hook["state"] not in _HOOK_DELIVERY_STATES
    ):
        raise RecordError("field settlement receipt has invalid promotion_hook state")
    if not isinstance(hook.get("adapter_name"), str) or not isinstance(
        hook.get("failure_type"), str
    ):
        raise RecordError("field settlement receipt has invalid promotion_hook details")
    hook_state = hook["state"]
    adapter_name = hook["adapter_name"]
    failure_type = hook["failure_type"]
    if hook_state == "not_applicable" and (adapter_name or failure_type):
        raise RecordError("field settlement receipt has premature promotion_hook details")
    if hook_state in {"pending", "succeeded", "delivery_unknown"} and (
        not adapter_name or failure_type
    ):
        raise RecordError("field settlement receipt has inconsistent promotion_hook result")
    if hook_state == "failed" and (not adapter_name or not failure_type):
        raise RecordError("field settlement receipt has incomplete promotion_hook failure")
    index_projection = intent.get("index_projection")
    if (
        not isinstance(index_projection, dict)
        or not isinstance(index_projection.get("state"), str)
        or index_projection["state"] not in _INDEX_PROJECTION_STATES
        or not isinstance(index_projection.get("error_type"), str)
    ):
        raise RecordError("field settlement receipt has invalid index_projection state")
    index_state = index_projection["state"]
    index_error = index_projection["error_type"]
    if (index_state in {"repair_required", "repaired"}) != bool(index_error):
        raise RecordError("field settlement receipt has inconsistent index_projection result")
    if state == "committed" and index_state == "pending":
        raise RecordError("committed field settlement receipt has a pending index projection")

    raw_candidates = intent.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise RecordError("field settlement receipt has no candidate list")
    candidates: list[tuple[dict[str, Any], OutcomeRecord]] = []
    generation_ids: set[str] = set()
    promoted_ids: set[str] = set()
    structure = ""
    for raw in raw_candidates:
        if not isinstance(raw, dict) or not isinstance(raw.get("outcome"), dict):
            raise RecordError("field settlement receipt contains an invalid candidate record")
        _required_string(raw, "experiment_id")
        generation_id = _required_string(raw, "generation_id")
        if generation_id in generation_ids:
            raise RecordError(
                f"field settlement receipt contains duplicate generation {generation_id!r}"
            )
        _required_string(raw, "created_at")
        _validate_optional_number(raw.get("parent_scalar"), "parent_scalar")
        _validate_optional_number(raw.get("child_scalar"), "child_scalar")
        outcome = outcome_from_dict(raw["outcome"])
        if structure and outcome.structure != structure:
            raise RecordError(
                f"field settlement candidate {generation_id!r} names structure "
                f"{outcome.structure!r}, expected {structure!r}"
            )
        structure = outcome.structure
        generation_ids.add(generation_id)
        if outcome.tournament_decision == "promoted":
            promoted_ids.add(generation_id)
        candidates.append((raw, outcome))

    first_challenger = candidates[0][0]["generation_id"]
    if len(settlement_id) != 32 or any(
        character not in "0123456789abcdef" for character in settlement_id
    ):
        raise RecordError("field settlement receipt has inconsistent round identity")

    decision = (
        "promoted"
        if promoted_ids
        else (
            "deferred"
            if any(outcome.tournament_decision == "deferred" for _raw, outcome in candidates)
            else "rejected"
        )
    )
    raw_field = intent.get("field_tournament_record")
    parent_id = raw_field.get("champion_generation_id", "") if isinstance(raw_field, dict) else ""
    _field_record, primary = validate_settlement_field_record(
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
        raise RecordError(
            "field settlement receipt primary champion conflicts with its tournament record"
        )
    return SettlementReceipt(
        epoch_id=epoch_id,
        round_index=round_index,
        settlement_id=settlement_id,
        state=state,
        primary_id=primary,
        candidates=tuple(
            SettlementCandidate(
                raw["experiment_id"],
                raw["generation_id"],
                raw["created_at"],
                raw.get("parent_scalar"),
                raw.get("child_scalar"),
                json.dumps(raw["outcome"], allow_nan=False),
                json.dumps(raw, allow_nan=False),
            )
            for raw, _outcome in candidates
        ),
        promotion_hook=PromotionHook(hook_state, adapter_name, failure_type),
        index_projection=IndexProjection(index_state, index_error),
        _json=json.dumps(intent, allow_nan=False),
    )


def new_settlement_receipt(
    *,
    settlement_id: str,
    epoch_id: str,
    round_index: int,
    primary_id: str | None,
    candidates: tuple[SettlementCandidate, ...],
    field_record: dict[str, Any] | None,
    hook_adapter_name: str = "",
) -> SettlementReceipt:
    """Construct the complete pending format-3 decision before publication."""
    return decode_settlement_receipt(
        {
            "format_version": SETTLEMENT_INTENT_FORMAT_VERSION,
            "state": "pending",
            "settlement_id": settlement_id,
            "epoch_id": epoch_id,
            "round_index": round_index,
            "primary_promoted_generation_id": primary_id,
            "candidates": [candidate.to_dict() for candidate in candidates],
            "field_tournament_record": field_record,
            "index_projection": {"state": "pending", "error_type": ""},
            "promotion_hook": {
                "state": "pending" if hook_adapter_name else "not_applicable",
                "adapter_name": hook_adapter_name,
                "failure_type": "",
            },
        }
    )


def write_settlement_receipt(workspace_root: Path, receipt: SettlementReceipt) -> None:
    """Atomically publish accepted receipt facts and their validated progress."""
    receipt = decode_settlement_receipt(receipt.to_dict())
    workspace_backend(workspace_root, start=False).write_json(
        field_settlement_intent_key(receipt.epoch_id, receipt.round_index), receipt.to_dict()
    )


def validate_settlement_experiments(
    receipt: SettlementReceipt, experiments: tuple[Experiment, ...]
) -> str:
    """Check experiment authority against accepted receipt facts without I/O."""
    if len(experiments) != len(receipt.candidates):
        raise RecordError("field settlement lacks candidate experiments")
    parents: set[str] = set()
    for candidate, experiment in zip(receipt.candidates, experiments, strict=True):
        generation_id = candidate.generation_id
        if (
            experiment.id != candidate.experiment_id
            or experiment.epoch_id != receipt.epoch_id
            or experiment.generation_id != generation_id
            or experiment.round_index != receipt.round_index
        ):
            raise RecordError(
                f"field settlement candidate {generation_id!r} does not match its experiment"
            )
        outcome = candidate.outcome
        if receipt.state == "committed" and experiment.outcome != outcome:
            raise RecordError(
                f"committed field settlement candidate {generation_id!r} lacks its outcome"
            )
        if (
            receipt.state == "pending"
            and experiment.outcome is not None
            and experiment.outcome != outcome
        ):
            raise RecordError(
                f"field settlement candidate {generation_id!r} already has a different outcome"
            )
        parent_id = experiment.parent_generation_id
        if not isinstance(parent_id, str) or not parent_id:
            raise RecordError(
                f"field settlement candidate {generation_id!r} has no parent generation"
            )
        parents.add(parent_id)
    if len(parents) != 1:
        raise RecordError("field settlement candidates do not share one parent generation")
    parent_id = parents.pop()
    field_record = receipt.field_record
    if field_record is not None and field_record["champion_generation_id"] != parent_id:
        raise RecordError("field settlement tournament record names a different incumbent")
    return parent_id


def validate_workspace_settlement(
    workspace_root: Path,
    intent: Any,
    *,
    expected_epoch_id: str | None = None,
    expected_round_index: int | None = None,
) -> tuple[SettlementReceipt, str]:
    """Read all settlement authorities and reject contradictions before mutation."""
    receipt = decode_settlement_receipt(
        intent, expected_epoch_id=expected_epoch_id, expected_round_index=expected_round_index
    )
    experiments = tuple(
        read_experiment(workspace_root, receipt.epoch_id, candidate.generation_id)
        for candidate in receipt.candidates
    )
    layout = WorkspaceLayout.from_root(workspace_root)
    existing_field = None
    if receipt.field_record is not None:
        try:
            existing_field = read_field_tournament_record(
                layout.field_tournament(receipt.epoch_id, receipt.first_challenger_id)
            )
        except FileNotFoundError:
            pass
    marker = None
    if receipt.state == "pending" and receipt.primary_id is not None:
        try:
            marker = (
                layout.current_generation_marker(receipt.epoch_id)
                .read_text(encoding="utf-8")
                .strip()
            )
        except FileNotFoundError:
            pass
    lineage = load_lineage(workspace_root)
    parent_id = validate_settlement_records(
        receipt,
        experiments=experiments,
        lineage=lineage,
        field_record=existing_field,
        current_generation=marker,
    )
    return receipt, parent_id


def validate_settlement_records(
    receipt: SettlementReceipt,
    *,
    experiments: tuple[Experiment, ...],
    lineage: Lineage,
    field_record: FieldTournamentRecord | None,
    current_generation: str | None,
) -> str:
    """Compare independently loaded authorities without I/O or mutation."""
    parent_id = validate_settlement_experiments(receipt, experiments)
    _validate_existing_field_record(field_record, expected=receipt.field_record)
    validate_generation_resolution_rows(
        lineage,
        receipt.epoch_id,
        lineage_resolutions(parent_id, receipt),
        require_resolved=receipt.state == "committed",
    )
    if receipt.state == "pending" and receipt.primary_id is not None:
        if current_generation is not None and current_generation not in (
            parent_id,
            receipt.primary_id,
        ):
            raise RecordError(
                f"field settlement cannot replace current_generation {current_generation!r}; "
                f"expected {parent_id!r} or {receipt.primary_id!r}"
            )
    return parent_id


def lineage_resolutions(parent_id: str, receipt: SettlementReceipt) -> dict[str, dict[str, Any]]:
    """Project accepted candidate decisions into the lineage owner's mutation input."""
    return {
        candidate.generation_id: {
            "parent_id": parent_id,
            "created_at": candidate.created_at,
            "round_index": receipt.round_index,
            "promoted": candidate.outcome.tournament_decision == "promoted",
            "rejection_reason": candidate.outcome.rejection_reason,
            "parent_scalar": None
            if candidate.parent_scalar is None
            else float(candidate.parent_scalar),
            "child_scalar": None
            if candidate.child_scalar is None
            else float(candidate.child_scalar),
        }
        for candidate in receipt.candidates
    }


def read_settlement_receipt(
    workspace_root: Path, epoch_id: str, round_index: int
) -> SettlementReceipt | None:
    """Read one receipt strictly; absence is legal before settlement publication."""
    key = field_settlement_intent_key(epoch_id, round_index)
    try:
        text = workspace_backend(workspace_root, start=False).read_text(key)
        return (
            None
            if text is None
            else decode_settlement_receipt(
                json.loads(text), expected_epoch_id=epoch_id, expected_round_index=round_index
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise RecordError(f"field settlement receipt {key}: {exc}") from exc


def _validate_existing_field_record(
    existing: FieldTournamentRecord | None,
    *,
    expected: dict[str, Any] | None,
) -> None:
    """Refuse a conflicting canonical snapshot without reading or writing files."""
    if expected is None or existing is None:
        return
    raw = existing.to_dict()
    state = raw.get("state")
    if state == "settled":
        if raw != expected:
            raise RecordError(
                "existing settled field settlement tournament conflicts with its receipt"
            )
        return
    if state != "in_progress":
        raise RecordError(f"existing field settlement tournament has invalid state {state!r}")
    for key in (
        "tournament_id",
        "epoch_id",
        "structure",
        "structure_params",
        "competitors",
        "champion_generation_id",
    ):
        if raw.get(key) != expected.get(key):
            raise RecordError(
                f"existing in-progress field tournament conflicts on {key} "
                "(field settlement tournament record)"
            )


def scan_field_settlement_receipts(
    workspace_root: Path,
    epoch_id: str,
) -> tuple[tuple[SettlementReceipt, ...], tuple[dict[str, str], ...]]:
    """Return valid receipts and one diagnostic record per invalid receipt."""
    receipts: list[SettlementReceipt] = []
    corruptions: list[dict[str, str]] = []
    try:
        locations = tuple(_stored_receipt_locations(workspace_root, epoch_id))
    except Exception as exc:  # namespace enumeration itself failed
        return (), (_receipt_corruption(epoch_id, "", exc),)
    for _backend, key, stored_epoch, round_index in locations:
        try:
            receipt = read_settlement_receipt(workspace_root, stored_epoch, round_index)
            if receipt is None:
                continue
            receipt, _parent_id = validate_workspace_settlement(
                workspace_root,
                receipt.to_dict(),
                expected_epoch_id=stored_epoch,
                expected_round_index=round_index,
            )
        except Exception as exc:  # noqa: BLE001 — returned as operator-visible corruption
            corruptions.append(_receipt_corruption(stored_epoch, key, exc))
        else:
            receipts.append(receipt)
    return tuple(receipts), tuple(corruptions)


def settlement_index_repair_required(workspace_root: Path) -> bool:
    """Inspect accepted receipt progress without invoking settlement replay."""
    for receipt in iter_settlement_receipts(workspace_root):
        if receipt.state == "committed" and receipt.index_projection.state == "repair_required":
            return True
    return False


def _receipt_corruption(epoch_id: str, key: str, exc: Exception) -> dict[str, str]:
    return {
        "epoch_id": epoch_id,
        "storage_key": key,
        "exception_type": type(exc).__name__,
        "message": str(exc),
    }


def iter_settlement_receipts(
    workspace_root: Path,
    epoch_id: str | None = None,
) -> Iterator[SettlementReceipt]:
    """Yield accepted receipts in epoch and numeric round order."""
    for _backend, _key, stored_epoch, round_index in _stored_receipt_locations(
        workspace_root, epoch_id
    ):
        receipt = read_settlement_receipt(workspace_root, stored_epoch, round_index)
        if receipt is not None:
            yield receipt


def _stored_receipt_locations(
    workspace_root: Path,
    epoch_id: str | None = None,
) -> Iterator[tuple[Any, str, str, int]]:
    """Yield each receipt key with the epoch and round encoded by its namespace."""
    backend = workspace_backend(workspace_root, start=False)
    epochs_key = epochs_prefix()
    epoch_namespaces = (
        (epoch_prefix(epoch_id),) if epoch_id else tuple(backend.list_namespaces(epochs_key))
    )
    for epoch_namespace in epoch_namespaces:
        parts = epoch_namespace.split("/")
        if len(parts) != 2 or parts[0] != epochs_key or not parts[1]:
            continue
        stored_epoch = parts[1]
        namespaces = sorted(
            backend.list_namespaces(rounds_prefix(stored_epoch)),
            key=_round_namespace_order,
        )
        for namespace in namespaces:
            round_index = _validate_containing_namespace(namespace, stored_epoch)
            key = field_settlement_intent_key(stored_epoch, round_index)
            yield backend, key, stored_epoch, round_index


def _validate_containing_namespace(namespace: str, epoch_id: str) -> int:
    """Return the round encoded by an exact ``epochs/<epoch>/rounds/<n>`` key."""
    prefix = f"{rounds_prefix(epoch_id)}/"
    if not namespace.startswith(prefix):
        raise RecordError(f"field settlement receipt has invalid namespace {namespace!r}")
    tail = namespace.removeprefix(prefix)
    if not tail.isdigit() or str(int(tail)) != tail:
        raise RecordError(f"field settlement receipt has invalid round namespace {namespace!r}")
    return int(tail)


def _round_namespace_order(namespace: str) -> tuple[int, str]:
    tail = namespace.rsplit("/", 1)[-1]
    return (int(tail), namespace) if tail.isdigit() else (2**31 - 1, namespace)


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RecordError(f"field settlement receipt {key} must be a non-empty string")
    return value


def _required_int(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RecordError(f"field settlement receipt {key} must be a non-negative integer")
    return value


def _validate_optional_number(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise RecordError(f"field settlement receipt {name} must be finite or null")
