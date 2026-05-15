"""Tests for :mod:`zicato.epoch.lifecycle`."""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

import pytest

from zicato.core.types import ScoringWeights
from zicato.epoch import (
    close_epoch,
    current_epoch_id,
    list_epochs,
    load_epoch,
    new_epoch,
    switch_epoch,
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
    path.write_text('{"id": "e1", "kind": "single_turn", '
                    '"wall_clock_budget_seconds": 60, "input": "hi"}\n')
    return path


@pytest.fixture()
def rubric_file(tmp_path: Path) -> Path:
    path = tmp_path / "rubric.md"
    path.write_text("# Rubric for tests\n\n## Forbidden\n\n(none)\n")
    return path


# ---------------------------------------------------------------------------
# new_epoch
# ---------------------------------------------------------------------------


def test_new_epoch_creates_expected_layout(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights()
    cfg = new_epoch(
        workspace_root=workspace,
        name="my first epoch",
        board_source=board_file,
        rubric_source=rubric_file,
        weights=weights,
    )

    # Id is date-prefixed with a filesystem-safe slug.
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    assert cfg.id == f"{today}_my_first_epoch"

    # Directory layout exists.
    edir = workspace / "epochs" / cfg.id
    assert edir.is_dir()
    assert (edir / "board.jsonl").exists()
    assert (edir / "rubric.md").exists()
    assert (edir / "scoring.json").exists()
    assert (edir / "config.json").exists()

    # Board and rubric are copies, not the originals.
    assert (edir / "board.jsonl").read_text() == board_file.read_text()
    assert (edir / "rubric.md").read_text() == rubric_file.read_text()

    # scoring.json is parseable and round-trips key fields.
    scoring = json.loads((edir / "scoring.json").read_text())
    assert scoring["drift_weight"] == weights.drift_weight
    assert scoring["pass_weight"] == weights.pass_weight
    assert scoring["severity_weights"]["critical"] == 10.0

    # current_epoch marker points at the new epoch.
    assert current_epoch_id(workspace) == cfg.id


def test_new_epoch_with_duplicate_name_gets_numeric_suffix(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights()
    a = new_epoch(workspace, "experiment", board_file, rubric_file, weights)
    b = new_epoch(workspace, "experiment", board_file, rubric_file, weights)
    assert a.id != b.id
    assert b.id.endswith("_2")


def test_new_epoch_rejects_empty_slug(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    with pytest.raises(ValueError, match="empty slug"):
        new_epoch(workspace, "!!!", board_file, rubric_file, ScoringWeights())


def test_new_epoch_auto_closes_previous_open_epoch(
    workspace: Path,
    board_file: Path,
    rubric_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    weights = ScoringWeights()
    first = new_epoch(workspace, "alpha", board_file, rubric_file, weights)
    assert not load_epoch(workspace, first.id).closed

    # A small time gap so the suffix logic doesn't collide.
    time.sleep(0.01)
    second = new_epoch(workspace, "beta", board_file, rubric_file, weights)

    # First epoch is now closed.
    refreshed = load_epoch(workspace, first.id)
    assert refreshed.closed
    assert refreshed.closed_at
    # Auto-close emitted a warning on stderr.
    err = capsys.readouterr().err
    assert "auto-closing" in err.lower() or "not closed manually" in err.lower()
    # Second is current and open.
    assert current_epoch_id(workspace) == second.id
    assert not load_epoch(workspace, second.id).closed


def test_new_epoch_can_skip_auto_close(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights()
    first = new_epoch(workspace, "alpha", board_file, rubric_file, weights)
    new_epoch(
        workspace,
        "beta",
        board_file,
        rubric_file,
        weights,
        auto_close_previous=False,
    )
    # First epoch remains open.
    assert not load_epoch(workspace, first.id).closed


# ---------------------------------------------------------------------------
# close_epoch
# ---------------------------------------------------------------------------


def test_close_epoch_marks_closed_and_writes_analysis(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(
        workspace, "alpha", board_file, rubric_file, ScoringWeights()
    )

    async def stub_call(system: str, user: str, model: str) -> str:
        # Echo a fixed structured response so we can assert on it.
        return (
            f"# Epoch analysis: {cfg.id}\n\n"
            "## Headline movements\n- A\n\n"
            "## Hypotheses that held\n- B\n\n"
            "## Hypotheses that didn't\n- C\n\n"
            "## Surface still open at epoch close\n- D\n\n"
            "## Recommended focus for next epoch\n- E\n"
        )

    out = close_epoch(workspace, cfg.id, aux_call_llm=stub_call)
    assert out.exists()
    text = out.read_text()
    assert f"# Epoch analysis: {cfg.id}" in text
    assert "## Headline movements" in text
    assert "## Recommended focus for next epoch" in text

    # Persistent state updated.
    refreshed = load_epoch(workspace, cfg.id)
    assert refreshed.closed
    assert refreshed.closed_at


def test_close_epoch_without_aux_writes_stub(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(
        workspace, "alpha", board_file, rubric_file, ScoringWeights()
    )
    out = close_epoch(workspace, cfg.id, aux_call_llm=None)
    text = out.read_text()
    assert f"# Epoch analysis: {cfg.id}" in text
    assert "stub" in text.lower()
    assert load_epoch(workspace, cfg.id).closed


def test_close_epoch_uses_current_when_id_omitted(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    close_epoch(workspace, None, aux_call_llm=None)
    assert load_epoch(workspace, cfg.id).closed


def test_close_epoch_with_no_current_raises(workspace: Path) -> None:
    with pytest.raises(RuntimeError, match="no current_epoch marker"):
        close_epoch(workspace, None, aux_call_llm=None)


# ---------------------------------------------------------------------------
# list / switch / current
# ---------------------------------------------------------------------------


def test_list_epochs_returns_creation_order(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights()
    a = new_epoch(workspace, "alpha", board_file, rubric_file, weights)
    time.sleep(1.01)  # created_at has second precision
    b = new_epoch(workspace, "beta", board_file, rubric_file, weights)
    time.sleep(1.01)
    c = new_epoch(workspace, "gamma", board_file, rubric_file, weights)
    epochs = list_epochs(workspace)
    ids = [e.id for e in epochs]
    assert ids == [a.id, b.id, c.id]


def test_list_epochs_empty_when_no_workspace(tmp_path: Path) -> None:
    assert list_epochs(tmp_path / ".zicato") == []


def test_list_epochs_skips_directories_without_config(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    # Drop a stub directory with no config.json.
    (workspace / "epochs" / "junk").mkdir()
    epochs = list_epochs(workspace)
    assert len(epochs) == 1


def test_switch_epoch_updates_marker(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights()
    a = new_epoch(workspace, "alpha", board_file, rubric_file, weights)
    b = new_epoch(workspace, "beta", board_file, rubric_file, weights)
    assert current_epoch_id(workspace) == b.id
    switch_epoch(workspace, a.id)
    assert current_epoch_id(workspace) == a.id


def test_switch_epoch_rejects_unknown_id(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        switch_epoch(workspace, "definitely_not_a_real_epoch")


def test_current_epoch_id_returns_none_when_marker_missing(workspace: Path) -> None:
    assert current_epoch_id(workspace) is None


def test_load_epoch_round_trips(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    weights = ScoringWeights(
        drift_weight=2.0, pass_weight=3.0, promote_margin=0.05
    )
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, weights)
    loaded = load_epoch(workspace, cfg.id)
    assert loaded.id == cfg.id
    assert loaded.name == "alpha"
    assert loaded.scoring.drift_weight == 2.0
    assert loaded.scoring.pass_weight == 3.0
    assert loaded.scoring.promote_margin == 0.05
    assert loaded.closed is False


def test_load_epoch_missing_raises(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_epoch(workspace, "nothing")
