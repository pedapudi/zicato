"""log_stream — the ONE reader for the structured log streams.

Files-canonical (LOGGING.md §3): the ``.zicato/logs/<stamp>-<pid>.jsonl``
files are the source of truth, and this module is the single parser both
the ``zicato inspect logs`` CLI and the dashboard ``/api/logs`` endpoint read
through. ``build_log_view`` mirrors ``query/run_log.build_run_log``: a
tail with a monotone line cursor for append-only follow.
"""

from __future__ import annotations

import json
import logging
import os
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

#: Reverse-tail byte budget for the INITIAL (``after is None``) tail. The
#: reader block-reads backward from EOF looking for ``limit`` complete lines
#: but never scans more than this many bytes, so the initial paint's read +
#: RSS is bounded no matter how large the stream has grown: a 250 MB stream
#: costs the same bounded read as a small one on every follow tick.
#: Records older than the budget are simply not in the initial tail; the
#: ``after=`` byte cursor then streams everything appended from there on.
_TAIL_BYTE_BUDGET = 4 * 1024 * 1024

#: Block size for the backward read.
_TAIL_BLOCK = 64 * 1024


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


def _records_from_bytes(
    data: bytes,
    base_offset: int,
    *,
    threshold: int,
    skip_leading_partial: bool,
) -> list[dict[str, Any]]:
    """Parse COMPLETE (newline-terminated) JSONL lines out of ``data``.

    Each returned record carries a ``cursor`` = the byte offset just PAST
    its terminating newline (i.e. where the next line begins), so passing
    it back as ``after=`` seeks straight to the following record. An
    incomplete trailing line (no ``\\n`` yet — a record mid-write) is left
    unparsed. When ``skip_leading_partial`` is set (a reverse read that
    started mid-line) the bytes before the first newline are discarded.
    """
    out: list[dict[str, Any]] = []
    start = 0
    if skip_leading_partial:
        nl = data.find(b"\n")
        if nl == -1:
            return out
        start = nl + 1
    while True:
        nl = data.find(b"\n", start)
        if nl == -1:
            break
        raw = data[start:nl].decode("utf-8", "replace")
        end_offset = base_offset + nl + 1
        start = nl + 1
        rec = _parse_record_line(raw)
        if rec is None:
            continue
        if threshold and _level_value(rec.get("level")) < threshold:
            continue
        out.append({**rec, "cursor": end_offset})
    return out


def tail_records(
    path: Path,
    *,
    limit: int,
    level: str | None = None,
    after: int | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Read a stream's records with a level filter + a BYTE-OFFSET cursor.

    Returns ``(records, cursor)``. Each returned record carries an added
    ``cursor`` = the byte offset just past its line (append-only follow);
    ``cursor`` (the second element) is the byte length of the file (the
    resume point), or ``None`` when the file is empty / unreadable.

    * ``level`` keeps only records at or above that level name.
    * ``after`` is a byte offset: the reader ``seek``s there and reads
      FORWARD, returning only the records appended since (bounded by the
      appended size). ``None`` is the INITIAL tail: a bounded reverse
      block-read from EOF (:data:`_TAIL_BLOCK`-sized blocks backward until
      ``limit`` complete lines OR :data:`_TAIL_BYTE_BUDGET` bytes), so the
      whole file is never read. Both paths return at most ``limit`` records.
    """
    threshold = _level_value(level)
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()

            if after is not None:
                # Forward tail: seek to the cursor, read what was appended.
                start = max(0, min(int(after), file_size))
                if start >= file_size:
                    return [], file_size
                fh.seek(start)
                data = fh.read(file_size - start)
                records = _records_from_bytes(
                    data, start, threshold=threshold, skip_leading_partial=False
                )
                if len(records) > limit:
                    records = records[-limit:]
                return records, file_size

            # Initial tail: bounded reverse block-read from EOF.
            if file_size == 0:
                return [], None
            blocks: list[bytes] = []
            pos = file_size
            scanned = 0
            newlines = 0
            while pos > 0 and scanned < _TAIL_BYTE_BUDGET:
                read_size = min(_TAIL_BLOCK, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                blocks.append(chunk)
                scanned += read_size
                newlines += chunk.count(b"\n")
                # ``> limit`` guarantees at least ``limit`` complete lines
                # remain after dropping the (partial) leading one below.
                if newlines > limit:
                    break
            data = b"".join(reversed(blocks))
            records = _records_from_bytes(
                data, pos, threshold=threshold, skip_leading_partial=pos > 0
            )
            if len(records) > limit:
                records = records[-limit:]
            return records, file_size
    except (FileNotFoundError, OSError):
        return [], None


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
          "cursor": <int|None>,      # byte offset of EOF (the resume point)
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
