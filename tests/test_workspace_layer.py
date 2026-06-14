"""Unit tests for the typed canonical-read layer ``zicato.workspace``.

Covers the three responsibilities the package owns: the single ordering
definition (``epoch_sort_key`` timestamp-first), the single epoch
enumeration (``iter_epochs`` / ``list_epoch_ids``), the ``WorkspaceLayout``
path math, and the best-effort typed reads (board / experiments / loss /
gen_score / epoch config).
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.workspace import (
    Epoch,
    WorkspaceLayout,
    epoch_created_at,
    epoch_sort_key,
    iter_epochs,
    list_epoch_ids,
    natural_key,
    read_board,
    read_epoch_config,
    read_experiment,
    read_experiments,
    read_gen_score,
    read_loss,
)


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _epoch(
    ws: Path,
    epoch_id: str,
    created_at: str | None,
    gens: list[str] | None = None,
) -> None:
    edir = ws / "epochs" / epoch_id
    edir.mkdir(parents=True, exist_ok=True)
    if created_at is not None:
        _write(edir / "config.json", {"id": epoch_id, "created_at": created_at})
    for gid in gens or []:
        (edir / "generations" / gid).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Ordering primitives — the single definition
# ---------------------------------------------------------------------------


def test_natural_key_orders_numerically() -> None:
    assert sorted(["v0", "v1", "v2", "v10", "v11", "v9"], key=natural_key) == [
        "v0",
        "v1",
        "v2",
        "v9",
        "v10",
        "v11",
    ]


def test_epoch_sort_key_is_timestamp_first(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e10", "2026-01-01T00:00:00Z")  # name sorts AFTER e2, created BEFORE
    _epoch(ws, "e2", "2026-02-01T00:00:00Z")
    a = ws / "epochs" / "e10"
    b = ws / "epochs" / "e2"
    # e10 created first -> smaller sort key -> sorts first.
    assert epoch_sort_key(a) < epoch_sort_key(b)


def test_epoch_created_at_absent_is_empty(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e0", None)
    assert epoch_created_at(ws / "epochs" / "e0") == ""


def test_epoch_created_at_reads_config(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e0", "2026-03-04T05:06:07Z")
    assert epoch_created_at(ws / "epochs" / "e0") == "2026-03-04T05:06:07Z"


# ---------------------------------------------------------------------------
# Enumeration — the single authority
# ---------------------------------------------------------------------------


def test_iter_epochs_empty_when_no_epochs_dir(tmp_path: Path) -> None:
    layout = WorkspaceLayout.from_root(tmp_path / ".zicato")
    assert iter_epochs(layout) == []
    assert list_epoch_ids(layout) == []


def test_list_epoch_ids_timestamp_order(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    # Created in id order e0 -> e2 -> e10, ascending timestamps.
    _epoch(ws, "e0", "2026-01-01T00:00:00Z")
    _epoch(ws, "e2", "2026-02-01T00:00:00Z")
    _epoch(ws, "e10", "2026-03-01T00:00:00Z")
    layout = WorkspaceLayout.from_root(ws)
    assert list_epoch_ids(layout) == ["e0", "e2", "e10"]


def test_list_epoch_ids_timestamp_overrides_id(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    # e10 created BEFORE e2 -> must sort first despite 10 > 2.
    _epoch(ws, "e10", "2026-01-01T00:00:00Z")
    _epoch(ws, "e2", "2026-02-01T00:00:00Z")
    layout = WorkspaceLayout.from_root(ws)
    assert list_epoch_ids(layout) == ["e10", "e2"]


def test_list_epoch_ids_falls_back_to_numeric_id(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    for epoch in ("e0", "e2", "e10"):
        _epoch(ws, epoch, None)
    layout = WorkspaceLayout.from_root(ws)
    # No timestamps anywhere -> numeric id fallback, never lexical.
    assert list_epoch_ids(layout) == ["e0", "e2", "e10"]


def test_iter_epochs_returns_typed_handles(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e0", "2026-01-01T00:00:00Z")
    layout = WorkspaceLayout.from_root(ws)
    epochs = iter_epochs(layout)
    assert len(epochs) == 1
    e = epochs[0]
    assert isinstance(e, Epoch)
    assert e.id == "e0"
    assert e.directory == ws / "epochs" / "e0"
    assert e.created_at == "2026-01-01T00:00:00Z"


def test_iter_epochs_skips_non_directories(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e0", "2026-01-01T00:00:00Z")
    (ws / "epochs" / "stray.txt").write_text("not a dir")
    layout = WorkspaceLayout.from_root(ws)
    assert list_epoch_ids(layout) == ["e0"]


# ---------------------------------------------------------------------------
# WorkspaceLayout — path math (byte-identical to the inline joins)
# ---------------------------------------------------------------------------


def test_layout_paths(tmp_path: Path) -> None:
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    assert layout.epochs_dir == root / "epochs"
    assert layout.lineage_path == root / "lineage.json"
    assert layout.index_db_path == root / "index.db"
    assert layout.epoch_dir("e0") == root / "epochs" / "e0"
    assert layout.epoch_config("e0") == root / "epochs" / "e0" / "config.json"
    assert layout.board("e0") == root / "epochs" / "e0" / "board.jsonl"
    assert layout.scoring("e0") == root / "epochs" / "e0" / "scoring.json"
    assert layout.contract_components("e0") == (root / "epochs" / "e0" / "contract_components.json")
    assert layout.generations_dir("e0") == root / "epochs" / "e0" / "generations"
    assert layout.generation_dir("e0", "v1") == (root / "epochs" / "e0" / "generations" / "v1")
    assert layout.experiment("e0", "v1") == (
        root / "epochs" / "e0" / "generations" / "v1" / "experiment.json"
    )
    assert layout.gen_score("e0", "v1") == (
        root / "epochs" / "e0" / "generations" / "v1" / "gen_score.json"
    )
    assert layout.runs_dir("e0", "v1") == (root / "epochs" / "e0" / "generations" / "v1" / "runs")
    assert layout.run_dir("e0", "v1", "t1") == (
        root / "epochs" / "e0" / "generations" / "v1" / "runs" / "t1"
    )
    assert layout.loss("e0", "v1", "t1") == (
        root / "epochs" / "e0" / "generations" / "v1" / "runs" / "t1" / "loss.json"
    )
    assert layout.events("e0", "v1", "t1") == (
        root / "epochs" / "e0" / "generations" / "v1" / "runs" / "t1" / "events.jsonl"
    )


def test_layout_is_pure_no_io(tmp_path: Path) -> None:
    # The layout never touches the filesystem: it resolves paths for a root
    # that does not exist without error.
    layout = WorkspaceLayout.from_root(tmp_path / "nonexistent" / ".zicato")
    assert layout.board("eX").name == "board.jsonl"


# ---------------------------------------------------------------------------
# Typed reads — best-effort
# ---------------------------------------------------------------------------


def test_read_epoch_config_present_and_absent(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(ws, "e0", "2026-01-01T00:00:00Z")
    layout = WorkspaceLayout.from_root(ws)
    cfg = read_epoch_config(layout, "e0")
    assert cfg == {"id": "e0", "created_at": "2026-01-01T00:00:00Z"}
    assert read_epoch_config(layout, "missing") is None


def test_read_epoch_config_malformed_is_none(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "epochs" / "e0").mkdir(parents=True)
    (ws / "epochs" / "e0" / "config.json").write_text("{not json")
    layout = WorkspaceLayout.from_root(ws)
    assert read_epoch_config(layout, "e0") is None


def test_read_board_lines_and_missing(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "epochs" / "e0").mkdir(parents=True)
    board = ws / "epochs" / "e0" / "board.jsonl"
    board.write_text(
        '{"board_meta": true, "disable_drift": false}\n'
        "\n"  # blank line skipped
        "not json\n"  # non-JSON skipped
        '"a string"\n'  # non-dict skipped
        '{"id": "t1", "kind": "single_turn"}\n'
    )
    layout = WorkspaceLayout.from_root(ws)
    lines = read_board(layout, "e0")
    assert lines is not None
    assert lines == [
        {"board_meta": True, "disable_drift": False},
        {"id": "t1", "kind": "single_turn"},
    ]
    # Missing file -> None.
    assert read_board(layout, "missing") is None


def test_read_experiments_in_numeric_order(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    for gid in ("v0", "v1", "v2", "v10"):
        _write(
            ws / "epochs" / "e0" / "generations" / gid / "experiment.json",
            {"generation_id": gid},
        )
    # A generation with no experiment.json is skipped.
    (ws / "epochs" / "e0" / "generations" / "v3").mkdir(parents=True)
    layout = WorkspaceLayout.from_root(ws)
    out = read_experiments(layout, "e0")
    assert [gid for gid, _ in out] == ["v0", "v1", "v2", "v10"]
    assert out[0][1] == {"generation_id": "v0"}
    # Single-generation reader agrees.
    assert read_experiment(layout, "e0", "v1") == {"generation_id": "v1"}
    assert read_experiment(layout, "e0", "missing") is None


def test_read_experiments_empty_when_no_generations(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "epochs" / "e0").mkdir(parents=True)
    layout = WorkspaceLayout.from_root(ws)
    assert read_experiments(layout, "e0") == []


def test_read_loss_and_gen_score(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    run = ws / "epochs" / "e0" / "generations" / "v1" / "runs" / "t1"
    _write(run / "loss.json", {"entry_id": "t1", "drift_loss": 0.5})
    _write(
        ws / "epochs" / "e0" / "generations" / "v1" / "gen_score.json",
        {"scalar": 0.25},
    )
    layout = WorkspaceLayout.from_root(ws)
    assert read_loss(layout, "e0", "v1", "t1") == {"entry_id": "t1", "drift_loss": 0.5}
    assert read_loss(layout, "e0", "v1", "missing") is None
    assert read_gen_score(layout, "e0", "v1") == {"scalar": 0.25}
    # Absent gen_score -> {} (not None), matching the prior reader.
    assert read_gen_score(layout, "e0", "missing") == {}


def test_reads_never_raise_on_garbage(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    run = ws / "epochs" / "e0" / "generations" / "v1" / "runs" / "t1"
    run.mkdir(parents=True)
    (run / "loss.json").write_text("{broken")
    (ws / "epochs" / "e0" / "generations" / "v1" / "gen_score.json").write_text("[]")
    layout = WorkspaceLayout.from_root(ws)
    assert read_loss(layout, "e0", "v1", "t1") is None
    # gen_score that parses to a non-dict (list) -> {}.
    assert read_gen_score(layout, "e0", "v1") == {}
