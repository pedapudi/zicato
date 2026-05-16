"""Control-file protocol: dashboard → orchestrator.

The dashboard (and tests) write small files under ``.zicato/runtime/control/``
to request operator actions. The orchestrator polls
:func:`list_pending_commands` at safe points (between board entries, between
rounds) and consumes each via :func:`consume_command`, which atomically
moves the file into ``.zicato/runtime/control_log/`` with a JSON sidecar
recording when, by whom, and why.

Why files rather than an HTTP endpoint:

* The supervisor binary is a separate process (potentially a different
  language); a filesystem protocol works without negotiating a port.
* Crash-safety is trivial — an interrupted consume leaves the request
  in ``control/`` and the next orchestrator poll will retry.
* Operators can drop commands by hand (``touch .zicato/runtime/control/pause_epoch``)
  for emergency intervention without going through the dashboard.

Command surface
---------------

The recognized command names are the ``CMD_*`` constants below. The
dashboard converts user actions into one of these by writing a file at
the corresponding path. Two flavors:

* **Flag commands** (``pause_epoch``, ``skip_round``) — single file with
  no per-target argument. Empty payload (timestamp suffices).
* **Targeted commands** (``kill_runs/<run_id>``, ``promote/<gen_id>``,
  ``reject/<gen_id>``) — one file per target under a per-command
  subdirectory. The argument is encoded in the filename.
* **Payload command** (``rubric_replacement.txt``) — one file whose body
  IS the new rubric. The orchestrator reads it on consume and overwrites
  the epoch's rubric.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from zicato.runtime._storage import (
    backend_for,
    control_command_key,
    control_log_prefix,
)
from zicato.runtime.paths import (
    control_dir,
    control_log_dir,
    ensure_runtime_dirs,
)

# ---------------------------------------------------------------------------
# Command name constants
# ---------------------------------------------------------------------------

#: Flag command. Pauses the epoch at the next safe orchestrator point.
#: The orchestrator stops scheduling new board entries until the pause
#: flag is consumed and replaced with a corresponding "resume" gesture
#: (delete the file via the dashboard or via :func:`consume_command`).
CMD_PAUSE_EPOCH = "pause_epoch"

#: Flag command. Skips the remainder of the current round.
CMD_SKIP_ROUND = "skip_round"

#: Targeted command prefix. Files live at ``control/kill_runs/<run_id>``.
#: The orchestrator sends SIGTERM to the named run; if the run was
#: already finished, consume is a no-op.
CMD_KILL_RUN_PREFIX = "kill_runs"

#: Targeted command prefix. Files live at ``control/promote/<generation_id>``.
#: Force-promotes the named generation regardless of tournament outcome.
#: Operator overrides are audit-logged in the journal.
CMD_PROMOTE_PREFIX = "promote"

#: Targeted command prefix. Files live at ``control/reject/<generation_id>``.
#: Force-rejects the named generation regardless of tournament outcome.
CMD_REJECT_PREFIX = "reject"

#: Payload command. Single file ``control/rubric_replacement.txt`` whose
#: body is the operator-supplied replacement rubric text.
CMD_RUBRIC_REPLACEMENT = "rubric_replacement.txt"


# ---------------------------------------------------------------------------
# Command dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """A pending operator command parsed from the control directory.

    Fields
    ------
    name:
        The command kind. For targeted commands this is the prefix
        (``"kill_runs"``, ``"promote"``, ``"reject"``); the per-target
        id lives in :attr:`arg`. For flag and payload commands this is
        the full name.
    arg:
        Per-target argument for targeted commands (the run id, the
        generation id). Empty string for flag and payload commands.
    payload:
        Body of the file when it carries operator-supplied text (today
        only :data:`CMD_RUBRIC_REPLACEMENT`). Empty string otherwise.
    file_path:
        Absolute path to the on-disk command file. The orchestrator
        passes this back to :func:`consume_command` after acting on the
        request.
    """

    name: str
    arg: str = ""
    payload: str = ""
    file_path: Path = field(default_factory=Path)


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with seconds precision."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _audit_log_name(consumed_at: str, name: str, arg: str) -> str:
    """Compose the audit-log filename for a consumed command.

    Format: ``{iso}_{name}[_{arg}].json``. Colons in the timestamp are
    replaced with hyphens so the filename is portable across filesystems
    that disallow them (most notably Windows).
    """
    safe_ts = consumed_at.replace(":", "-")
    suffix = f"_{arg}" if arg else ""
    return f"{safe_ts}_{name}{suffix}.json"


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def list_pending_commands(workspace_root: Path) -> list[ControlCommand]:
    """Enumerate every pending command under ``.zicato/runtime/control/``.

    Returns an empty list when the directory is absent. The result is
    sorted (top-level commands first by name, then targeted commands
    sorted by ``(prefix, arg)``) so the orchestrator processes commands
    deterministically — useful for tests and for replaying audit logs.

    Half-written ``.tmp`` files (in the rare window of a racing write)
    are skipped.

    The control protocol is a *directory tree* — flag files at the top
    level, one file per target under a per-command-kind subdirectory —
    so enumerating it is a two-level filesystem walk rather than a flat
    keyed-record lookup. The storage backend's interface is deliberately
    keyed records (read/write/delete by key), not a tree walker; pushing
    a recursive-list method onto it to serve this one consumer would
    bloat the seam. The leaf I/O — :func:`write_command`'s writes and
    :func:`consume_command`'s audit-log write — does go through the
    backend; the tree traversal here stays path-based on purpose.
    """
    cdir = control_dir(workspace_root)
    if not cdir.exists():
        return []

    out: list[ControlCommand] = []

    # Top-level entries: flag files (pause_epoch / skip_round) and the
    # rubric-replacement payload file.
    for entry in sorted(cdir.iterdir()):
        if entry.name.endswith(".tmp"):
            continue
        if entry.is_file():
            if entry.name == CMD_RUBRIC_REPLACEMENT:
                payload = entry.read_text(encoding="utf-8")
                out.append(
                    ControlCommand(
                        name=CMD_RUBRIC_REPLACEMENT,
                        payload=payload,
                        file_path=entry,
                    )
                )
            else:
                out.append(ControlCommand(name=entry.name, file_path=entry))
        elif entry.is_dir():
            # Targeted command directory.
            prefix = entry.name
            for sub in sorted(entry.iterdir()):
                if sub.name.endswith(".tmp"):
                    continue
                if not sub.is_file():
                    continue
                out.append(ControlCommand(name=prefix, arg=sub.name, file_path=sub))
    return out


def is_paused(workspace_root: Path) -> bool:
    """Return ``True`` iff the pause-epoch flag file is present.

    Cheap predicate for the orchestrator's inner loops — checks for
    file existence without parsing JSON.
    """
    return (control_dir(workspace_root) / CMD_PAUSE_EPOCH).exists()


# ---------------------------------------------------------------------------
# Write side (used by dashboard + tests)
# ---------------------------------------------------------------------------


def write_command(workspace_root: Path, cmd: ControlCommand) -> Path:
    """Atomically enqueue a control command.

    Used by the dashboard's POST endpoints and by tests. Decides the
    on-disk path from :attr:`ControlCommand.name` and :attr:`arg`:

    * ``name == CMD_RUBRIC_REPLACEMENT`` → ``control/rubric_replacement.txt``
      with :attr:`payload` as the file body.
    * ``name in {CMD_PAUSE_EPOCH, CMD_SKIP_ROUND}`` → ``control/{name}``,
      empty file.
    * Otherwise (targeted command) → ``control/{name}/{arg}``, empty
      file. ``arg`` MUST be non-empty in this case; raises
      :class:`ValueError` if missing.

    Returns the absolute path of the written file.

    The write itself goes through the storage backend's atomic
    :meth:`~zicato.storage.StorageBackend.write_text`; the on-disk path is
    still computed here (and returned) because the control protocol is a
    directory-tree shape — one file per target under a per-command-kind
    subdirectory — and callers want the concrete path back.
    """
    ensure_runtime_dirs(workspace_root)
    backend = backend_for(workspace_root)
    cdir = control_dir(workspace_root)

    if cmd.name == CMD_RUBRIC_REPLACEMENT:
        backend.write_text(control_command_key(CMD_RUBRIC_REPLACEMENT), cmd.payload)
        return cdir / CMD_RUBRIC_REPLACEMENT

    if cmd.name in (CMD_PAUSE_EPOCH, CMD_SKIP_ROUND):
        backend.write_text(control_command_key(cmd.name), "")
        return cdir / cmd.name

    # Targeted command. Require an arg.
    if not cmd.arg:
        raise ValueError(
            f"control command {cmd.name!r} requires a non-empty arg "
            "(e.g. run_id for kill_runs, generation_id for promote/reject)"
        )
    backend.write_text(control_command_key(f"{cmd.name}/{cmd.arg}"), "")
    return cdir / cmd.name / cmd.arg


# ---------------------------------------------------------------------------
# Consume side (used by orchestrator)
# ---------------------------------------------------------------------------


def consume_command(
    workspace_root: Path,
    cmd: ControlCommand,
    *,
    source: str = "dashboard",
    reason: str = "",
) -> Path:
    """Move a consumed command into ``control_log/`` with a JSON sidecar.

    Atomically:

    1. Deletes the source file in ``control/``.
    2. Writes a JSON audit record under ``control_log/`` capturing the
       command name, argument, payload (if any), the consumer-supplied
       ``source`` (e.g. ``"dashboard"`` / ``"cli"`` / ``"watchdog"``),
       and a freeform ``reason``.

    The order is "write log, then delete source" so a crash mid-consume
    leaves both copies present (the orchestrator re-reads, the audit log
    has one extra entry — both observable, neither lost) rather than
    deleting without recording.

    Returns the absolute path of the audit-log file.

    If the source file is already gone (e.g. operator deleted it
    manually between :func:`list_pending_commands` and this call), the
    audit log is still written so the orchestrator's intent is recorded
    — the journal cares that the action was *taken*, not whether the
    file was on disk at the instant of action.
    """
    ensure_runtime_dirs(workspace_root)
    consumed_at = _utc_now_iso()
    log_name = _audit_log_name(consumed_at, cmd.name, cmd.arg)
    log_path = control_log_dir(workspace_root) / log_name
    record = {
        "command": cmd.name,
        "arg": cmd.arg,
        "payload": cmd.payload,
        "consumed_at": consumed_at,
        "source": source,
        "reason": reason,
        "original_file_path": str(cmd.file_path),
    }
    backend_for(workspace_root).write_json(f"{control_log_prefix()}/{log_name}", record)
    # Delete the source AFTER the log is durable.
    if cmd.file_path != Path() and cmd.file_path.exists():
        try:
            cmd.file_path.unlink()
        except OSError:
            # The file is gone (raced with another consumer or a manual
            # delete). The audit log is the authoritative record.
            pass
        # If the parent directory is now empty (targeted command
        # subdir), tidy it up so the dashboard's listing doesn't show
        # an empty bucket.
        parent = cmd.file_path.parent
        if parent != control_dir(workspace_root) and parent.exists():
            try:
                if not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
    return log_path


__all__ = [
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
]
