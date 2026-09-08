"""A dossier compares parent identities before composing scientific evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._workspace_support import workspace, write_epoch
from tests.test_settlement_receipt_records import REJECTED_RECEIPT
from zicato.core.types import Experiment, HypothesisSpec
from zicato.epoch._storage import RecordError
from zicato.epoch.journal import write_experiment
from zicato.epoch.lineage import decode_lineage, write_lineage
from zicato.epoch.settlement_receipt import (
    decode_settlement_receipt,
    field_settlement_intent_path,
    read_settlement_receipt,
    scan_field_settlement_receipts,
    write_settlement_receipt,
)
from zicato.query import candidate_view
from zicato.query.paths import WorkspacePaths


def _workspace(
    tmp_path: Path,
    *,
    recorded_parent: str | None,
    experiment_parent: str | None,
    generation_id: str = "v1",
) -> WorkspacePaths:
    layout = workspace(tmp_path)
    write_epoch(layout, "selected", current=True)
    for gid, parent in {"v0": None, generation_id: experiment_parent}.items():
        write_experiment(
            layout.root,
            "selected",
            gid,
            Experiment(
                id="experiment" if gid == generation_id else "baseline",
                epoch_id="selected",
                generation_id=gid,
                parent_generation_id=parent,
                proposed_at="2026-06-01T00:00:00Z",
                hypothesis=HypothesisSpec(
                    core_idea="Reduce loss",
                    modulating=(),
                    why="Measured difference",
                    expected_pass_rate_delta="unchanged",
                ),
                patches=(),
                outcome=None,
                round_index=0,
            ),
        )
    generations = [{"id": generation_id, "parent_id": recorded_parent, "promoted": None}]
    if generation_id != "v0":
        generations.insert(0, {"id": "v0", "parent_id": None, "promoted": True})
    epoch = {"id": "selected", "generations": generations}
    if generation_id == "v0" and recorded_parent and ":" in recorded_parent:
        epoch["v0_parent"] = recorded_parent
    write_lineage(layout.root, decode_lineage({"format_version": 1, "epochs": [epoch]}))
    return WorkspacePaths(layout.root)


def test_contradictory_parent_refuses_comparison_before_gate_reads(tmp_path, monkeypatch):
    paths = _workspace(tmp_path, recorded_parent="v9", experiment_parent="v0")

    def unexpected_gate(*args, **kwargs):
        pytest.fail("a contradictory parent reached gate composition")

    monkeypatch.setattr(candidate_view, "build_gate_breakdown", unexpected_gate)
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v1")
    assert dossier["found"] is True
    assert "selected:v9" in dossier["parent_inconsistency"]
    assert "selected:v0" in dossier["parent_inconsistency"]
    assert dossier["parent"] is None and dossier["parent_epoch_id"] is None
    assert dossier["gates"] == []
    assert (
        dossier["matchup_grid"] is dossier["comparison"] is dossier["hypothesis_accuracy"] is None
    )
    assert dossier["generation"]["parent_generation_id"] == "v9"
    assert dossier["experiment"]["parent_generation_id"] == "v0"


def test_explicit_and_local_parent_coordinates_are_equivalent(tmp_path):
    paths = _workspace(tmp_path, recorded_parent="selected:v0", experiment_parent="v0")
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v1")
    assert dossier["parent_inconsistency"] is None
    assert dossier["parent"] == "v0" and dossier["parent_epoch_id"] == "selected"
    assert dossier["gates"][0]["champion"] == "v0"


def test_external_baseline_ancestry_is_not_a_cross_contract_gate(tmp_path):
    paths = _workspace(
        tmp_path, recorded_parent="source:v7", experiment_parent=None, generation_id="v0"
    )
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v0")
    assert dossier["parent_inconsistency"] is None
    assert dossier["parent"] == "source:v7" and dossier["parent_epoch_id"] == "source"
    assert dossier["relatives"][0]["epoch_id"] == "source"
    assert dossier["relatives"][0]["generation_id"] == "v7"
    assert dossier["gates"] == [] and dossier["matchup_grid"] is None


def test_different_epochs_with_same_generation_are_distinct_parents(tmp_path):
    paths = _workspace(tmp_path, recorded_parent="source:v0", experiment_parent="selected:v0")
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v1")
    assert "source:v0" in dossier["parent_inconsistency"]
    assert "selected:v0" in dossier["parent_inconsistency"]


def test_receipt_incumbent_must_agree_with_candidate_parent(tmp_path):
    paths = _workspace(tmp_path, recorded_parent="v0", experiment_parent="v0")
    body = json.loads(json.dumps(REJECTED_RECEIPT))
    body["epoch_id"] = "selected"
    body["candidates"][0]["outcome"]["structure"] = "swiss"
    body["field_tournament_record"] = {
        "tournament_id": "selected:field:v1",
        "epoch_id": "selected",
        "state": "settled",
        "structure": "swiss",
        "structure_params": {},
        "ran_at": "2026-06-01T00:01:00Z",
        "champion_generation_id": "v9",
        "promoted_generation_id": "",
        "decision": "rejected",
        "reason": "higher loss",
        "delta_scalar": 1,
        "competitors": [
            {"generation_id": "v9", "role": "champion"},
            {"generation_id": "v1", "role": "challenger"},
        ],
        "rounds": [],
        "standings": [],
        "field_status": [],
    }
    write_settlement_receipt(paths.root, decode_settlement_receipt(body))
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v1")
    assert "settlement incumbent declares selected:v9" in dossier["parent_inconsistency"]
    assert dossier["gates"] == []


def test_null_receipt_is_a_visible_refusal_in_owner_scan_and_dossier(tmp_path):
    paths = _workspace(tmp_path, recorded_parent="v0", experiment_parent="v0")
    path = field_settlement_intent_path(paths.root, "selected", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("null", encoding="utf-8")
    with pytest.raises(RecordError, match="JSON object"):
        read_settlement_receipt(paths.root, "selected", 0)
    receipts, errors = scan_field_settlement_receipts(paths.root, "selected")
    assert receipts == () and len(errors) == 1
    dossier = candidate_view.build_candidate_dossier(paths, "selected", "v1")
    assert "JSON object" in dossier["parent_inconsistency"]
    assert dossier["gates"] == []
