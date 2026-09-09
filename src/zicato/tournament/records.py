"""Durable tournament records and their shared JSON acceptance rules."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.storage import atomic_write_json
from zicato.workspace import WorkspaceLayout

HARNESS_LOAD_SCHEMA = "zicato.harness_load/1"


def decode_harness_load(value: Any, *, generation_id: str) -> dict[str, Any]:
    """Accept records of loaded source files, retaining extensions and historical omissions."""
    if not isinstance(value, dict) or value.get("schema") != HARNESS_LOAD_SCHEMA:
        raise RecordError("source record has an invalid schema")
    if value.get("generation_id") != generation_id:
        raise RecordError("source record has a different generation")
    if not isinstance(value.get("entrypoint_file"), str):
        raise RecordError("source entrypoint must be text")
    for key in ("trees_verified", "trees_never_imported"):
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(name, str) or not name for name in value[key])
        ):
            raise RecordError(f"source {key} must contain tree names")
    if "implementation" in value:
        implementation = value["implementation"]
        if not isinstance(implementation, dict) or not isinstance(
            implementation.get("factory_spec"), str
        ):
            raise RecordError("source implementation requires a factory specification")
        for key in ("factory_file", "factory_source_sha256"):
            if key not in implementation or (
                implementation[key] is not None and not isinstance(implementation[key], str)
            ):
                raise RecordError(f"source implementation has invalid {key}")
        modules = implementation.get("candidate_modules")
        if not isinstance(modules, dict) or any(
            not isinstance(name, str) or not isinstance(path, str) for name, path in modules.items()
        ):
            raise RecordError("source candidate modules must map names to paths")
    return value


def read_harness_load(path: Path, *, generation_id: str) -> dict[str, Any] | None:
    """Read a source record; only an absent file has no provenance."""
    try:
        return decode_harness_load(
            json.loads(path.read_text(encoding="utf-8")), generation_id=generation_id
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecordError) as exc:
        raise RecordError(f"source record {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class FieldTournamentRecord:
    """Accepted tournament facts with a detached, lossless stored representation.

    Nested bracket and override payloads are retained as JSON text so callers
    cannot mutate the accepted record or normalize optional keys and numbers.
    ``to_dict`` returns a fresh projection for existing JSON consumers.
    """

    tournament_id: str
    epoch_id: str
    state: str
    champion_generation_id: str
    promoted_generation_id: str
    _json: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self._json)
        body.update(
            tournament_id=self.tournament_id,
            epoch_id=self.epoch_id,
            state=self.state,
            champion_generation_id=self.champion_generation_id,
            promoted_generation_id=self.promoted_generation_id,
        )
        return body


def decode_field_tournament_record(value: Any) -> FieldTournamentRecord:
    """Accept a snapshot with an explicit in-progress or settled state."""
    if not isinstance(value, dict):
        raise RecordError("field tournament record must be an object")
    for key in ("tournament_id", "epoch_id", "structure", "ran_at", "champion_generation_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RecordError(f"field tournament record has invalid {key}")
    state = value.get("state")
    if not isinstance(state, str) or state not in {"in_progress", "settled"}:
        raise RecordError("field tournament record has invalid state")
    for key in ("promoted_generation_id", "decision", "reason"):
        if not isinstance(value.get(key), str):
            raise RecordError(f"field tournament record has invalid {key}")
    if not isinstance(value.get("structure_params"), dict):
        raise RecordError("field tournament record has invalid structure_params")
    for key in ("competitors", "rounds", "standings", "field_status"):
        rows = value.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RecordError(f"field tournament record has invalid {key}")
    competitors = value["competitors"]
    ids = [row.get("generation_id") for row in competitors]
    if any(not isinstance(gid, str) or not gid for gid in ids) or len(ids) != len(set(ids)):
        raise RecordError("field tournament record has invalid competitor identities")
    if any(
        not isinstance(row.get("role"), str) or row["role"] not in {"champion", "challenger"}
        for row in competitors
    ):
        raise RecordError("field tournament record has invalid competitor roles")
    champions = [row["generation_id"] for row in competitors if row["role"] == "champion"]
    if champions != [value["champion_generation_id"]]:
        raise RecordError("field tournament record has inconsistent incumbent")
    _validate_optional_number(value.get("delta_scalar"), "delta_scalar")
    if "promoted_generation_ids" in value:
        promoted = value["promoted_generation_ids"]
        if (
            not isinstance(promoted, list)
            or any(not isinstance(gid, str) or not gid for gid in promoted)
            or len(promoted) != len(set(promoted))
        ):
            raise RecordError("field tournament record has invalid promoted ids")
    if "override_status" in value and not isinstance(value["override_status"], dict):
        raise RecordError("field tournament record has invalid override_status")
    return FieldTournamentRecord(
        value["tournament_id"],
        value["epoch_id"],
        state,
        value["champion_generation_id"],
        value["promoted_generation_id"],
        json.dumps(value, allow_nan=False),
    )


def read_field_tournament_record(path: Path) -> FieldTournamentRecord:
    """Read a tournament, using its committed round as the result authority."""
    from zicato.epoch._storage import experiment_key
    from zicato.epoch.settlement_receipt import read_settlement_receipt
    from zicato.storage import workspace_backend

    try:
        record = decode_field_tournament_record(json.loads(path.read_text(encoding="utf-8")))
        root = path.parents[3]
        first_id = path.stem.removeprefix("field-")
        experiment = workspace_backend(root, start=False).read_json(
            experiment_key(record.epoch_id, first_id)
        )
        if experiment is not None:
            receipt = read_settlement_receipt(root, record.epoch_id, experiment["round_index"])
            if (
                receipt is not None
                and receipt.state == "committed"
                and receipt.field_record is not None
            ):
                settled = decode_field_tournament_record(receipt.field_record)
                if settled.tournament_id != record.tournament_id:
                    raise RecordError("round result names a different tournament")
                return settled
        return record
    except FileNotFoundError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise RecordError(f"field tournament record {path}: {exc}") from exc


def field_tournament_records(
    workspace_root: Path, epoch_id: str
) -> tuple[FieldTournamentRecord, ...]:
    """Read completed structures from rounds and include tournaments still in progress."""
    from zicato.core.workspace import field_tournaments_dir
    from zicato.epoch.settlement_receipt import iter_settlement_receipts

    records = {}
    for receipt in iter_settlement_receipts(workspace_root, epoch_id):
        if receipt.state == "committed" and receipt.field_record is not None:
            record = decode_field_tournament_record(receipt.field_record)
            records[record.tournament_id] = record
    for path in sorted(field_tournaments_dir(workspace_root, epoch_id).glob("field-*.json")):
        tournament_id = f"{epoch_id}:field:{path.stem.removeprefix('field-')}"
        if tournament_id not in records:
            record = read_field_tournament_record(path)
            records[record.tournament_id] = record
    return tuple(records.values())


def field_tournament_record(
    *,
    field_tournament_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    standings: list[dict[str, Any]],
    field_status: list[dict[str, Any]],
    decision: Any,
    ran_at: str,
    state: str = "settled",
    override_status: dict[str, dict[str, Any]] | None = None,
    promoted_generation_ids: list[str] | None = None,
) -> FieldTournamentRecord | None:
    """Record the executed structure, including a two-candidate tournament."""
    crowning_delta: float | None = None
    for r in reversed(rounds):
        matches = r.get("matches") or []
        if matches:
            crowning_delta = matches[-1].get("delta_scalar")
            break
    champion_id = next(
        (c.get("generation_id") for c in competitors if str(c.get("role", "")) == "champion"),
        "",
    )
    # ``decision`` is None while the round is still in flight (the envelope
    # is opened before the bracket resolves); the crowning fields stay empty
    # until settle. getattr tolerates the None case alongside the settled
    # TournamentDecision so the open + settle writes share one code path.
    record: dict[str, Any] = {
        "tournament_id": field_tournament_id,
        "epoch_id": epoch_id,
        "structure": structure,
        "structure_params": dict(structure_params),
        "competitors": [dict(c) for c in competitors],
        "rounds": rounds,
        "standings": standings,
        "crowning_matchup_id": getattr(decision, "crowning_matchup_id", None),
        "field_status": [dict(f) for f in field_status],
        "promoted_generation_id": getattr(decision, "promoted_generation_id", "") or "",
        "champion_generation_id": champion_id or "",
        "decision": getattr(decision, "decision", "") or "",
        "reason": getattr(decision, "reason", "") or "",
        "delta_scalar": crowning_delta,
        "state": state,
        "ran_at": ran_at,
    }
    # Optional provenance stays omitted when absent. Multiple promotions
    # retain the complete advanced set; each override retains its reason.
    if promoted_generation_ids:
        record["promoted_generation_ids"] = list(promoted_generation_ids)
    if override_status:
        record["override_status"] = {gid: dict(prov) for gid, prov in override_status.items()}
    return decode_field_tournament_record(record)


def write_field_tournament_record(
    workspace_root: Path,
    *,
    epoch_id: str,
    first_challenger_id: str,
    record: FieldTournamentRecord,
) -> None:
    """Atomically publish an accepted snapshot in its declared namespace."""
    record = decode_field_tournament_record(record.to_dict())
    if (
        record.epoch_id != epoch_id
        or record.tournament_id != f"{epoch_id}:field:{first_challenger_id}"
    ):
        raise RecordError("field tournament record conflicts with its storage namespace")
    atomic_write_json(
        WorkspaceLayout.from_root(workspace_root).field_tournament(epoch_id, first_challenger_id),
        record.to_dict(),
    )


def validate_settlement_field_record(
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
            raise RecordError("multi-challenger settlement receipt has no field tournament record")
        return None, next(iter(promoted_ids), None)
    try:
        raw_record = decode_field_tournament_record(raw_record).to_dict()
    except (ValueError, RuntimeError) as exc:
        raise RecordError(f"field settlement tournament record: {exc}") from exc
    expected_tournament_id = f"{epoch_id}:field:{first_challenger}"
    if raw_record.get("tournament_id") != expected_tournament_id:
        raise RecordError("field settlement tournament record has a different identity")
    if raw_record.get("epoch_id") != epoch_id or raw_record.get("state") != "settled":
        raise RecordError("field settlement tournament record has an invalid epoch or state")
    if raw_record["structure"] != structure:
        raise RecordError("field settlement tournament record has inconsistent structure")
    if raw_record.get("champion_generation_id") != parent_id:
        raise RecordError("field settlement tournament record names a different incumbent")

    competitors = raw_record.get("competitors")
    competitor_ids = [row.get("generation_id") for row in competitors]
    expected_ids = {parent_id, *candidate_ids}
    if set(competitor_ids) != expected_ids:
        raise RecordError("field settlement tournament record has inconsistent competitors")
    roles = {str(row["generation_id"]): row.get("role") for row in competitors}
    if roles.get(parent_id) != "champion" or any(
        roles.get(generation_id) != "challenger" for generation_id in candidate_ids
    ):
        raise RecordError("field settlement tournament record has inconsistent competitor roles")

    record_primary = raw_record.get("promoted_generation_id") or None
    if record_primary is not None and record_primary not in promoted_ids:
        raise RecordError("field settlement tournament record names an invalid primary champion")
    raw_promoted = raw_record.get("promoted_generation_ids")
    if raw_promoted is None:
        record_promoted = {record_primary} if record_primary is not None else set()
    else:
        record_promoted = set(raw_promoted)
    if record_promoted != promoted_ids:
        raise RecordError("field settlement tournament record has a different promoted set")
    if (record_primary is None) != (not promoted_ids):
        raise RecordError("field settlement tournament record lacks its primary champion")
    if raw_record["decision"] != decision:
        raise RecordError("field settlement tournament record has a different decision or reason")
    return raw_record, record_primary


def _validate_optional_number(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise RecordError(f"field settlement receipt {name} must be finite or null")
