"""Runtime state layer for the supervisor + dashboard.

This subpackage owns the read/write contract for ``.zicato/runtime/``
state files. The orchestrator writes here; the (Rust) supervisor binary
and the dashboard read here. Both directions use the same dataclass +
atomic-file helpers exposed by this module's public surface.

Re-exports are organized by file:

* :mod:`zicato.runtime.paths` — path math + directory creation.
* :mod:`zicato.runtime.state` — typed dataclasses + load/save for the
  heartbeat, active runs, and active tournament.
* :mod:`zicato.runtime.heartbeat` — async :class:`HeartbeatBeater`
  background task.
* :mod:`zicato.runtime.control` — control-file protocol (operator
  commands flow dashboard → orchestrator via files under
  ``.zicato/runtime/control/``).
* :mod:`zicato.runtime.lock` — pid-based workspace lock with stale-pid
  stealing.

The orchestrator wiring (when does the orchestrator actually call into
this module) lands in a separate change once Round 6+7 stabilizes the
evolve loop. This subpackage is pure state plumbing.
"""

from __future__ import annotations

from zicato.runtime.control import (
    CMD_KILL_RUN_PREFIX,
    CMD_PAUSE_EPOCH,
    CMD_PROMOTE_PREFIX,
    CMD_REJECT_PREFIX,
    CMD_RUBRIC_REPLACEMENT,
    CMD_SKIP_ROUND,
    ControlCommand,
    consume_command,
    is_paused,
    list_pending_commands,
    write_command,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import (
    WorkspaceLock,
    WorkspaceLockHeld,
    acquire_workspace_lock,
    is_pid_alive,
    release_workspace_lock,
)
from zicato.runtime.paths import (
    active_run_path,
    active_runs_dir,
    active_tournament_path,
    control_command_path,
    control_dir,
    control_log_dir,
    ensure_runtime_dirs,
    heartbeat_path,
    lock_path,
    runtime_dir,
)
from zicato.runtime.resume import (
    ResumePlan,
    clear_runtime_state,
    prepare_resume,
)
from zicato.runtime.state import (
    ActiveRun,
    ActiveTournament,
    ActiveTournamentEntry,
    Heartbeat,
    clear_active_tournament,
    list_active_runs,
    read_active_tournament,
    read_heartbeat,
    remove_active_run,
    touch_active_run_progress,
    update_tournament_entry,
    write_active_run,
    write_active_tournament,
    write_heartbeat,
)

__all__ = [
    # paths
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
    # state dataclasses
    "Heartbeat",
    "ActiveRun",
    "ActiveTournamentEntry",
    "ActiveTournament",
    # state helpers
    "read_heartbeat",
    "write_heartbeat",
    "list_active_runs",
    "write_active_run",
    "remove_active_run",
    "touch_active_run_progress",
    "read_active_tournament",
    "write_active_tournament",
    "update_tournament_entry",
    "clear_active_tournament",
    # heartbeat task
    "HeartbeatBeater",
    # control
    "CMD_PAUSE_EPOCH",
    "CMD_SKIP_ROUND",
    "CMD_KILL_RUN_PREFIX",
    "CMD_PROMOTE_PREFIX",
    "CMD_REJECT_PREFIX",
    "CMD_RUBRIC_REPLACEMENT",
    "ControlCommand",
    "list_pending_commands",
    "is_paused",
    "write_command",
    "consume_command",
    # lock
    "WorkspaceLockHeld",
    "WorkspaceLock",
    "is_pid_alive",
    "acquire_workspace_lock",
    "release_workspace_lock",
    # resume
    "ResumePlan",
    "clear_runtime_state",
    "prepare_resume",
]
