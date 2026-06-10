"""Tests for ``zicato.runtime.paths`` — pure path math + directory creation."""

from __future__ import annotations

from pathlib import Path

from zicato.runtime.paths import (
    active_run_path,
    active_runs_dir,
    active_tournament_path,
    control_command_path,
    control_dir,
    control_log_dir,
    ensure_runtime_dirs,
    heartbeat_path,
    kill_request_path,
    kill_requests_dir,
    lock_path,
    runtime_dir,
)


def test_runtime_dir_resolves_directly_under_workspace(tmp_path: Path) -> None:
    """``workspace_root`` is the ``.zicato/`` dir; runtime/ is its child."""
    assert runtime_dir(tmp_path) == tmp_path / "runtime"


def test_top_level_files_live_directly_under_runtime(tmp_path: Path) -> None:
    rt = runtime_dir(tmp_path)
    assert lock_path(tmp_path) == rt / "lock.json"
    assert heartbeat_path(tmp_path) == rt / "heartbeat.json"
    assert active_tournament_path(tmp_path) == rt / "active_tournament.json"


def test_active_runs_dir_and_per_run_paths(tmp_path: Path) -> None:
    rt = runtime_dir(tmp_path)
    assert active_runs_dir(tmp_path) == rt / "active_runs"
    assert active_run_path(tmp_path, "run_abc") == rt / "active_runs" / "run_abc.json"


def test_control_and_control_log_dirs(tmp_path: Path) -> None:
    rt = runtime_dir(tmp_path)
    assert control_dir(tmp_path) == rt / "control"
    assert control_log_dir(tmp_path) == rt / "control_log"


def test_control_command_path_takes_relative_subpath(tmp_path: Path) -> None:
    rt = runtime_dir(tmp_path)
    assert control_command_path(tmp_path, "pause_epoch") == rt / "control" / "pause_epoch"
    assert (
        control_command_path(tmp_path, "kill_runs/run_xyz")
        == rt / "control" / "kill_runs" / "run_xyz"
    )


def test_kill_request_paths_live_under_a_distinct_control_subdir(tmp_path: Path) -> None:
    # Parent→supervisor kill requests sit under control/kill_requests/, kept
    # distinct from the operator's control/kill_runs/ channel.
    rt = runtime_dir(tmp_path)
    assert kill_requests_dir(tmp_path) == rt / "control" / "kill_requests"
    assert kill_request_path(tmp_path, "run_xyz") == rt / "control" / "kill_requests" / "run_xyz"


def test_ensure_runtime_dirs_creates_tree(tmp_path: Path) -> None:
    ensure_runtime_dirs(tmp_path)
    assert runtime_dir(tmp_path).is_dir()
    assert active_runs_dir(tmp_path).is_dir()
    assert control_dir(tmp_path).is_dir()
    assert control_log_dir(tmp_path).is_dir()


def test_ensure_runtime_dirs_idempotent(tmp_path: Path) -> None:
    ensure_runtime_dirs(tmp_path)
    # Drop a marker file under one of the directories.
    (active_runs_dir(tmp_path) / "marker").write_text("hi")
    # Calling again must not error or wipe the marker.
    ensure_runtime_dirs(tmp_path)
    assert (active_runs_dir(tmp_path) / "marker").read_text() == "hi"


def test_paths_do_not_touch_filesystem(tmp_path: Path) -> None:
    # Calling every path helper on a tmp_path that has no runtime/ tree
    # must not create anything.
    _ = runtime_dir(tmp_path)
    _ = lock_path(tmp_path)
    _ = heartbeat_path(tmp_path)
    _ = active_runs_dir(tmp_path)
    _ = active_run_path(tmp_path, "foo")
    _ = active_tournament_path(tmp_path)
    _ = control_dir(tmp_path)
    _ = control_log_dir(tmp_path)
    _ = control_command_path(tmp_path, "pause_epoch")
    assert not (tmp_path / "runtime").exists()
