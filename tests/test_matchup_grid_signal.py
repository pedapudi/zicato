"""The matchup grid resolves each board entry on the signal the contract carries.

``build_matchup_grid`` used to define every per-entry ``verdict`` / ``won_by``
on the drift loss alone. An adapter that emits no drift stream writes a
structural ``0.0`` on every entry, so on such a workspace every entry read
"flat" and nothing was ever won — while the continuous scores that actually
decided the round sat unread on the same row.

These tests pin the resolution order (score → pass predicate → drift), the
per-row degrade, the served ``delta_score`` and its replicate spread, and the
``drift_present`` flag that lets a client hide a channel that carries nothing.
Everything is driven off the on-disk ``loss.json`` / ``loss.r<N>.json`` files
the worker actually writes — this reader never touches the SQLite index.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.loss import LossProfile
from zicato.core.workspace import WorkspaceLayout, loss_profile_path
from zicato.query.paths import WorkspacePaths
from zicato.query.replicate_scores import standard_error
from zicato.query.tournament_view import build_matchup_grid

EPOCH = "2026-08-17_signal"


def _unit_loss_path(workspace: Path, gen: str, entry: str, replicate: int) -> Path:
    base = loss_profile_path(workspace, EPOCH, gen, entry)
    return base if replicate == 0 else base.with_name(f"loss.r{replicate}.json")


def _write_loss(
    workspace: Path,
    gen: str,
    entry: str,
    *,
    drift: float = 0.0,
    passes: bool | None = None,
    score: float | None = None,
    replicate: int = 0,
    drift_counts: tuple = (),
) -> None:
    """Write ONE per-replicate loss profile exactly as the worker emits it."""
    from zicato.telemetry import reducer  # noqa: PLC0415

    profile = LossProfile(
        run_id=f"{gen}:{entry}" + ("" if replicate == 0 else f":r{replicate}"),
        entry_id=entry,
        generation_id=gen,
        epoch_id=EPOCH,
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift,
        pass_fail=passes,
        score=score,
    )
    reducer.write_loss_profile(profile, _unit_loss_path(workspace, gen, entry, replicate))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    WorkspaceLayout.from_root(root).epoch_dir(EPOCH).mkdir(parents=True, exist_ok=True)
    (root / "current_epoch").write_text(EPOCH, encoding="utf-8")
    return root


def _grid(workspace: Path) -> dict:
    return build_matchup_grid(WorkspacePaths(root=workspace), EPOCH, "v0", "v1")


def _row(grid: dict, entry_id: str) -> dict:
    return next(r for r in grid["entry_grid"] if r["entry_id"] == entry_id)


def test_scored_no_drift_board_resolves_on_score(workspace: Path) -> None:
    """The bug: with no drift stream every verdict was flat and nothing was won."""
    for entry, champ, chall in (("gain", 0.415, 1.0), ("regress", 1.0, 0.959), ("held", 1.0, 1.0)):
        _write_loss(workspace, "v0", entry, drift=0.0, passes=True, score=champ)
        _write_loss(workspace, "v1", entry, drift=0.0, passes=True, score=chall)

    grid = _grid(workspace)

    assert {r["decided_by"] for r in grid["entry_grid"]} == {"score"}
    assert _row(grid, "gain")["verdict"] == "improved"
    assert _row(grid, "gain")["won_by"] == "v1"
    assert _row(grid, "gain")["delta_score"] == pytest.approx(0.585)
    assert _row(grid, "regress")["verdict"] == "regressed"
    assert _row(grid, "regress")["won_by"] == "v0"
    assert _row(grid, "regress")["delta_score"] == pytest.approx(-0.041)
    # A control held AT THE CEILING is flat, but on the score channel — not
    # because the only populated number was a structural zero.
    assert _row(grid, "held")["verdict"] == "flat"
    assert _row(grid, "held")["won_by"] is None
    assert _row(grid, "held")["delta_score"] == pytest.approx(0.0)
    # Nothing on this board recorded a drift event or a non-zero loss, so the
    # channel is reported absent and a client hides it rather than guessing.
    assert grid["drift_present"] is False


def test_drift_only_board_keeps_the_drift_verdicts(workspace: Path) -> None:
    """A board with no continuous score falls through to drift, lower-is-better."""
    _write_loss(workspace, "v0", "a", drift=105.5, drift_counts=(("tool_swap", 1, "minor"),))
    _write_loss(workspace, "v1", "a", drift=60.5)
    _write_loss(workspace, "v0", "b", drift=60.5)
    _write_loss(workspace, "v1", "b", drift=642.5)

    grid = _grid(workspace)

    assert _row(grid, "a")["decided_by"] == "drift"
    assert _row(grid, "a")["verdict"] == "improved"
    assert _row(grid, "a")["won_by"] == "v1"
    assert _row(grid, "b")["verdict"] == "regressed"
    assert _row(grid, "b")["won_by"] == "v0"
    assert all(r["delta_score"] is None for r in grid["entry_grid"])
    assert grid["drift_present"] is True


def test_pass_predicate_decides_when_neither_score_nor_drift_moves(workspace: Path) -> None:
    _write_loss(workspace, "v0", "a", drift=0.0, passes=False)
    _write_loss(workspace, "v1", "a", drift=0.0, passes=True)

    row = _row(_grid(workspace), "a")

    assert row["decided_by"] == "pass"
    assert row["verdict"] == "improved"
    assert row["won_by"] == "v1"


def test_channels_resolve_per_row_not_per_grid(workspace: Path) -> None:
    """A pre-score champion degrades THAT row only — the entry is never dropped."""
    # scored on both sides
    _write_loss(workspace, "v0", "scored", drift=0.0, passes=True, score=0.35)
    _write_loss(workspace, "v1", "scored", drift=0.0, passes=True, score=0.80)
    # the champion's profile predates the score field: the row falls through to
    # the next populated channel rather than dropping out of the grid.
    _write_loss(workspace, "v0", "legacy", drift=4.0, passes=False, score=None)
    _write_loss(workspace, "v1", "legacy", drift=1.0, passes=False, score=0.9)
    # only the challenger ran this entry at all.
    _write_loss(workspace, "v1", "unpaired", drift=0.0, passes=True, score=1.0)

    grid = _grid(workspace)

    assert [r["entry_id"] for r in grid["entry_grid"]] == ["legacy", "scored", "unpaired"]
    assert _row(grid, "scored")["decided_by"] == "score"
    assert _row(grid, "legacy")["decided_by"] == "drift"
    assert _row(grid, "legacy")["delta_score"] is None
    assert _row(grid, "unpaired")["decided_by"] is None
    assert _row(grid, "unpaired")["verdict"] == "flat"
    assert _row(grid, "unpaired")["won_by"] is None
    assert _row(grid, "unpaired")["child_score"] == pytest.approx(1.0)


def test_delta_score_column_sums_over_the_shared_slice(workspace: Path) -> None:
    """Only entries BOTH sides ran carry a delta, so summing the column is honest.

    The client used to restrict the champion to the candidate's sampled boards
    before summing; getting that restriction wrong silently changes what the
    total means. The server now applies it: an entry one side never ran carries
    ``delta_score: None`` and contributes nothing.
    """
    _write_loss(workspace, "v0", "shared_a", score=0.35, passes=True)
    _write_loss(workspace, "v1", "shared_a", score=0.80, passes=True)
    _write_loss(workspace, "v0", "shared_b", score=1.0, passes=True)
    _write_loss(workspace, "v1", "shared_b", score=0.90, passes=True)
    _write_loss(workspace, "v0", "champion_only", score=0.10, passes=False)
    _write_loss(workspace, "v1", "challenger_only", score=0.99, passes=True)

    grid = _grid(workspace)
    deltas = [r["delta_score"] for r in grid["entry_grid"] if r["delta_score"] is not None]

    assert len(deltas) == 2
    assert sum(deltas) == pytest.approx(0.35)
    assert _row(grid, "champion_only")["delta_score"] is None
    assert _row(grid, "challenger_only")["delta_score"] is None


def test_score_standard_error_needs_more_than_one_replicate(workspace: Path) -> None:
    """One draw measures no spread: the wire says null, never 0.0."""
    _write_loss(workspace, "v0", "single", score=0.5, passes=True)
    _write_loss(workspace, "v1", "single", score=0.7, passes=True)
    _write_loss(workspace, "v0", "replicated", score=0.5, passes=True)
    for replicate, score in ((0, 0.60), (1, 0.70), (2, 0.80)):
        _write_loss(workspace, "v1", "replicated", score=score, passes=True, replicate=replicate)

    grid = _grid(workspace)

    assert _row(grid, "single")["score_replicates"] == 1
    assert _row(grid, "single")["score_se"] is None
    assert _row(grid, "replicated")["score_replicates"] == 3
    assert _row(grid, "replicated")["score_se"] == pytest.approx(standard_error([0.6, 0.7, 0.8]))


def test_standard_error_is_the_sample_sd_over_root_n() -> None:
    assert standard_error([]) is None
    assert standard_error([0.5]) is None
    # sample sd of {0.6, 0.7, 0.8} is 0.1; se = 0.1 / sqrt(3).
    assert standard_error([0.6, 0.7, 0.8]) == pytest.approx(0.1 / 3**0.5)


def test_drift_present_is_true_when_a_run_recorded_a_drift_event(workspace: Path) -> None:
    """A zero loss WITH a recorded observation is a real reading, not an absent channel."""
    _write_loss(
        workspace, "v0", "a", drift=0.0, score=0.5, drift_counts=(("tool_swap", 0, "minor"),)
    )
    _write_loss(workspace, "v1", "a", drift=0.0, score=0.6)

    assert _grid(workspace)["drift_present"] is True


def test_unknown_matchup_degrades_to_an_empty_grid(workspace: Path) -> None:
    grid = build_matchup_grid(WorkspacePaths(root=workspace), EPOCH, "nope", "also_nope")

    assert grid["entry_grid"] == []
    assert grid["drift_present"] is False
    assert grid["source"] == "loss_files"


def test_grid_is_json_serialisable(workspace: Path) -> None:
    """The endpoint serves this verbatim, so every added field must survive encoding."""
    _write_loss(workspace, "v0", "a", score=0.5, passes=True)
    _write_loss(workspace, "v1", "a", score=0.9, passes=True)

    assert json.loads(json.dumps(_grid(workspace)))["entry_grid"][0]["decided_by"] == "score"
