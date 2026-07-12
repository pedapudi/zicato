"""Structured logging — one JSONL stream per evolve/reflect invocation.

See ``docs/design/LOGGING.md`` for the model. The short version:

* the orchestrator entrypoint calls :func:`install_log_stream`, which
  attaches a :class:`JsonlStreamHandler` to the ``zicato`` logger so
  EVERY existing ``zicato.*`` ``log.*`` call is structured into
  ``.zicato/logs/<utc-stamp>-<pid>.jsonl`` with ZERO call-site changes;
* each tournament worker subprocess re-installs the same handler pointed
  at the SAME file (append mode) via :func:`install_worker_log_stream`,
  so worker records reach the one invocation stream (POSIX ``O_APPEND``
  keeps single-line writes atomic across the handful of parallel workers);
* the optional ``epoch_id`` / ``generation_id`` / ``run_id`` on each
  record come from a :mod:`contextvars` binding (:func:`bind_log_context`)
  read by :class:`LogContextFilter` — never a per-call ``extra=``, so no
  log statement changes.

Logs are OBSERVABILITY. Nothing in scoring / gate / journal reads them
back (LOGGING.md §invariant).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The workspace subdirectory the streams live under (sibling of
#: ``runtime/`` and ``epochs/``).
LOGS_DIRNAME = "logs"

#: The logger the handler attaches to. Every ``zicato.*`` child logger
#: reaches it by propagation; third-party loggers (goldfive, httpx,
#: asyncio) do NOT, so the stream is not flooded with library chatter.
ZICATO_LOGGER_NAME = "zicato"

#: Default capture floor. The stream keeps INFO and above by default; the
#: reader re-filters on read (``--level``). Overridable via
#: ``install_log_stream(level=...)`` or the ``ZICATO_LOG_LEVEL`` env var.
DEFAULT_CAPTURE_LEVEL = logging.INFO

#: Retention: keep at most this many invocation streams (LOGGING.md §4).
#: Pruned at install time, oldest first. The single source of truth.
MAX_RETAINED_INVOCATIONS = 20

#: The stream filename suffix.
_STREAM_SUFFIX = ".jsonl"


# ---------------------------------------------------------------------------
# Run-context binding (contextvars — never a per-call ``extra=``).
# ---------------------------------------------------------------------------

_epoch_id: ContextVar[str | None] = ContextVar("zicato_log_epoch_id", default=None)
_generation_id: ContextVar[str | None] = ContextVar("zicato_log_generation_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("zicato_log_run_id", default=None)


def set_log_context(
    *,
    epoch_id: str | None = None,
    generation_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Bind run context onto every subsequent record in THIS process.

    Only the keyword arguments passed (non-``None``) are set; an argument
    left ``None`` leaves that field's current binding untouched. Used by
    the worker's ``main()`` (full context, once) — a fire-and-forget set
    for the life of the process.
    """
    if epoch_id is not None:
        _epoch_id.set(epoch_id)
    if generation_id is not None:
        _generation_id.set(generation_id)
    if run_id is not None:
        _run_id.set(run_id)


@contextlib.contextmanager
def bind_log_context(
    *,
    epoch_id: str | None = None,
    generation_id: str | None = None,
    run_id: str | None = None,
) -> Iterator[None]:
    """Scope a run-context binding to a ``with`` block, then restore.

    Used by the round loop to tag every record emitted during one round
    with its ``epoch_id`` (and, where known, the generation/run) without
    leaking the binding past the round. Each field is reset to its prior
    value on exit.
    """
    tokens = []
    if epoch_id is not None:
        tokens.append((_epoch_id, _epoch_id.set(epoch_id)))
    if generation_id is not None:
        tokens.append((_generation_id, _generation_id.set(generation_id)))
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_log_context() -> dict[str, str]:
    """The currently-bound run context as a ``{field: value}`` dict.

    Only truthy fields appear — an unbound field is absent, never a
    ``null``. Read by :class:`LogContextFilter` and exposed for tests.
    """
    out: dict[str, str] = {}
    e = _epoch_id.get()
    g = _generation_id.get()
    r = _run_id.get()
    if e:
        out["epoch_id"] = e
    if g:
        out["generation_id"] = g
    if r:
        out["run_id"] = r
    return out


