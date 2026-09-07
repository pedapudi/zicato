"""Durable field-tournament snapshots and their shared JSON acceptance rules."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.storage import atomic_write_json
from zicato.workspace import WorkspaceLayout


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
            champion_generation_id=self.champion_generation_id,
            promoted_generation_id=self.promoted_generation_id,
        )
        if "state" in body or self.state != "settled":
            body["state"] = self.state
        return body


def decode_field_tournament_record(value: Any) -> FieldTournamentRecord:
    """Accept snapshots, including unstamped settled records without ``state``.

    The field snapshot predates the explicit live/settled state. An absent
    state retains its settled meaning and stays omitted when written again.
    Format-3 receipts independently require an explicit settled payload.
    """
    if not isinstance(value, dict):
        raise RecordError("field tournament record must be an object")
    for key in ("tournament_id", "epoch_id", "structure", "ran_at", "champion_generation_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RecordError(f"field tournament record has invalid {key}")
    state = value.get("state", "settled")
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
    """Read one strict canonical snapshot; absence remains FileNotFoundError."""
    try:
        return decode_field_tournament_record(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise RecordError(f"field tournament record {path}: {exc}") from exc


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
    """Build one field-tournament snapshot without writing it.

    A two-competitor gauntlet has a canonical duel record already, so it
    does not create a separate field snapshot.
    """
    if len(competitors) < 3:
        return None
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
