"""Direct format acceptance and immutable receipt facts without tournament setup."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests._workspace_support import experiment_record
from zicato.core.types import Experiment, HypothesisSpec
from zicato.epoch.lineage import decode_lineage
from zicato.epoch.settlement_receipt import (
    decode_settlement_receipt,
    field_settlement_intent_path,
    read_settlement_receipt,
    settlement_index_repair_required,
    validate_settlement_records,
    write_settlement_receipt,
)
from zicato.tournament.records import (
    decode_field_tournament_record,
    read_field_tournament_record,
    write_field_tournament_record,
)

# The receipt carries a complete outcome. Integer deltas retain their JSON type.
REJECTED_RECEIPT = {
    "format_version": 3,
    "state": "pending",
    "settlement_id": "0123456789abcdef0123456789abcdef",
    "epoch_id": "epoch",
    "round_index": 0,
    "primary_promoted_generation_id": None,
    "candidates": [
        {
            "experiment_id": "experiment",
            "generation_id": "v1",
            "created_at": "2026-06-01T00:00:00Z",
            "parent_scalar": 0,
            "child_scalar": 1.0,
            "outcome": experiment_record(
                "v1",
                epoch_id="epoch",
                outcome={
                    "ran_at": "2026-06-01T00:01:00Z",
                    "tournament_decision": "rejected",
                    "rejection_reason": "higher loss",
                    "scalar_score_delta": 1,
                    "structure": "gauntlet",
                    "evidence": {"draws": [{"draw": 0, "eligible": False}]},
                },
            )["outcome"],
        }
    ],
    "field_tournament_record": None,
    "index_projection": {"state": "pending", "error_type": ""},
    "promotion_hook": {"state": "not_applicable", "adapter_name": "", "failure_type": ""},
}


def _receipt() -> dict:
    return json.loads(json.dumps(REJECTED_RECEIPT))


def test_receipt_bytes_and_nested_facts_survive_decode_and_write(tmp_path: Path) -> None:
    raw = _receipt()
    expected = json.dumps(raw, indent=2, sort_keys=True).encode()
    receipt = decode_settlement_receipt(raw)
    raw["candidates"][0]["outcome"]["evidence"]["draws"].clear()
    projection = receipt.to_dict()
    projection["candidates"][0]["outcome"]["evidence"]["draws"].clear()
    outcome = receipt.candidates[0].outcome
    assert outcome.evidence is not None
    outcome.evidence["draws"].clear()
    assert receipt.to_dict() == REJECTED_RECEIPT
    assert type(receipt.candidates[0].parent_scalar) is int
    with pytest.raises(FrozenInstanceError):
        receipt.state = "committed"  # type: ignore[misc]
    write_settlement_receipt(tmp_path, receipt)
    assert field_settlement_intent_path(tmp_path, "epoch", 0).read_bytes() == expected
    stored = read_settlement_receipt(tmp_path, "epoch", 0)
    assert stored is not None and stored.to_dict() == REJECTED_RECEIPT


@pytest.mark.parametrize("case", ["promotion", "multiple_promotions", "projection_failure"])
def test_receipt_preserves_promotion_override_and_failure_bytes(tmp_path: Path, case: str) -> None:
    raw = _receipt()
    raw["primary_promoted_generation_id"] = "v1"
    raw["candidates"][0]["outcome"]["tournament_decision"] = "promoted"
    raw["candidates"][0]["outcome"]["rejection_reason"] = ""
    raw["promotion_hook"] = {"state": "pending", "adapter_name": "target", "failure_type": ""}
    if case == "multiple_promotions":
        sibling = json.loads(json.dumps(raw["candidates"][0]))
        sibling.update(experiment_id="sibling", generation_id="v2")
        sibling["outcome"].update(operator_override=True, operator_override_reason="tradeoff")
        raw["candidates"].append(sibling)
        raw["field_tournament_record"] = {
            "tournament_id": "epoch:field:v1",
            "epoch_id": "epoch",
            "structure": "gauntlet",
            "structure_params": {"field_size": 2},
            "competitors": [
                {"generation_id": "v0", "role": "champion"},
                {"generation_id": "v1", "role": "challenger"},
                {"generation_id": "v2", "role": "challenger"},
            ],
            "rounds": [],
            "standings": [],
            "field_status": [],
            "promoted_generation_id": "v1",
            "champion_generation_id": "v0",
            "decision": "promoted",
            "reason": "",
            "delta_scalar": 0,
            "ran_at": "2026-06-01T00:01:00Z",
            "state": "settled",
            "promoted_generation_ids": ["v1", "v2"],
            "override_status": {"v2": {"reason": "tradeoff", "action": "promote"}},
        }
    if case == "projection_failure":
        raw["state"] = "committed"
        raw["index_projection"] = {"state": "repair_required", "error_type": "OSError"}
    expected = json.dumps(raw, indent=2, sort_keys=True).encode()
    write_settlement_receipt(tmp_path, decode_settlement_receipt(raw))
    assert field_settlement_intent_path(tmp_path, "epoch", 0).read_bytes() == expected


@pytest.mark.parametrize("version", [None, True, 3.0, 2, 4, "3"])
def test_receipt_requires_explicit_integer_format(version: object) -> None:
    raw = _receipt()
    if version is None:
        del raw["format_version"]
    else:
        raw["format_version"] = version
    with pytest.raises(RuntimeError, match="unsupported format_version"):
        decode_settlement_receipt(raw)


@pytest.mark.parametrize("number", [True, float("nan"), float("inf"), "0"])
def test_receipt_refuses_invalid_scalars_without_workspace(number: object) -> None:
    raw = _receipt()
    raw["candidates"][0]["parent_scalar"] = number
    with pytest.raises(RuntimeError, match="parent_scalar must be finite"):
        decode_settlement_receipt(raw)


@pytest.mark.parametrize("field", ["state", "promotion_hook", "index_projection"])
def test_receipt_malformed_state_is_a_record_error(field: str) -> None:
    from zicato.epoch._storage import RecordError

    raw = _receipt()
    if field == "state":
        raw[field] = []
    else:
        raw[field]["state"] = []
    with pytest.raises(RecordError, match="state"):
        decode_settlement_receipt(raw)


def test_decode_refuses_duplicate_candidate_and_namespace_conflicts() -> None:
    raw = _receipt()
    raw["candidates"].append(raw["candidates"][0])
    with pytest.raises(RuntimeError, match="duplicate generation"):
        decode_settlement_receipt(raw)
    with pytest.raises(RuntimeError, match="inside 'different'"):
        decode_settlement_receipt(_receipt(), expected_epoch_id="different")
    with pytest.raises(RuntimeError, match="inside round 1"):
        decode_settlement_receipt(_receipt(), expected_round_index=1)


def test_index_inspection_refuses_malformed_claim_of_completed_repair(tmp_path: Path) -> None:
    path = field_settlement_intent_path(tmp_path, "epoch", 0)
    path.parent.mkdir(parents=True)
    path.write_text('{"state":"committed","index_projection":{"state":"repair_required"}}')
    with pytest.raises(RuntimeError, match="unsupported format_version"):
        settlement_index_repair_required(tmp_path)


def test_receipt_absence_and_corruption_remain_distinct(tmp_path: Path) -> None:
    assert read_settlement_receipt(tmp_path, "epoch", 0) is None
    path = field_settlement_intent_path(tmp_path, "epoch", 0)
    path.parent.mkdir(parents=True)
    path.write_text('{"format_version":')
    with pytest.raises(RuntimeError, match="epochs/epoch/rounds/0/field_settlement.json"):
        read_settlement_receipt(tmp_path, "epoch", 0)


def test_pure_cross_record_checks_preserve_unresolved_lineage() -> None:
    receipt = decode_settlement_receipt(_receipt())
    experiment = Experiment(
        id="experiment",
        epoch_id="epoch",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-06-01T00:00:00Z",
        hypothesis=HypothesisSpec("Reduce loss", (), "Measured difference", (), "unchanged"),
        patches=(),
        outcome=None,
        round_index=0,
    )
    lineage = {
        "format_version": 1,
        "epochs": [
            {
                "id": "epoch",
                "generations": [
                    {
                        "id": "v1",
                        "parent_id": "v0",
                        "created_at": "2026-06-01T00:00:00Z",
                        "round_index": 0,
                        "promoted": None,
                    }
                ],
            }
        ],
    }
    before = json.dumps(lineage)
    assert (
        validate_settlement_records(
            receipt,
            experiments=(experiment,),
            lineage=decode_lineage(lineage),
            field_record=None,
            current_generation="v0",
        )
        == "v0"
    )
    assert json.dumps(lineage) == before
    lineage["epochs"][0]["generations"][0]["round_index"] = 1
    with pytest.raises(RuntimeError, match="conflicts on round_index"):
        validate_settlement_records(
            receipt,
            experiments=(experiment,),
            lineage=decode_lineage(lineage),
            field_record=None,
            current_generation="v0",
        )


def test_field_readers_expose_corruption_instead_of_dropping_records(tmp_path: Path) -> None:
    from zicato.epoch._storage import RecordError
    from zicato.index.ingest import _load_field_tournaments
    from zicato.query.paths import WorkspacePaths
    from zicato.query.tournament_view import _enrich_override_status

    path = tmp_path / "epochs/epoch/tournaments/field-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    paths = WorkspacePaths(tmp_path)
    with pytest.raises(RecordError, match="field-v1.json"):
        _load_field_tournaments(tmp_path, "epoch")
    with pytest.raises(RecordError, match="field-v1.json"):
        read_field_tournament_record(path)
    projected = _enrich_override_status(paths, "epoch", "epoch:field:v1", {})
    assert "field-v1.json" in projected["unreadable"]


@pytest.mark.parametrize("state", ["in_progress", "settled", "omitted", None, True, "pending"])
def test_tournament_snapshot_requires_state_and_preserves_optional_keys(
    tmp_path: Path, state: object
) -> None:
    raw = {
        "tournament_id": "epoch:field:v1",
        "epoch_id": "epoch",
        "structure": "swiss",
        "structure_params": {"field_size": 2},
        "competitors": [
            {"generation_id": "v0", "role": "champion"},
            {"generation_id": "v1", "role": "challenger"},
            {"generation_id": "v2", "role": "challenger"},
        ],
        "rounds": [],
        "standings": [],
        "field_status": [],
        "promoted_generation_id": "",
        "champion_generation_id": "v0",
        "decision": "rejected",
        "reason": "higher loss",
        "delta_scalar": 0,
        "ran_at": "2026-06-01T00:01:00Z",
    }
    if state != "omitted":
        raw["state"] = state
    if state not in ("in_progress", "settled"):
        from zicato.epoch._storage import RecordError

        with pytest.raises(RecordError, match="invalid state"):
            decode_field_tournament_record(raw)
        return
    if state == "in_progress":
        raw["decision"] = ""
        raw["reason"] = ""
    expected = json.dumps(raw, indent=2, sort_keys=True).encode()
    record = decode_field_tournament_record(raw)
    raw["competitors"].clear()
    record.to_dict()["competitors"].clear()
    write_field_tournament_record(
        tmp_path, epoch_id="epoch", first_challenger_id="v1", record=record
    )
    path = tmp_path / "epochs/epoch/tournaments/field-v1.json"
    assert path.read_bytes() == expected
    assert read_field_tournament_record(path).state == state