class LogContextFilter(logging.Filter):
    """Copy the currently-bound run context onto each record.

    A ``logging.Filter`` runs on the emitting thread as the record is
    handled, so it reads the live :mod:`contextvars` binding. It always
    returns ``True`` (it filters nothing — it only enriches).
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — stdlib name
        ctx = current_log_context()
        record.zicato_epoch_id = ctx.get("epoch_id")
        record.zicato_generation_id = ctx.get("generation_id")
        record.zicato_run_id = ctx.get("run_id")
        return True


# ---------------------------------------------------------------------------
# Record → JSONL line.
# ---------------------------------------------------------------------------


def _iso_millis(created: float) -> str:
    """ISO-8601 UTC timestamp (millisecond resolution) for a record time."""
    dt = _dt.datetime.fromtimestamp(created, _dt.UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    """Project a :class:`logging.LogRecord` to the LOGGING.md §1 shape.

    Required keys (``ts`` / ``level`` / ``component`` / ``message``) are
    always present; the optional context keys and ``fields`` appear only
    when bound / supplied. ``component`` is the logger name verbatim, so
    no call site has to name itself.
    """
    out: dict[str, Any] = {
        "ts": _iso_millis(record.created),
        "level": record.levelname,
        "component": record.name,
        "message": record.getMessage(),
    }
    epoch_id = getattr(record, "zicato_epoch_id", None)
    generation_id = getattr(record, "zicato_generation_id", None)
    run_id = getattr(record, "zicato_run_id", None)
    if epoch_id:
        out["epoch_id"] = epoch_id
    if generation_id:
        out["generation_id"] = generation_id
    if run_id:
        out["run_id"] = run_id
    # A structured ``extra={"fields": {...}}`` rides through verbatim when
    # a call site opts in; absent (the common case) the key is omitted.
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict) and fields:
        out["fields"] = fields
    return out


class JsonlFormatter(logging.Formatter):
    """Format a record as one JSON object (no trailing newline).

    The handler adds the terminator. Serialisation is defensive:
    ``default=str`` so a stray non-JSON value in ``fields`` degrades to
    its ``repr`` rather than raising inside logging.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 — stdlib name
        return json.dumps(record_to_dict(record), ensure_ascii=False, default=str)


class JsonlStreamHandler(logging.FileHandler):
    """A ``FileHandler`` that writes structured JSONL records.

    ``FileHandler`` opens the file with mode ``"a"`` → ``O_APPEND`` on
    POSIX, which is what makes concurrent worker appends atomic per line
    (LOGGING.md §2). ``delay=True`` defers the open until the first
    record, so installing a stream that never emits creates no empty file
    contention.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(str(path), mode="a", encoding="utf-8", delay=True)
        self.setFormatter(JsonlFormatter())
        self.addFilter(LogContextFilter())


# ---------------------------------------------------------------------------
# Path layout + retention.
# ---------------------------------------------------------------------------


def logs_dir(workspace_root: Path | str) -> Path:
    """The ``.zicato/logs/`` directory for a workspace root."""
    return Path(workspace_root) / LOGS_DIRNAME


def invocation_id(stamp: str, pid: int) -> str:
    """The ``<stamp>-<pid>`` id that names one invocation's stream."""
    return f"{stamp}-{pid}"


def stream_path(workspace_root: Path | str, stamp: str, pid: int) -> Path:
    """The stream file path for one invocation."""
    return logs_dir(workspace_root) / f"{invocation_id(stamp, pid)}{_STREAM_SUFFIX}"


