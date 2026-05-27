"""Tests for the per-epoch ``goal`` field — task #178.

Covers:

* :class:`zicato.core.types.EpochConfig` serialises and deserialises the
  ``goal`` field through :mod:`zicato.epoch.lifecycle`.
* ``new_epoch`` round-trips an operator-supplied goal into
  ``config.json``.
* The index schema is at version 2 and an older v1 database upgrades in
  place to include the ``epochs.goal`` column.
* :func:`zicato.index.ingest.rebuild_index` populates ``epochs.goal``
  from the per-epoch ``config.json``.
* The ``zicato epoch new --goal "..."`` CLI flag end-to-end.
* :func:`zicato.index.ingest.repair_epoch_goals` is idempotent and
  backfills the ``goal`` key on epochs that predate the field.
* The analyzer report header surfaces the goal (and renders the empty
  case as "no goal recorded").
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.analyzer.report_sections import render_title_block
from zicato.cli.commands.epoch import epoch_grp, repair_epoch_goals_cmd
from zicato.cli.common import write_workspace_config
from zicato.core.types import EpochConfig, ScoringWeights
from zicato.epoch.lifecycle import (
    _config_from_dict,
    _config_to_dict,
    load_epoch,
    new_epoch,
    set_epoch_goal,
)
from zicato.index.ingest import (
    rebuild_index,
    repair_epoch_goals,
)
from zicato.index.schema import (
    SCHEMA_VERSION,
    apply_schema,
    read_schema_version,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


@pytest.fixture()
def board_file(tmp_path: Path) -> Path:
    path = tmp_path / "board.jsonl"
    path.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.md"
    path.write_text("# Proposer brief\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. EpochConfig serialisation round-trip
# ---------------------------------------------------------------------------


def test_epoch_config_serializes_and_deserializes_goal() -> None:
    cfg = EpochConfig(
        id="2026-05-27_x",
        name="x",
        created_at="2026-05-27T00:00:00+00:00",
        board_path=Path("/board.jsonl"),
        brief_path=Path("/brief.md"),
        scoring=ScoringWeights(),
        goal="shift the proposer brief toward concrete deltas\nline two",
    )
    payload = _config_to_dict(cfg)
    assert payload["goal"] == "shift the proposer brief toward concrete deltas\nline two"

    # The payload must JSON-round-trip cleanly (no Path objects, no
    # tuples) so it can land on disk verbatim.
    text = json.dumps(payload)
    raw = json.loads(text)
    restored = _config_from_dict(raw)
    assert restored.goal == cfg.goal

    # Back-compat: a config without the ``goal`` key loads as "".
    raw_legacy = dict(payload)
    del raw_legacy["goal"]
    restored_legacy = _config_from_dict(raw_legacy)
    assert restored_legacy.goal == ""


# ---------------------------------------------------------------------------
# 2. lifecycle writer round-trips goal
# ---------------------------------------------------------------------------


def test_new_epoch_writes_goal_into_config_json(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    goal = "shift the proposer brief toward concrete deltas"
    cfg = new_epoch(
        workspace_root=workspace,
        name="goal-test",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
        goal=goal,
    )
    assert cfg.goal == goal

    # On disk: config.json carries the goal key verbatim.
    raw = json.loads((workspace / "epochs" / cfg.id / "config.json").read_text())
    assert raw["goal"] == goal

    # Reload through the canonical reader.
    reloaded = load_epoch(workspace, cfg.id)
    assert reloaded.goal == goal


def test_set_epoch_goal_overwrites_existing(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    cfg = new_epoch(
        workspace_root=workspace,
        name="setgoal",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
        goal="initial",
    )
    set_epoch_goal(workspace, cfg.id, "revised text")
    assert load_epoch(workspace, cfg.id).goal == "revised text"

    # Idempotent — writing the same goal again is a no-op.
    set_epoch_goal(workspace, cfg.id, "revised text")
    assert load_epoch(workspace, cfg.id).goal == "revised text"


# ---------------------------------------------------------------------------
# 3. Schema migration
# ---------------------------------------------------------------------------


def test_schema_version_is_two() -> None:
    assert SCHEMA_VERSION == 2


def test_v1_database_upgrades_in_place_to_v2(tmp_path: Path) -> None:
    """An older v1 ``epochs`` table picks up the ``goal`` column on apply."""
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    # Pretend an older zicato wrote this database: the v1 epochs DDL
    # plus the v1 user_version stamp. No ``goal`` column.
    conn.execute(
        "CREATE TABLE epochs ("
        "epoch_id TEXT PRIMARY KEY, "
        "contract_hash TEXT, "
        "created_at TEXT, "
        "closed INTEGER)"
    )
    conn.execute(
        "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) "
        "VALUES('legacy_epoch', 'h', '2026-01-01T00:00:00Z', 0)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Re-open and apply the current schema; the migrator must add the
    # ``goal`` column without dropping the legacy row.
    conn = sqlite3.connect(str(db_path))
    apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(epochs)").fetchall()}
    assert "goal" in cols
    assert read_schema_version(conn) == 2
    # Legacy row survives the migration; ``goal`` defaults to NULL.
    row = conn.execute(
        "SELECT epoch_id, contract_hash, goal FROM epochs WHERE epoch_id = ?",
        ("legacy_epoch",),
    ).fetchone()
    assert row is not None
    assert row[0] == "legacy_epoch"
    assert row[1] == "h"
    assert row[2] is None
    conn.close()


# ---------------------------------------------------------------------------
# 4. Ingest populates goal from config.json
# ---------------------------------------------------------------------------


def test_rebuild_index_populates_epoch_goal(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    goal = "new scoring weights for cost drift"
    cfg = new_epoch(
        workspace_root=workspace,
        name="ingest",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
        goal=goal,
    )

    db_path = rebuild_index(workspace)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT goal FROM epochs WHERE epoch_id = ?",
            (cfg.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == goal


def test_rebuild_index_defaults_goal_to_empty_string_when_absent(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """An epoch whose on-disk config.json lacks the ``goal`` key still indexes."""
    cfg = new_epoch(
        workspace_root=workspace,
        name="legacy",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
    )
    # Strip the ``goal`` key to mimic an epoch that predates the field.
    config_path = workspace / "epochs" / cfg.id / "config.json"
    raw = json.loads(config_path.read_text())
    raw.pop("goal", None)
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    db_path = rebuild_index(workspace)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT goal FROM epochs WHERE epoch_id = ?",
            (cfg.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # An empty goal lands as the empty string (the lifecycle loader
    # defaults missing keys to "").
    assert row[0] == ""


# ---------------------------------------------------------------------------
# 5. CLI: zicato epoch new --goal "..."
# ---------------------------------------------------------------------------


def _seed_workspace_for_cli(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a minimal workspace + board + brief files for the CLI test."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    # ``epoch new`` reads workspace config to look up entrypoint /
    # mutable_trees, so write a minimal one.
    write_workspace_config(
        workspace,
        {
            "instance_id": "test",
            "created_at": "2026-05-27T00:00:00Z",
        },
    )
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n", encoding="utf-8")
    return workspace, board, brief


def test_cli_epoch_new_goal_flag_writes_into_config(tmp_path: Path) -> None:
    workspace, board, brief = _seed_workspace_for_cli(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        epoch_grp,
        [
            "new",
            "alpha",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--goal",
            "shift the proposer brief toward concrete deltas",
        ],
    )
    assert result.exit_code == 0, result.output

    # Locate the epoch directory the CLI just created and inspect its
    # config.json.
    epochs_root = workspace / "epochs"
    epoch_dirs = list(epochs_root.iterdir())
    assert len(epoch_dirs) == 1
    raw = json.loads((epoch_dirs[0] / "config.json").read_text())
    assert raw["goal"] == "shift the proposer brief toward concrete deltas"


def test_cli_epoch_new_without_goal_defaults_to_empty_in_non_tty(
    tmp_path: Path,
) -> None:
    """No --goal + non-TTY stdin → empty string (no prompt)."""
    workspace, board, brief = _seed_workspace_for_cli(tmp_path)
    runner = CliRunner()
    # CliRunner.invoke wires stdin to a non-tty StringIO so the prompt
    # branch is not exercised. The CLI must fall back to "".
    result = runner.invoke(
        epoch_grp,
        [
            "new",
            "alpha",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
        ],
    )
    assert result.exit_code == 0, result.output
    epoch_dir = next((workspace / "epochs").iterdir())
    raw = json.loads((epoch_dir / "config.json").read_text())
    assert raw["goal"] == ""


# ---------------------------------------------------------------------------
# 6. repair-epoch-goals idempotency
# ---------------------------------------------------------------------------


def test_repair_epoch_goals_is_idempotent(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """Two epochs, one missing the ``goal`` key; repair is idempotent."""
    cfg_a = new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())
    new_epoch(workspace, "beta", board_file, brief_file, ScoringWeights())

    # Mimic a pre-feature config.json for ``cfg_a``: strip the goal key.
    config_a = workspace / "epochs" / cfg_a.id / "config.json"
    raw = json.loads(config_a.read_text())
    raw.pop("goal", None)
    config_a.write_text(json.dumps(raw), encoding="utf-8")

    # Build the index first so the repair has a target to update.
    rebuild_index(workspace)

    first = repair_epoch_goals(workspace)
    # Two epochs scanned; exactly one config patched (the one we
    # stripped) — ``cfg_b`` was untouched and remains correct.
    assert first["scanned"] == 2
    assert first["config_patched"] == 1
    # config.json now carries the key.
    raw_after = json.loads(config_a.read_text())
    assert "goal" in raw_after
    assert raw_after["goal"] == ""

    # Second pass: nothing left to patch.
    second = repair_epoch_goals(workspace)
    assert second["scanned"] == 2
    assert second["config_patched"] == 0
    assert second["index_updated"] == 0


def test_repair_epoch_goals_cli_command(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """The ``zicato repair-epoch-goals`` CLI surface drives the repair."""
    cfg = new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())
    # Strip the goal key so the repair has work to do.
    config_path = workspace / "epochs" / cfg.id / "config.json"
    raw = json.loads(config_path.read_text())
    raw.pop("goal", None)
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(repair_epoch_goals_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    # The CLI reports the patched count.
    assert "1 config.json files patched" in result.output

    raw_after = json.loads(config_path.read_text())
    assert "goal" in raw_after


# ---------------------------------------------------------------------------
# 7. Analyzer report header surfaces the goal
# ---------------------------------------------------------------------------


def test_analyzer_report_renders_goal_in_header(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """The masthead block surfaces the goal under a ``### Goal`` heading."""
    cfg = new_epoch(
        workspace_root=workspace,
        name="reporting",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
        goal="reduce off-topic drift while keeping pass rate stable",
    )
    data = gather_epoch_report_data(workspace, cfg.id)
    assert data.goal == "reduce off-topic drift while keeping pass rate stable"

    block = render_title_block(data)
    # Goal heading + body sit underneath the masthead.
    assert "### Goal" in block
    assert "reduce off-topic drift while keeping pass rate stable" in block


def test_analyzer_report_renders_placeholder_for_empty_goal(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """A blank goal renders as "(no goal recorded)" so the report shape is uniform."""
    cfg = new_epoch(
        workspace_root=workspace,
        name="empty",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
    )
    data = gather_epoch_report_data(workspace, cfg.id)
    assert data.goal == ""
    block = render_title_block(data)
    assert "### Goal" in block
    assert "(no goal recorded)" in block
