"""Bridge between the ``runtime/`` domain and the storage seam.

The ``runtime/`` modules historically reached straight for the atomic-file
helpers (:mod:`zicato.runtime._atomic`) and :class:`pathlib.Path` math. As
of the storage-layer refactor they go through :class:`StorageBackend`
instead — this module is the thin adapter that makes that routing
ergonomic without changing any public ``runtime/`` signature.

Two responsibilities:

* **Backend selection.** :func:`backend_for` constructs the canonical file
  backend for a workspace root. It is the single seam where ``runtime/``
  decides which backend it uses; swapping it (or honouring a config knob)
  is a change in exactly this one function.
* **Key computation.** ``runtime/`` state lives under the ``runtime/``
  namespace of the workspace. The ``*_key`` helpers turn a workspace
  coordinate into the logical storage key — the exact mirror of the path
  helpers in :mod:`zicato.runtime.paths`, but yielding a backend *key*
  (a ``/``-relative string) rather than an absolute :class:`Path`.

Public ``runtime/`` functions keep their ``workspace_root: Path`` first
argument; internally they call :func:`backend_for` and one of the key
helpers. A caller cannot tell the implementation now flows through a
backend — the on-disk layout, the atomic-write discipline, and every
function signature are unchanged.
"""

from __future__ import annotations

from pathlib import Path

from zicato.storage import FileStorageBackend, StorageBackend

#: The logical namespace every ``runtime/`` record sits under, mirroring
#: :func:`zicato.runtime.paths.runtime_dir`.
RUNTIME_NS = "runtime"


def backend_for(workspace_root: Path) -> StorageBackend:
    """Return the canonical storage backend for a workspace.

    ``runtime/`` records are the live-state surface the orchestrator
    writes and the supervisor + dashboard read; files are their canonical
    store (one keyed record per file means a misbehaving run's blast
    radius is one file). This returns a :class:`FileStorageBackend` rooted
    at the workspace.

    The backend is intentionally *not* started here: ``runtime/`` writers
    already call :func:`zicato.runtime.paths.ensure_runtime_dirs` (which
    creates the directory tree), and readers tolerate a missing root. A
    cheap unstarted backend keeps read-only callers side-effect-free.
    """
    return FileStorageBackend(workspace_root)


# --- key helpers (mirror zicato.runtime.paths, but yield storage keys) -----


def heartbeat_key() -> str:
    """Storage key for ``heartbeat.json``."""
    return f"{RUNTIME_NS}/heartbeat.json"


def lock_key() -> str:
    """Storage key for the workspace lock record."""
    return f"{RUNTIME_NS}/lock.json"


def active_tournament_key() -> str:
    """Storage key for the legacy active-tournament SNAPSHOT record.

    Retained only for the compat reader: an ``active_tournament.json``
    snapshot written by a pre-RUNTIME-V2 producer (or a hand-edited
    file) is still read when no event log is present. The live producer
    no longer writes it — see :func:`active_tournament_log_key`.
    """
    return f"{RUNTIME_NS}/active_tournament.json"


def active_tournament_log_key() -> str:
    """Storage key for the active-tournament EVENT LOG (RUNTIME-V2 Phase 3).

    The single-writer, append-only JSONL log that replaces the mutable
    ``active_tournament.json`` snapshot. The orchestrator/runner appends
    one typed event per state transition (a full-envelope ``Snapshot``
    plus ``EntryUpdate`` / ``PartialAggregate`` / ``ProjectedUpdate``
    deltas); a reader folds the log into the live view. Single-writer
    append-only removes the snapshot's read-modify-write race.
    """
    return f"{RUNTIME_NS}/active_tournament.events.jsonl"


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
    "backend_for",
    "heartbeat_key",
    "lock_key",
    "active_tournament_key",
    "active_tournament_log_key",
    "active_runs_prefix",
    "active_run_key",
    "control_prefix",
    "control_command_key",
    "control_log_prefix",
    "kill_request_key",
]
