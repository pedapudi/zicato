"""Absolute paths into the ``.zicato/runtime/`` state-file tree.

Pure path math — no I/O is performed here other than the explicit
:func:`ensure_runtime_dirs` helper which creates the directory tree.
Every other function returns a :class:`Path` and never touches the
filesystem.

Where each file lives is declared once, on
:class:`zicato.workspace.layout.WorkspaceLayout`; the functions below
resolve those declarations against a workspace root, and the ``*_key``
helpers in :mod:`zicato.runtime._storage` render the same declarations as
storage keys. Adding a runtime state file means adding one layout method.

The runtime tree is the read/write surface the orchestrator and the
external supervisor binary share. Layout (``workspace_root`` is the
``.zicato/`` directory itself, matching the convention every other
zicato helper uses)::

    {workspace_root}/runtime/
      lock.json                       # exclusive workspace lock
      heartbeat.json                  # orchestrator liveness beat
      dashboard.json                  # dashboard's actually-bound host/port
      active_runs/{run_id}.json       # per in-flight tournament run
      active_tournament.json          # current tournament shape
      control/                        # operator commands queued by dashboard
        pause_epoch                   # flag file
        skip_round                    # flag file
        kill_runs/{run_id}            # one file per kill target
        kill_requests/{run_id}        # parent->supervisor escalation request
        promote/{generation_id}       # one file per promote target
        reject/{generation_id}        # one file per reject target
        rubric_replacement.txt        # full rubric text payload
      control_log/                    # consumed commands persist here
"""

from __future__ import annotations

from pathlib import Path

from zicato.workspace.layout import WorkspaceLayout


def _layout(workspace_root: Path) -> WorkspaceLayout:
    """The path declarations for one workspace root.

    ``workspace_root`` is the ``.zicato/`` directory itself; nothing here
    prepends ``.zicato`` so the caller can name the workspace whatever it
    likes (the CLI passes ``.zicato`` by default, while tests and embedded
    usage may pass alternate names).
    """
    return WorkspaceLayout.from_root(workspace_root)


def runtime_dir(workspace_root: Path) -> Path:
    """Return ``runtime/`` for a workspace."""
    return _layout(workspace_root).runtime_dir


def lock_path(workspace_root: Path) -> Path:
    """Return the path to the workspace lock JSON file."""
    return _layout(workspace_root).lock


def heartbeat_path(workspace_root: Path) -> Path:
    """Return the path to the heartbeat JSON file."""
    return _layout(workspace_root).heartbeat


def dashboard_endpoint_path(workspace_root: Path) -> Path:
    """Return the path to the dashboard's bound-endpoint JSON file.

    See :attr:`~zicato.workspace.layout.WorkspaceLayout.dashboard_endpoint`
    for why the bound port is only knowable after the listener is up.
    """
    return _layout(workspace_root).dashboard_endpoint


def active_runs_dir(workspace_root: Path) -> Path:
    """Return the directory holding per-run live state JSONs."""
    return _layout(workspace_root).active_runs_dir


def active_run_path(workspace_root: Path, run_id: str) -> Path:
    """Return the path to one run's live-state JSON file."""
    return _layout(workspace_root).active_run(run_id)


def active_tournament_path(workspace_root: Path) -> Path:
    """Return the path to the LEGACY current-tournament snapshot file.

    Retained for the compat reader + resume cleanup. The live producer
    writes the event log (see :func:`active_tournament_log_path`); this
    snapshot is only read when no log exists.
    """
    return _layout(workspace_root).active_tournament


def active_tournament_log_path(workspace_root: Path) -> Path:
    """Return the path to the active-tournament EVENT LOG."""
    return _layout(workspace_root).active_tournament_log


def progress_log_path(workspace_root: Path) -> Path:
    """Return the path to the orchestrator progress EVENT LOG.

    The single-writer append-only JSONL whose monotonic ``seq`` is the
    true orchestrator-produced liveness signal (advances only on a genuine
    transition, never on the heartbeat timer).
    """
    return _layout(workspace_root).progress_log


def inconclusive_dir(workspace_root: Path) -> Path:
    """Return the dead-letter directory for inconclusive crowning duels.

    See :attr:`~zicato.workspace.layout.WorkspaceLayout.inconclusive_dir`
    for what lands here and why an absent directory is the norm.
    """
    return _layout(workspace_root).inconclusive_dir


def inconclusive_record_path(workspace_root: Path, generation_id: str) -> Path:
    """Return the dead-letter path for one inconclusive challenger generation."""
    return _layout(workspace_root).inconclusive_record(generation_id)


def control_dir(workspace_root: Path) -> Path:
    """Return the directory operator commands are dropped into."""
    return _layout(workspace_root).control_dir


def control_log_dir(workspace_root: Path) -> Path:
    """Return the directory consumed commands are archived in."""
    return _layout(workspace_root).control_log_dir


def control_command_path(workspace_root: Path, command: str) -> Path:
    """Return the path to a control command file (relative to ``control/``).

    The ``command`` argument is taken verbatim as a relative path under
    :func:`control_dir`. It may include subdirectories (e.g.
    ``"kill_runs/run_abc"``) — the kill/promote/reject commands keep
    one file per target underneath a per-command-kind subdirectory.
    """
    return _layout(workspace_root).control_command(command)


def kill_requests_dir(workspace_root: Path) -> Path:
    """Return the directory holding parent→supervisor kill-escalation requests."""
    return _layout(workspace_root).kill_requests_dir


def kill_request_path(workspace_root: Path, run_id: str) -> Path:
    """Return the path to one run's parent→supervisor kill-request marker."""
    return _layout(workspace_root).kill_request(run_id)


def ensure_runtime_dirs(workspace_root: Path) -> None:
    """Create the runtime directory tree (idempotent).

    Safe to call repeatedly; ``mkdir(parents=True, exist_ok=True)`` does
    not error if the tree already exists. Does NOT create any of the
    JSON state files themselves — only the directories that hold them.
    """
    layout = _layout(workspace_root)
    layout.runtime_dir.mkdir(parents=True, exist_ok=True)
    layout.active_runs_dir.mkdir(parents=True, exist_ok=True)
    layout.control_dir.mkdir(parents=True, exist_ok=True)
    layout.control_log_dir.mkdir(parents=True, exist_ok=True)


__all__ = [
    "runtime_dir",
    "lock_path",
    "heartbeat_path",
    "dashboard_endpoint_path",
    "active_runs_dir",
    "active_run_path",
    "active_tournament_path",
    "active_tournament_log_path",
    "progress_log_path",
    "inconclusive_dir",
    "inconclusive_record_path",
    "control_dir",
    "control_log_dir",
    "control_command_path",
    "kill_requests_dir",
    "kill_request_path",
    "ensure_runtime_dirs",
]
