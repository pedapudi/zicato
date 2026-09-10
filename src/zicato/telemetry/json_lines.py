"""Read growing JSON logs with the same tolerance for incomplete records."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def read_json_lines[T](
    path: Path, decode: Callable[[dict[str, Any]], T | None]
) -> tuple[tuple[T, ...], int, bool]:
    """Return accepted records, malformed count, and whether the final line was valid."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return (), 0, True
    records = []
    malformed = 0
    last_line_ok = True
    for line in lines:
        if not line.strip():
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            body = None
        record = decode(body) if isinstance(body, dict) else None
        last_line_ok = record is not None
        if record is None:
            malformed += 1
        else:
            records.append(record)
    return tuple(records), malformed, last_line_ok
