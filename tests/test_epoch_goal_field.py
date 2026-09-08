"""Tests for the per-epoch ``goal`` field — task #178.

Covers:

* :class:`zicato.core.types.EpochConfig` serialises and deserialises the
  ``goal`` field through :mod:`zicato.epoch.lifecycle`.
* ``new_epoch`` round-trips an operator-supplied goal into
  ``config.json``.
* :func:`zicato.index.ingest.rebuild_index` populates ``epochs.goal``
  from the per-epoch ``config.json``.
* The ``zicato epoch new --goal "..."`` CLI flag end-to-end.
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
from zicato.cli.commands.epoch import epoch_grp
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
)
from zicato.workspace.config_io import write_workspace_config

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
        contract_hash="0" * 64,
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


def test_epoch_config_requires_its_recorded_contract_identity() -> None:
    from zicato.testing.fixtures import make_epoch_config

    cfg = make_epoch_config(contract_hash="f" * 64)
    payload = json.loads(json.dumps(_config_to_dict(cfg)))
    assert _config_from_dict(payload).contract_hash == cfg.contract_hash
    for invalid in (None, "", "not-a-digest"):
        with pytest.raises(ValueError, match="contract_hash"):
            _config_from_dict({**payload, "contract_hash": invalid})
    del payload["contract_hash"]
    with pytest.raises(ValueError, match="contract_hash"):
        _config_from_dict(payload)


def test_epoch_config_round_trips_implementation_identity() -> None:
    identity = {
        "zicato_evaluator_revision": 1,
        "goldfive_version": "git:abcdef",
        "zicato_goldfive_integration_revision": 1,
    }
    cfg = EpochConfig(
        id="2026-05-27_identity",
        name="identity",
        created_at="2026-05-27T00:00:00+00:00",
        board_path=Path("/board.jsonl"),
        brief_path=Path("/brief.md"),
        scoring=ScoringWeights(goldfive={}),
        contract_hash="feedface00000002" * 4,
        implementation_identity=identity,
    )

    payload = json.loads(json.dumps(_config_to_dict(cfg)))
    assert payload["implementation_identity"] == identity
    assert _config_from_dict(payload).implementation_identity == identity


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


def test_new_epoch_writes_evaluator_identity_into_config_json(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    cfg = new_epoch(
        workspace_root=workspace,
        name="implementation-identity",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
    )

    expected = {"zicato_evaluator_revision": 2}
    assert cfg.implementation_identity == expected
    raw = json.loads((workspace / "epochs" / cfg.id / "config.json").read_text())
    assert raw["implementation_identity"] == expected
    assert load_epoch(workspace, cfg.id).implementation_identity == expected


def test_invalid_goldfive_document_leaves_the_current_epoch_unchanged(
    workspace: Path,
    board_file: Path,
    brief_file: Path,
) -> None:
    current = new_epoch(
        workspace_root=workspace,
        name="valid-contract",
        board_source=board_file,
        brief_source=brief_file,
        weights=ScoringWeights(),
    )
    before_epochs = {path.name for path in (workspace / "epochs").iterdir()}

    with pytest.raises(ValueError, match="unknown"):
        new_epoch(
            workspace_root=workspace,
            name="invalid-goldfive-contract",
            board_source=board_file,
            brief_source=brief_file,
            weights=ScoringWeights(goldfive={"unknown_field": True}),
        )

    assert load_epoch(workspace, current.id).closed is False
    assert (workspace / "current_epoch").read_text().strip() == current.id
    assert {path.name for path in (workspace / "epochs").iterdir()} == before_epochs


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
