"""Typed record edits remain authoritative while historical JSON stays lossless."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_settlement_receipt_records import REJECTED_RECEIPT
from zicato.epoch._storage import RecordError
from zicato.epoch.lineage import decode_lineage, write_lineage
from zicato.epoch.settlement_receipt import (
    IndexProjection,
    decode_settlement_receipt,
    field_settlement_intent_path,
    write_settlement_receipt,
)
from zicato.reflection.plan import ReflectionPlan, write_plan
from zicato.runtime.state import ActiveRun
from zicato.selection.dead_letter import InconclusiveRecord, record_inconclusive
from zicato.tournament.records import decode_field_tournament_record, write_field_tournament_record
from zicato.tournament.scoring import ScoreMeasurement, decode_gen_score, write_gen_score
from zicato.workspace.layout import WorkspaceLayout


def test_nested_lineage_edits_reach_canonical_bytes(tmp_path: Path) -> None:
    body = {
        "epochs": [{"id": "epoch", "generations": [{"id": "v0", "child_scalar": 1}]}],
        "extension": {"count": 0, "weight": 0.0},
    }
    graph = decode_lineage(body)
    epoch = graph.epochs[0]
    generation = replace(epoch.generations[0], promoted=False, child_scalar=1.0)
    edited = replace(graph, epochs=(replace(epoch, name="named", generations=(generation,)),))
    expected = json.loads(json.dumps(body))
    expected["epochs"][0].update(name="named")
    expected["epochs"][0]["generations"][0].update(promoted=False, child_scalar=1.0)
    write_lineage(tmp_path, edited)
    assert (tmp_path / "lineage.json").read_bytes() == json.dumps(
        expected, indent=2, sort_keys=True
    ).encode()
    revision_path = WorkspaceLayout.from_root(tmp_path).index_revision("epoch")
    revision = revision_path.read_bytes()
    integer = replace(generation, child_scalar=1)
    write_lineage(
        tmp_path, replace(edited, epochs=(replace(edited.epochs[0], generations=(integer,)),))
    )
    assert revision_path.read_bytes() != revision
    assert graph.to_dict() == body
    assert replace(graph, epochs=()).to_dict() == dict(body, epochs=[])


def test_score_and_measurement_edits_reach_history_and_flat_file(tmp_path: Path) -> None:
    score = decode_gen_score({"scalar": 1, "extension": {"value": 0}})
    edited = replace(score, scalar=1.0, mean_score=0.5)
    measurement = replace(ScoreMeasurement(edited, 0, None), round_index=2)
    assert measurement.to_dict() == {
        "scalar": 1.0,
        "extension": {"value": 0},
        "mean_score": 0.5,
        "seq": 0,
        "round_index": 2,
    }
    write_gen_score(tmp_path, "epoch", "v1", edited.to_dict(), round_index=2)
    layout = WorkspaceLayout.from_root(tmp_path)
    stored = json.loads(layout.gen_score("epoch", "v1").read_text())
    assert type(stored["scalar"]) is float
    assert stored["mean_score"] == 0.5
    assert "drift_loss_mean" not in stored
    assert score.to_dict() == {"scalar": 1, "extension": {"value": 0}}


def test_receipt_edits_compose_candidates_and_progress_then_revalidate(tmp_path: Path) -> None:
    body = json.loads(json.dumps(REJECTED_RECEIPT))
    del body["candidates"][0]["parent_scalar"]
    body["candidates"][0]["extension"] = {"count": 0}
    body["promotion_hook"]["extension"] = 1
    body["index_projection"]["extension"] = 1.0
    receipt = decode_settlement_receipt(body)
    assert json.dumps(receipt.to_dict(), sort_keys=True) == json.dumps(body, sort_keys=True)
    candidate = replace(receipt.candidates[0], child_scalar=2.0)
    edited = replace(
        receipt,
        state="committed",
        candidates=(candidate,),
        index_projection=IndexProjection("succeeded", ""),
    )
    expected = json.loads(json.dumps(body))
    expected["state"] = "committed"
    expected["candidates"][0]["child_scalar"] = 2.0
    expected["index_projection"]["state"] = "succeeded"
    write_settlement_receipt(tmp_path, edited)
    path = field_settlement_intent_path(tmp_path, "epoch", 0)
    assert path.read_bytes() == json.dumps(expected, indent=2, sort_keys=True).encode()
    written = path.read_bytes()
    with pytest.raises(RecordError, match="pending index"):
        write_settlement_receipt(tmp_path, replace(receipt, state="committed"))
    assert path.read_bytes() == written
    assert receipt.to_dict() == body


def test_tournament_edits_preserve_omission_and_refuse_inconsistent_identity(
    tmp_path: Path,
) -> None:
    body = {
        "tournament_id": "epoch:field:v1",
        "epoch_id": "epoch",
        "structure": "swiss",
        "structure_params": {},
        "ran_at": "2026-06-01",
        "champion_generation_id": "v0",
        "promoted_generation_id": "",
        "decision": "rejected",
        "reason": "higher loss",
        "delta_scalar": 0,
        "competitors": [{"generation_id": "v0", "role": "champion"}],
        "rounds": [],
        "standings": [],
        "field_status": [],
    }
    record = decode_field_tournament_record(body)
    assert record.to_dict() == body and "state" not in record.to_dict()
    edited = replace(record, state="in_progress")
    write_field_tournament_record(
        tmp_path, epoch_id="epoch", first_challenger_id="v1", record=edited
    )
    path = WorkspaceLayout.from_root(tmp_path).field_tournament("epoch", "v1")
    assert json.loads(path.read_text())["state"] == "in_progress"
    written = path.read_bytes()
    with pytest.raises(RecordError, match="incumbent"):
        write_field_tournament_record(
            tmp_path,
            epoch_id="epoch",
            first_challenger_id="v1",
            record=replace(record, champion_generation_id="v9"),
        )
    assert path.read_bytes() == written


def test_active_run_already_serializes_replaced_process_identity() -> None:
    run = ActiveRun("run", 1, "start", "progress", 10, "deadline", "events", "entry", "v1", "epoch")
    edited = replace(run, pid=2, producer_pid=3, producer_start_time=4.0)
    assert edited.to_dict()["pid"] == 2
    assert edited.to_dict()["producer_pid"] == 3
    assert edited.to_dict()["producer_start_time"] == 4.0
    assert "producer_pid" not in run.to_dict()


def test_plan_edit_preserves_unrelated_historical_omissions(tmp_path: Path) -> None:
    body = {
        "format_version": 1,
        "reflection_id": "reflection",
        "epoch_id": "epoch",
        "replicates": 1,
    }
    plan = ReflectionPlan.from_json(body)
    edited = replace(plan, replicates=2, executed=True)
    path = write_plan(tmp_path, edited)
    assert json.loads(path.read_text()) == dict(body, replicates=2, executed=True)
    assert plan.to_json() == body


def test_inconclusive_edit_preserves_evidence_and_extensions(tmp_path: Path) -> None:
    body = {
        "generation_id": "v1",
        "champion_id": "v0",
        "epoch_id": "epoch",
        "rating": {},
        "ci_history": [],
        "reason": "unresolved",
        "extension": {"integer": 1, "decimal": 1.0},
    }
    record = InconclusiveRecord.from_json(body)
    path = record_inconclusive(tmp_path, replace(record, reason="still unresolved"))
    assert (
        path.read_bytes()
        == json.dumps(dict(body, reason="still unresolved"), indent=2, sort_keys=True).encode()
    )
    assert record.to_json() == body
