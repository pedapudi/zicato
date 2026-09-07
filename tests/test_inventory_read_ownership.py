"""Recorded inventories retain valid data and refuse partial corruption."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from zicato.epoch._storage import RecordError
from zicato.query import WorkspacePaths
from zicato.workspace import WorkspaceLayout


@pytest.mark.parametrize("record", ["mutations.json", "contract_components.json"])
def test_inventory_absence_empty_extensions_and_atomic_refusal(tmp_path: Path, record: str) -> None:
    from zicato.epoch.contract import read_component_hashes, write_component_hashes
    from zicato.mutation.enumerator import enumerate_mutations
    from zicato.mutation.inventory import read_mutation_inventory, write_mutation_inventory

    path = tmp_path / record
    if record == "mutations.json":
        read, write, empty = read_mutation_inventory, write_mutation_inventory, []
        source = tmp_path / "source"
        source.mkdir()
        (source / "value.py").write_text('# zicato:mutable id="instruction"\nVALUE = "hello"\n')
        write(path, enumerate_mutations([source]))
        valid = read(path)
        valid[0]["extension"] = {"ratio": 1.0, "count": 1}
        invalid = [*valid, {"id": "incomplete"}]
        expected = json.dumps(valid, indent=2) + "\n"
    else:
        read, write, empty = read_component_hashes, write_component_hashes, {}
        valid = {"future_component": "opaque", "board": ""}
        invalid = {"board": 1}
        expected = json.dumps(valid, indent=2, sort_keys=True) + "\n"
    absent = tmp_path / "absent" / record
    assert read(absent) is None and not absent.parent.exists()
    write(path, empty)
    assert read(path) == empty
    write(path, valid)
    assert path.read_text() == expected
    assert read(path) == valid
    with pytest.raises(ValueError):
        write(path, invalid)
    assert path.read_text() == expected
    path.write_text(json.dumps(invalid))
    with pytest.raises(RecordError):
        read(path)


@pytest.mark.parametrize("body", ['{"board": 4}', '["board"]', "{"])
def test_contract_view_refuses_malformed_present_map(tmp_path: Path, body: str) -> None:
    from zicato.query.events_index import build_contract_diff

    layout = WorkspaceLayout.from_root(tmp_path)
    path = layout.contract_components("e1")
    path.parent.mkdir(parents=True)
    path.write_text(body)
    view = build_contract_diff(WorkspacePaths(tmp_path), "e1")
    assert "contract_components.json" in view.get("error", "")
    assert not any(row["current_hash"] for row in view["components"])
    assert not view["any_changed"]
    assert path.read_text() == body


@pytest.mark.parametrize("body", ['[{"id":"incomplete"}]', "[1]", "{"])
def test_mutation_consumers_refuse_malformed_present_inventory(tmp_path: Path, body: str) -> None:
    from zicato.analyzer.report_data import gather_epoch_report_data
    from zicato.query.epoch_view import build_epoch_view
    from zicato.query.mutation_view import build_mutation_index

    layout = WorkspaceLayout.from_root(tmp_path)
    path = layout.mutations("e1")
    path.parent.mkdir(parents=True)
    path.write_text(body)
    paths = WorkspacePaths(tmp_path)
    epoch = build_epoch_view(paths, "e1")
    assert epoch["mutations"] == []
    assert "mutations.json" in epoch.get("unreadable", "")
    index = build_mutation_index(paths, "e1")
    assert index["mutations"] == []
    assert "mutations.json:" in index.get("error", "")
    with pytest.raises(RecordError, match="mutations.json"):
        gather_epoch_report_data(tmp_path, "e1")
    assert path.read_text() == body


def test_auto_epoch_refuses_corrupt_components_before_publication(tmp_path: Path) -> None:
    from tests.test_auto_epoch import _aux_llm, _bootstrap, ensure_epoch_for_contract
    from zicato.epoch.lifecycle import current_epoch_id, list_epochs, load_epoch

    workspace, live = _bootstrap(tmp_path)
    epoch = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    layout = WorkspaceLayout.from_root(workspace)
    layout.contract_components(epoch).write_text('{"board": 4}')
    live["brief"].write_text("A changed brief.\n")
    with pytest.raises(RecordError, match="contract_components.json"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))
    assert current_epoch_id(workspace) == epoch
    assert [row.id for row in list_epochs(workspace)] == [epoch]
    assert not load_epoch(workspace, epoch).closed
    assert not (workspace / "epoch_publication.json").exists()
