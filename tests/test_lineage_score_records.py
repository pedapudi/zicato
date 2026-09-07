"""Canonical acceptance, byte compatibility, and interrupted publication boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.epoch._storage import RecordError
from zicato.epoch.lineage import decode_lineage, initialize_lineage, load_lineage, write_lineage
from zicato.tournament.scoring import read_gen_score, read_gen_score_history, write_gen_score
from zicato.workspace.layout import WorkspaceLayout
from zicato.workspace.projection import epoch_revisions


def test_lineage_preserves_omissions_zero_and_cross_epoch_parent(tmp_path: Path) -> None:
    body = {
        "epochs": [
            {
                "id": "epoch",
                "v0_parent": "source:v7",
                "generations": [
                    {"id": "v0", "parent_id": "source:v7", "promoted": True},
                    {
                        "id": "v1",
                        "parent_id": "v0",
                        "promoted": None,
                        "parent_scalar": 0,
                        "child_scalar": 0.0,
                        "delta_scalar": None,
                    },
                ],
            }
        ],
    }
    expected = json.dumps(body, indent=2, sort_keys=True).encode()
    graph = decode_lineage(body)
    epoch = graph.epoch("epoch")
    assert epoch is not None and epoch.parent_epoch_id == "source"
    assert epoch.generation("v0").parent_id == "source:v7"
    assert epoch.generation("v1").promoted is None
    write_lineage(tmp_path, graph)
    assert (tmp_path / "lineage.json").read_bytes() == expected
    assert type(load_lineage(tmp_path).epoch("epoch").generation("v1").parent_scalar) is int
    body["epochs"][0]["generations"].clear()
    projection = graph.to_dict()
    projection["epochs"].clear()
    assert len(graph.epoch("epoch").generations) == 2
    revision = WorkspaceLayout.from_root(tmp_path).index_revision("epoch").read_bytes()
    write_lineage(tmp_path, graph)
    assert WorkspaceLayout.from_root(tmp_path).index_revision("epoch").read_bytes() == revision


def test_empty_lineage_and_absence_remain_distinct(tmp_path: Path) -> None:
    assert not load_lineage(tmp_path).exists
    assert not tmp_path.joinpath("lineage.json").exists()
    initialize_lineage(tmp_path)
    assert load_lineage(tmp_path).exists
    assert (tmp_path / "lineage.json").read_bytes() == b'{\n  "epochs": []\n}\n'


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "[]",
        "{}",
        '{"epochs": null}',
        '{"epochs": [{"id": "epoch", "generations": ["v0"]}]}',
        '{"epochs": [{"id": "epoch", "generations": [{"id": "v1", "promoted": 0}]}]}',
        '{"epochs": [{"id": "epoch", "generations": [{"id": "v1", "round_index": true}]}]}',
        '{"epochs": [{"id": "epoch", "generations": [{"id": "v1", "parent_scalar": NaN}]}]}',
        '{"epochs": [{"id": "epoch", "generations": [{"id": "v1"}, {"id": "v1"}]}]}',
    ],
)
def test_malformed_lineage_is_never_replaced(tmp_path: Path, text: str) -> None:
    path = tmp_path / "lineage.json"
    path.write_text(text)
    with pytest.raises(RecordError):
        load_lineage(tmp_path)
    with pytest.raises(RecordError):
        initialize_lineage(tmp_path)
    assert path.read_text() == text


def test_lineage_revision_failure_prevents_canonical_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    write_lineage(tmp_path, decode_lineage({"epochs": [{"id": "epoch", "generations": []}]}))
    path = tmp_path / "lineage.json"
    before = path.read_bytes()

    def fail_mark(root: Path, epoch_id: str) -> None:
        assert root == tmp_path and epoch_id == "epoch"
        assert path.read_bytes() == before
        raise OSError("revision unavailable")

    monkeypatch.setattr("zicato.epoch.lineage.mark_epoch_changed", fail_mark)
    with pytest.raises(OSError, match="revision unavailable"):
        write_lineage(tmp_path, decode_lineage({"epochs": []}))
    assert path.read_bytes() == before


def test_score_and_history_preserve_written_bytes(tmp_path: Path) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    aggregate = {
        "scalar": 0,
        "mean_score": 0.0,
        "per_entry": {
            "task": {"drift_loss": 0, "pass_fail": None, "score": None},
        },
    }
    write_gen_score(tmp_path, "epoch", "v0", aggregate)
    revisions = epoch_revisions(tmp_path)
    assert revisions.keys() == {"epoch"}
    payload = {**aggregate, "generation_id": "v0"}
    history = {**payload, "round_index": None, "seq": 0}
    assert (
        layout.gen_score("epoch", "v0").read_bytes()
        == (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    )
    assert (
        layout.gen_score_history("epoch", "v0").read_bytes()
        == (json.dumps(history, sort_keys=True) + "\n").encode()
    )
    score = read_gen_score(layout, "epoch", "v0")
    assert score is not None and type(score.scalar) is int
    assert "pass_rate" not in score.to_dict()
    score.to_dict()["per_entry"].clear()
    assert score.to_dict()["per_entry"]
    assert epoch_revisions(tmp_path) == revisions


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "[]",
        "{}",
        '{"scalar": true}',
        '{"scalar": NaN}',
        '{"scalar": 0, "generation_id": "v2"}',
        '{"scalar": 0, "entry_count": 1.5}',
        '{"scalar": 0, "per_entry": []}',
        '{"scalar": 0, "per_entry": {"task": {"pass_fail": 1}}}',
        '{"format_version": 2, "scalar": 0}',
    ],
)
def test_malformed_score_blocks_replacement(tmp_path: Path, text: str) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    path = layout.gen_score("epoch", "v0")
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(RecordError):
        read_gen_score(layout, "epoch", "v0")
    with pytest.raises(RecordError):
        write_gen_score(tmp_path, "epoch", "v0", {"scalar": 0})
    assert path.read_text() == text
    assert not layout.gen_score_history("epoch", "v0").exists()


def test_score_history_recovers_only_an_incomplete_final_json_line(tmp_path: Path) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    write_gen_score(tmp_path, "epoch", "v0", {"scalar": 1})
    path = layout.gen_score_history("epoch", "v0")
    prefix = path.read_bytes()
    with path.open("ab") as stream:
        stream.write(b'{"scalar":')
    assert len(read_gen_score_history(layout, "epoch", "v0")) == 1
    write_gen_score(tmp_path, "epoch", "v0", {"scalar": 0}, round_index=2)
    assert path.read_bytes().startswith(prefix)
    assert [row.seq for row in read_gen_score_history(layout, "epoch", "v0")] == [0, 1]
    assert read_gen_score_history(layout, "epoch", "v0")[1].round_index == 2
    before = layout.gen_score("epoch", "v0").read_bytes()
    with path.open("ab") as stream:
        stream.write(b"{broken}\n")
    with pytest.raises(RecordError, match="line 3"):
        read_gen_score_history(layout, "epoch", "v0")
    with pytest.raises(RecordError, match="line 3"):
        write_gen_score(tmp_path, "epoch", "v0", {"scalar": -1})
    assert layout.gen_score("epoch", "v0").read_bytes() == before


def test_history_is_durable_before_flat_score_replacement(tmp_path: Path, monkeypatch) -> None:
    from zicato.tournament import scoring

    layout = WorkspaceLayout.from_root(tmp_path)
    write_gen_score(tmp_path, "epoch", "v0", {"scalar": 1})
    score_path = layout.gen_score("epoch", "v0")
    before = score_path.read_bytes()
    original = scoring.atomic_write_text

    def fail_flat(path: Path, text: str) -> None:
        if path == score_path:
            assert read_gen_score_history(layout, "epoch", "v0")[-1].score.scalar == 0
            raise OSError("flat replacement interrupted")
        original(path, text)

    monkeypatch.setattr(scoring, "atomic_write_text", fail_flat)
    with pytest.raises(OSError, match="flat replacement interrupted"):
        write_gen_score(tmp_path, "epoch", "v0", {"scalar": 0})
    assert score_path.read_bytes() == before


def test_query_and_index_share_lineage_refusal(tmp_path: Path) -> None:
    from zicato.index.ingest import _lineage_by_epoch
    from zicato.query.lineage_view import build_lineage_view
    from zicato.query.paths import WorkspacePaths
    from zicato.query.runtime_view import read_lineage_dict

    tmp_path.joinpath("lineage.json").write_text('{"epochs": [null]}')
    paths = WorkspacePaths(tmp_path)
    with pytest.raises(RecordError, match="generations list"):
        _lineage_by_epoch(tmp_path)
    assert "generations list" in build_lineage_view(paths)["unreadable"]
    assert "generations list" in read_lineage_dict(paths)["unreadable"]


def test_score_corruption_is_visible_and_cannot_supply_a_gate(tmp_path: Path) -> None:
    from zicato.evolve.round_baseline import _load_historical_aggregate
    from zicato.query.gate_view import build_gate_breakdown
    from zicato.query.paths import WorkspacePaths
    from zicato.query.tournament_view import build_matchup_grid, build_tournament_structure

    layout = WorkspaceLayout.from_root(tmp_path)
    path = layout.gen_score("epoch", "v1")
    path.parent.mkdir(parents=True)
    path.write_text('{"scalar": true}')
    paths = WorkspacePaths(tmp_path)
    with pytest.raises(RecordError, match="scalar must be finite"):
        _load_historical_aggregate(tmp_path, "epoch", "v1")
    for view in (
        build_matchup_grid(paths, "epoch", "v0", "v1"),
        build_gate_breakdown(paths, "epoch", "v0", "v1"),
        build_tournament_structure(paths, "epoch", "epoch:v0->v1"),
    ):
        assert "scalar must be finite" in view["unreadable"]
