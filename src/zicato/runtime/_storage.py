"""Bridge between the ``runtime/`` domain and the storage seam.

The ``runtime/`` modules reach persistence through :class:`StorageBackend`
rather than reaching for the atomic-file helpers (:mod:`zicato.storage`)
and :class:`pathlib.Path` math directly. This module is the thin adapter that
makes that routing ergonomic without putting a backend in any public
``runtime/`` signature.

What it owns is key computation. ``runtime/`` state lives under the
``runtime/`` namespace of the workspace. The ``*_key`` helpers turn a
workspace coordinate into the logical storage key — the exact mirror of
the path helpers in :mod:`zicato.runtime.paths`, but yielding a backend
*key* (a ``/``-relative string) rather than an absolute :class:`Path`.

The backend comes from :func:`zicato.storage.workspace_backend`, the one
construction path in the tree, and ``runtime/`` asks it for an unstarted
one: ``runtime/`` writers already call
:func:`zicato.runtime.paths.ensure_runtime_dirs`, which creates the
directory tree, and readers tolerate a missing root.

Public ``runtime/`` functions keep their ``workspace_root: Path`` first
argument; internally they construct a backend and pass it one of the key
helpers. A caller cannot tell the implementation now flows through a
backend — the on-disk layout, the atomic-write discipline, and every
function signature are unchanged.
"""

from __future__ import annotations

#: The logical namespace every ``runtime/`` record sits under, mirroring
#: :func:`zicato.runtime.paths.runtime_dir`.
RUNTIME_NS = "runtime"


# --- key helpers (mirror zicato.runtime.paths, but yield storage keys) -----


def heartbeat_key() -> str:
    """Storage key for ``heartbeat.json``."""
    return f"{RUNTIME_NS}/heartbeat.json"


def lock_key() -> str:
    """Storage key for the workspace lock record."""
    return f"{RUNTIME_NS}/lock.json"


def active_tournament_key() -> str:
    """Storage key for the active-tournament SNAPSHOT record.

    Read only as a fallback: an ``active_tournament.json`` file with no
    event log beside it is still folded into a live view. Nothing writes
    this key — the live producer appends to the event log instead (see
    :func:`active_tournament_log_key`).
    """
    return f"{RUNTIME_NS}/active_tournament.json"


def active_tournament_log_key() -> str:
    """Storage key for the active-tournament EVENT LOG.

    The single-writer, append-only JSONL log that carries the in-progress
    tournament's live state. The orchestrator/runner appends
    one typed event per state transition (a full-envelope ``Snapshot``
    plus ``EntryUpdate`` / ``PartialAggregate`` / ``ProjectedUpdate``
    deltas); a reader folds the log into the live view. Single-writer
    append-only removes the snapshot's read-modify-write race.
    """
    return f"{RUNTIME_NS}/active_tournament.events.jsonl"


def progress_log_key() -> str:
    """Storage key for the ORCHESTRATOR progress EVENT LOG.

    A single-writer, append-only JSONL log the evolve loop appends one
    typed event to on each genuine orchestrator transition (round start,
    propose, apply, tournament start/settle, gate, promote/reject). Its
    monotonic ``seq`` is the TRUE liveness signal: it advances only on real
    progress, never on a timer, so a wedged loop whose heartbeat thread
    keeps stamping ``now()`` does not read as alive. The tail ``seq`` is
    stamped into ``heartbeat.json`` and the dashboard SSE frames.
    """
    return f"{RUNTIME_NS}/progress.events.jsonl"


def active_runs_prefix() -> str:
    """Storage key prefix the per-run live-state records sit under."""
    return f"{RUNTIME_NS}/active_runs"


def active_run_key(run_id: str) -> str:
    """Storage key for one run's live-state record."""
    return f"{active_runs_prefix()}/{run_id}.json"


def control_prefix() -> str:
    """Storage key prefix operator commands are dropped under."""
    return f"{RUNTIME_NS}/control"


def control_command_key(command: str) -> str:
    """Storage key for a control command file.

    ``command`` is taken verbatim as a relative path under the control
    prefix; it may contain a subdirectory component (e.g.
    ``"kill_runs/run_abc"``).
    """
    return f"{control_prefix()}/{command.strip('/')}"


def control_log_prefix() -> str:
    """Storage key prefix consumed commands are archived under."""
    return f"{RUNTIME_NS}/control_log"


def kill_request_key(run_id: str) -> str:
    """Storage key for one run's parent→supervisor kill-request marker.

    Lives under ``control/kill_requests/{run_id}`` (no ``.json`` suffix —
    the supervisor matches on the bare run id). Distinct from the
    operator's ``kill_runs/{run_id}`` channel: this one asks the Rust
    supervisor to run the single SIGTERM→grace→SIGKILL escalator on the
    worker pid, so the Python parent never signals the worker itself.
    """
    return f"{control_prefix()}/kill_requests/{run_id}"


__all__ = [
    "RUNTIME_NS",
    "heartbeat_key",
    "lock_key",
    "active_tournament_key",
    "active_tournament_log_key",
    "progress_log_key",
    "active_runs_prefix",
    "active_run_key",
    "control_prefix",
    "control_command_key",
    "control_log_prefix",
    "kill_request_key",
]
