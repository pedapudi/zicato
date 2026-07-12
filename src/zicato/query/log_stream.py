"""log_stream — the ONE reader for the structured log streams.

Files-canonical (LOGGING.md §3): the ``.zicato/logs/<stamp>-<pid>.jsonl``
files are the source of truth, and this module is the single parser both
the ``zicato logs`` CLI and the dashboard ``/api/logs`` endpoint read
through. ``build_log_view`` mirrors ``query/run_log.build_run_log``: a
tail with a monotone line cursor for append-only follow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.query.paths import WorkspacePaths

#: Default / ceiling for the ``?limit=`` tail, mirroring the run-log tail.
LOG_DEFAULT_LIMIT = 200
LOG_MAX_LIMIT = 2000

#: The stream filename suffix (kept local so the reader has no import-time
#: dependency on the writer module).
_STREAM_SUFFIX = ".jsonl"


def clamp_log_limit(requested: int | None) -> int:
    """Clamp a requested tail limit into ``[1, LOG_MAX_LIMIT]``."""
    if requested is None or requested <= 0:
        return LOG_DEFAULT_LIMIT
    return min(requested, LOG_MAX_LIMIT)


def _level_value(name: str | None) -> int:
    """Numeric value of a stdlib level NAME; unknown / absent → 0 (DEBUG-)."""
    if not name:
        return 0
    val = logging.getLevelName(str(name).upper())
    return val if isinstance(val, int) else 0


@dataclass(frozen=True)
class Invocation:
    """One log stream on disk (metadata only, never the records)."""

    id: str
    stamp: str
    pid: int | None
    path: Path
    size: int
    mtime: float

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stamp": self.stamp,
            "pid": self.pid,
            "size": self.size,
            "mtime": self.mtime,
        }


def _parse_invocation_id(stem: str) -> tuple[str, int | None]:
    """Split a ``<stamp>-<pid>`` stem into ``(stamp, pid | None)``.

    A stem that does not match (no trailing ``-<int>``) yields
    ``(stem, None)`` so a hand-dropped file still lists rather than
    breaking enumeration.
    """
    idx = stem.rfind("-")
    if idx <= 0 or idx == len(stem) - 1:
        return stem, None
    tail = stem[idx + 1 :]
    try:
        return stem[:idx], int(tail)
    except ValueError:
        return stem, None


def list_invocations(paths: WorkspacePaths) -> list[Invocation]:
    """Every stream under ``.zicato/logs/``, NEWEST first.

    Newest = largest filename (the stamp leads and sorts lexically). A
    missing directory yields an empty list — the honest no-logs state.
    """
    directory = paths.logs
    try:
        files = [p for p in directory.iterdir() if p.is_file() and p.suffix == _STREAM_SUFFIX]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    out: list[Invocation] = []
    for p in sorted(files, key=lambda q: q.name, reverse=True):
        stamp, pid = _parse_invocation_id(p.stem)
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        out.append(Invocation(id=p.stem, stamp=stamp, pid=pid, path=p, size=size, mtime=mtime))
    return out


def resolve_invocation(paths: WorkspacePaths, selector: str | None) -> Invocation | None:
    """Resolve a selector (``"latest"`` / ``None`` / a specific id) to a stream.

    ``None`` / ``"latest"`` → the newest stream. Any other value → the
    stream whose id matches exactly. Returns ``None`` when nothing
    resolves (no streams, or an unknown id).
    """
    invocations = list_invocations(paths)
    if not invocations:
        return None
    if selector is None or selector == "latest" or selector == "":
        return invocations[0]
    for inv in invocations:
        if inv.id == selector:
            return inv
    return None


def _parse_record_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL record; malformed / non-object → ``None`` (skipped)."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def tail_records(
    path: Path,
    *,
    limit: int,
    level: str | None = None,
    after: int | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Read a stream's records with a level filter + line cursor.

    Returns ``(records, cursor)``. Each returned record carries an added
    ``cursor`` (its line index in the file) so a follower can advance.
    ``cursor`` (the second element) is the largest line index in the
    file, or ``None`` when the file is empty / unreadable.

    * ``level`` keeps only records at or above that level name.
    * ``after`` returns only records whose line index is strictly greater
      (append-only follow); ``None`` returns the last ``limit`` matches.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return [], None

    threshold = _level_value(level)
    lines = text.splitlines()
    file_cursor: int | None = len(lines) - 1 if lines else None

    matched: list[dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        rec = _parse_record_line(raw)
        if rec is None:
            continue
        if threshold and _level_value(rec.get("level")) < threshold:
            continue
        if after is not None and idx <= after:
            continue
        rec = {**rec, "cursor": idx}
        matched.append(rec)

    if after is None and len(matched) > limit:
        matched = matched[-limit:]
    elif after is not None and len(matched) > limit:
        matched = matched[-limit:]
    return matched, file_cursor


def build_log_view(
    paths: WorkspacePaths,
    *,
    limit: int,
    level: str | None = None,
    after: int | None = None,
    invocation: str | None = None,
) -> dict[str, Any]:
    """``GET /api/logs`` body — the operator-log tail + an append cursor.

    Returns::

        {
          "records": [...],          # newest-last, each with a `cursor`
          "cursor": <int|None>,      # largest line index in the file
          "invocation": <id|None>,   # the resolved stream id
          "invocations": [...],      # the roster, newest first (for a picker)
          "level": <str|None>,       # the applied level filter (echoed)
        }

    An empty / no-logs workspace degrades honestly: ``records: []``,
    ``cursor: null``, ``invocation: null`` — never an error.
    """
    invocations = list_invocations(paths)
    resolved = resolve_invocation(paths, invocation)
    if resolved is None:
        return {
            "records": [],
            "cursor": None,
            "invocation": None,
            "invocations": [inv.to_json() for inv in invocations],
            "level": level,
        }
    records, cursor = tail_records(resolved.path, limit=limit, level=level, after=after)
    return {
        "records": records,
        "cursor": cursor,
        "invocation": resolved.id,
        "invocations": [inv.to_json() for inv in invocations],
        "level": level,
    }


__all__ = [
    "LOG_DEFAULT_LIMIT",
    "LOG_MAX_LIMIT",
    "Invocation",
    "build_log_view",
    "clamp_log_limit",
    "list_invocations",
    "resolve_invocation",
    "tail_records",
]
