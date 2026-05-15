"""Path helpers for the ``.zicato/runtime/`` state-file tree.

Pure path math — no I/O is performed here other than the explicit
:func:`ensure_runtime_dirs` helper which creates the directory tree.
Every other function returns a :class:`Path` and never touches the
filesystem.

The runtime tree is the read/write surface the orchestrator and the
external supervisor binary share. Layout::

    {workspace_root}/.zicato/runtime/
      lock.json                       # exclusive workspace lock
      heartbeat.json                  # orchestrator liveness beat
      active_runs/{run_id}.json       # per in-flight tournament run
      active_tournament.json          # current tournament shape
      control/                        # operator commands queued by dashboard
        pause_epoch                   # flag file
        skip_round                    # flag file
        kill_runs/{run_id}            # one file per kill target
        promote/{generation_id}       # one file per promote target
        reject/{generation_id}        # one file per reject target
        rubric_replacement.txt        # full rubric text payload
      control_log/                    # consumed commands persist here

The tree lives under ``.zicato/runtime/`` regardless of where the
operator's source tree sits; ``workspace_root`` is the path of the
``.zicato/`` directory itself in the convention this codebase uses.
"""

from __future__ import annotations

from pathlib import Path


def runtime_dir(workspace_root: Path) -> Path:
    """Return ``.zicato/runtime/`` for a workspace."""
    return workspace_root / ".zicato" / "runtime"


def lock_path(workspace_root: Path) -> Path:
    """Return the path to the workspace lock JSON file."""
    return runtime_dir(workspace_root) / "lock.json"


def heartbeat_path(workspace_root: Path) -> Path:
    """Return the path to the heartbeat JSON file."""
    return runtime_dir(workspace_root) / "heartbeat.json"


def active_runs_dir(workspace_root: Path) -> Path:
    """Return the directory holding per-run live state JSONs."""
    return runtime_dir(workspace_root) / "active_runs"


def active_run_path(workspace_root: Path, run_id: str) -> Path:
    """Return the path to one run's live-state JSON file."""
    return active_runs_dir(workspace_root) / f"{run_id}.json"


def active_tournament_path(workspace_root: Path) -> Path:
    """Return the path to the current-tournament JSON file."""
    return runtime_dir(workspace_root) / "active_tournament.json"


def control_dir(workspace_root: Path) -> Path:
    """Return the directory operator commands are dropped into."""
    return runtime_dir(workspace_root) / "control"


def control_log_dir(workspace_root: Path) -> Path:
    """Return the directory consumed commands are archived in."""
    return runtime_dir(workspace_root) / "control_log"


def control_command_path(workspace_root: Path, command: str) -> Path:
    """Return the path to a control command file (relative to ``control/``).

    The ``command`` argument is taken verbatim as a relative path under
    :func:`control_dir`. It may include subdirectories (e.g.
    ``"kill_runs/run_abc"``) — the kill/promote/reject commands keep
    one file per target underneath a per-command-kind subdirectory.
    """
    return control_dir(workspace_root) / command


def ensure_runtime_dirs(workspace_root: Path) -> None:
    """Create the runtime directory tree (idempotent).

    Safe to call repeatedly; ``mkdir(parents=True, exist_ok=True)`` does
    not error if the tree already exists. Does NOT create any of the
    JSON state files themselves — only the directories that hold them.
    """
    runtime_dir(workspace_root).mkdir(parents=True, exist_ok=True)
    active_runs_dir(workspace_root).mkdir(parents=True, exist_ok=True)
    control_dir(workspace_root).mkdir(parents=True, exist_ok=True)
    control_log_dir(workspace_root).mkdir(parents=True, exist_ok=True)


__all__ = [
    "runtime_dir",
    "lock_path",
    "heartbeat_path",
    "active_runs_dir",
    "active_run_path",
    "active_tournament_path",
    "control_dir",
    "control_log_dir",
    "control_command_path",
    "ensure_runtime_dirs",
]