def _utc_stamp(now: _dt.datetime | None = None) -> str:
    """A ``YYYYMMDDTHHMMSSZ`` UTC stamp (sorts lexically = chronologically)."""
    dt = now or _dt.datetime.now(_dt.UTC)
    return dt.astimezone(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def list_stream_files(directory: Path) -> list[Path]:
    """Every stream file in ``directory``, oldest → newest by filename.

    The filename leads with the UTC stamp, so a plain lexical sort is
    chronological. A missing directory yields an empty list.
    """
    try:
        files = [p for p in directory.iterdir() if p.is_file() and p.suffix == _STREAM_SUFFIX]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    return sorted(files, key=lambda p: p.name)


def prune_streams(directory: Path, keep: int) -> list[Path]:
    """Delete the oldest streams so at most ``keep`` remain; best-effort.

    Returns the paths actually removed. A delete failure is swallowed (the
    file is simply left) — retention never blocks a run. ``keep <= 0``
    prunes everything.
    """
    files = list_stream_files(directory)
    if len(files) <= max(0, keep):
        return []
    to_remove = files[: len(files) - max(0, keep)]
    removed: list[Path] = []
    for p in to_remove:
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Install / teardown.
# ---------------------------------------------------------------------------


@dataclass
class LogStreamHandle:
    """The teardown handle a caller closes to remove an installed stream."""

    path: Path
    handler: logging.Handler
    _logger: logging.Logger
    _prior_level: int
    _restore_level: bool

    def close(self) -> None:
        """Remove the handler and restore the logger's prior level."""
        with contextlib.suppress(Exception):
            self._logger.removeHandler(self.handler)
        with contextlib.suppress(Exception):
            self.handler.close()
        if self._restore_level:
            with contextlib.suppress(Exception):
                self._logger.setLevel(self._prior_level)


def _resolve_level(level: int | str | None) -> int:
    """Resolve a level from an int / name / env var, defaulting to INFO."""
    if level is None:
        env = os.environ.get("ZICATO_LOG_LEVEL")
        if env:
            level = env
        else:
            return DEFAULT_CAPTURE_LEVEL
    if isinstance(level, int):
        return level
    named = logging.getLevelName(str(level).upper())
    return named if isinstance(named, int) else DEFAULT_CAPTURE_LEVEL


def _attach(path: Path, level: int) -> LogStreamHandle:
    """Attach a stream handler to the ``zicato`` logger at ``level``."""
    logger = logging.getLogger(ZICATO_LOGGER_NAME)
    # De-dup safety: remove any JSONL stream handler a prior install left on
    # the logger (e.g. a CLI command whose exception path skipped teardown,
    # or repeated in-process CLI invocations in a test). Keeps handlers from
    # accumulating and each stream write from being duplicated.
    for existing in list(logger.handlers):
        if isinstance(existing, JsonlStreamHandler):
            logger.removeHandler(existing)
            with contextlib.suppress(Exception):
                existing.close()
    prior_level = logger.level
    # The logger's effective level gates whether child records reach the
    # handler; lower it to the capture floor when it is currently stricter
    # (or unset → NOTSET/0 which inherits root's WARNING).
    restore_level = False
    if prior_level == logging.NOTSET or prior_level > level:
        logger.setLevel(level)
        restore_level = True
    handler = JsonlStreamHandler(path)
    handler.setLevel(level)
    logger.addHandler(handler)
    return LogStreamHandle(
        path=path,
        handler=handler,
        _logger=logger,
        _prior_level=prior_level,
        _restore_level=restore_level,
    )


def install_log_stream(
    workspace_root: Path | str,
    *,
    level: int | str | None = None,
    pid: int | None = None,
    now: _dt.datetime | None = None,
) -> LogStreamHandle:
    """Install THE per-invocation stream for the orchestrator process.

    Creates ``.zicato/logs/``, prunes to :data:`MAX_RETAINED_INVOCATIONS`
    (the new stream becomes the newest), and attaches a
    :class:`JsonlStreamHandler` to the ``zicato`` logger so every existing
    ``zicato.*`` ``log.*`` call is captured. Returns a
    :class:`LogStreamHandle` the entrypoint closes in its ``finally``.

    Best-effort: if the directory cannot be created the stream still
    installs (the handler opens lazily and simply no-ops on write errors),
    so logging setup can never fail a run.
    """
    resolved_pid = os.getpid() if pid is None else pid
    stamp = _utc_stamp(now)
    directory = logs_dir(workspace_root)
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
    # Prune to keep-1 BEFORE adding this one, so the post-install count is
    # at most MAX_RETAINED_INVOCATIONS.
    prune_streams(directory, MAX_RETAINED_INVOCATIONS - 1)
    path = stream_path(workspace_root, stamp, resolved_pid)
    return _attach(path, _resolve_level(level))


def current_log_stream_path() -> Path | None:
    """The path of the stream currently installed on the ``zicato`` logger.

    The tournament runner reads this in the orchestrator process to thread
    the invocation stream path into each worker's args file, so a worker
    can APPEND to the same file (LOGGING.md §2). Returns ``None`` when no
    stream is installed (an ad-hoc / test drive) — the worker then logs to
    stderr only.
    """
    logger = logging.getLogger(ZICATO_LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, JsonlStreamHandler):
            base = getattr(handler, "baseFilename", None)
            if base:
                return Path(base)
    return None


def install_worker_log_stream(
    log_stream_path: Path | str | None,
    *,
    epoch_id: str | None = None,
    generation_id: str | None = None,
    run_id: str | None = None,
    level: int | str | None = None,
) -> LogStreamHandle | None:
    """Install the worker-side stream, APPENDING to the parent's file.

    The worker's ``main()`` calls this with the ``log_stream_path`` the
    orchestrator threaded through the args file. It binds the run context
    (so every worker record is attributed) and attaches the same JSONL
    handler in append mode. Returns ``None`` when no path is supplied (an
    ad-hoc / test worker drive) — the worker then keeps only its stderr
    ``basicConfig``, exactly as before this surface existed.
    """
    if not log_stream_path:
        return None
    set_log_context(epoch_id=epoch_id, generation_id=generation_id, run_id=run_id)
    return _attach(Path(log_stream_path), _resolve_level(level))


__all__ = [
    "DEFAULT_CAPTURE_LEVEL",
    "LOGS_DIRNAME",
    "MAX_RETAINED_INVOCATIONS",
    "ZICATO_LOGGER_NAME",
    "JsonlFormatter",
    "JsonlStreamHandler",
    "LogContextFilter",
    "LogStreamHandle",
    "bind_log_context",
    "current_log_context",
    "current_log_stream_path",
    "install_log_stream",
    "install_worker_log_stream",
    "invocation_id",
    "list_stream_files",
    "logs_dir",
    "prune_streams",
    "record_to_dict",
    "set_log_context",
    "stream_path",
]
