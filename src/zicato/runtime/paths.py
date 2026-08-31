"""Path helpers for the ``.zicato/runtime/`` state-file tree.

Pure path math — no I/O is performed here other than the explicit
:func:`ensure_runtime_dirs` helper which creates the directory tree.
Every other function returns a :class:`Path` and never touches the
filesystem.

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


def runtime_dir(workspace_root: Path) -> Path:
    """Return ``runtime/`` for a workspace.

    ``workspace_root`` is the ``.zicato/`` directory itself; the helper
    does NOT prepend ``.zicato`` so the caller can name the workspace
    whatever it likes (the CLI passes ``.zicato`` by default but tests
    and embedded usage may pass alternate names).
    """
    return workspace_root / "runtime"


def lock_path(workspace_root: Path) -> Path:
    """Return the path to the workspace lock JSON file."""
    return runtime_dir(workspace_root) / "lock.json"


def heartbeat_path(workspace_root: Path) -> Path:
    """Return the path to the heartbeat JSON file."""
    return runtime_dir(workspace_root) / "heartbeat.json"


def dashboard_endpoint_path(workspace_root: Path) -> Path:
    """Return the path to the dashboard's bound-endpoint JSON file.

    The standalone dashboard service walks ``+1`` from its preferred
    port if that port is taken, so the port it ends up serving on is not
    knowable up front. The service writes the host/port it actually
    bound to this file once the listener is up; ``zicato evolve`` reads
    it back to report the dashboard's real URL instead of guessing.
    """
    return runtime_dir(workspace_root) / "dashboard.json"


def active_runs_dir(workspace_root: Path) -> Path:
    """Return the directory holding per-run live state JSONs."""
    return runtime_dir(workspace_root) / "active_runs"


def active_run_path(workspace_root: Path, run_id: str) -> Path:
    """Return the path to one run's live-state JSON file."""
    return active_runs_dir(workspace_root) / f"{run_id}.json"


def active_tournament_path(workspace_root: Path) -> Path:
    """Return the path to the LEGACY current-tournament snapshot file.

    Retained for the compat reader + resume cleanup. The live producer
    writes the event log (see :func:`active_tournament_log_path`); this
    snapshot is only read when no log exists.
    """
    return runtime_dir(workspace_root) / "active_tournament.json"


def active_tournament_log_path(workspace_root: Path) -> Path:
    """Return the path to the active-tournament EVENT LOG."""
    return runtime_dir(workspace_root) / "active_tournament.events.jsonl"


def progress_log_path(workspace_root: Path) -> Path:
    """Return the path to the orchestrator progress EVENT LOG.

    The single-writer append-only JSONL whose monotonic ``seq`` is the
    true orchestrator-produced liveness signal (advances only on a genuine
    transition, never on the heartbeat timer).
    """
    return runtime_dir(workspace_root) / "progress.events.jsonl"


def inconclusive_dir(workspace_root: Path) -> Path:
    """Return the dead-letter directory for inconclusive crowning duels.

    The Bradley--Terry promotion pre-gate (opt-in) records here any crowning
    duel whose rating CIs never separated after its replicate budget was spent
    — a terminal ``"inconclusive"`` verdict. One JSON file per generation
    (:func:`inconclusive_record_path`) captures the unresolved duel + its final
    CIs so nothing is silently dropped. The directory is created lazily by the
    writer; an absent directory simply means no inconclusive duel was ever
    recorded (the default for every run that did not opt into the pre-gate).
    """
    return runtime_dir(workspace_root) / "inconclusive"


def inconclusive_record_path(workspace_root: Path, generation_id: str) -> Path:
    """Return the dead-letter path for one inconclusive challenger generation."""
    return inconclusive_dir(workspace_root) / f"{generation_id}.json"


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


#: Subdirectory of ``control/`` holding parent→supervisor kill-escalation
#: requests. Distinct from the operator's ``kill_runs/`` channel (which the
#: orchestrator consumes): a ``kill_requests/{run_id}`` marker is written by
#: the *Python parent* when a worker overran its budget, asking the *Rust
#: supervisor* to run the single SIGTERM→grace→SIGKILL escalator on that
#: worker's pid. Consolidating escalation in the supervisor removes the
#: parent↔supervisor race over the same worker pid.
KILL_REQUESTS_DIRNAME = "kill_requests"


def kill_requests_dir(workspace_root: Path) -> Path:
    """Return the directory holding parent→supervisor kill-escalation requests."""
    return control_dir(workspace_root) / KILL_REQUESTS_DIRNAME


def kill_request_path(workspace_root: Path, run_id: str) -> Path:
    """Return the path to one run's parent→supervisor kill-request marker."""
    return kill_requests_dir(workspace_root) / run_id


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
    "KILL_REQUESTS_DIRNAME",
    "kill_requests_dir",
    "kill_request_path",
    "ensure_runtime_dirs",
]
