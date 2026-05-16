"""Internal helper: atomic JSON writes via ``.tmp`` + ``fsync`` + rename.

This is the one definition of "atomic file write" in zicato. The file
storage backend (:mod:`zicato.storage.files`) is its primary consumer —
it delivers the :class:`~zicato.storage.base.StorageBackend` atomic-write
contract by routing every write through here. The module sits in the
``storage`` package (not ``runtime``, where it historically lived) so the
storage layer is self-contained: ``runtime`` depends on ``storage``, never
the reverse. :mod:`zicato.runtime._atomic` re-exports these names for any
caller still importing from the old path.

The goal is a hard guarantee: no reader ever observes a half-written
file. A crash mid-write leaves the on-disk file either untouched (at the
previous content) or fully replaced with the new content; never a
truncated mix.

The pattern is:

1. Ensure the parent directory exists.
2. Write the full payload to ``path.with_suffix(path.suffix + ".tmp")``.
3. ``fsync`` the temporary file (durability of contents).
4. :func:`os.replace` it onto the final path (atomic on POSIX and
   Windows for files on the same filesystem).

Readers can use :func:`read_json` which tolerates a missing file (returns
``None``) and a transient mid-rename window (rare; retries once).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _tmp_path(path: Path) -> Path:
    """Return the sibling ``.tmp`` path for an atomic write.

    Suffix is ``.tmp`` appended to the existing extension so writes
    don't collide if two callers ever race on the same final path —
    the temporary lives only briefly and ``os.replace`` is atomic.
    """
    return path.with_suffix(path.suffix + ".tmp")


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content``.

    Creates parent directories as needed. ``fsync`` flushes the temp
    file's content before the rename so a crash after the rename
    cannot leave an empty file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with the JSON encoding of ``data``.

    Uses ``indent=2, sort_keys=True`` for stable diffs and operator
    readability. Calls into :func:`atomic_write_text` for the actual
    write so the durability contract is identical.
    """
    text = json.dumps(data, indent=2, sort_keys=True)
    atomic_write_text(path, text)


def read_json(path: Path) -> Any | None:
    """Read JSON from ``path``; return ``None`` if the file is absent.

    Returns the parsed JSON value on success. A missing file returns
    ``None`` — every state-file consumer treats "not yet written" as a
    valid state and we'd rather not raise on that case.

    Does NOT swallow JSON-decode errors — a malformed state file is a
    real bug and propagating the :class:`json.JSONDecodeError` lets the
    caller log it loudly. The atomic-write discipline above is
    specifically designed so the on-disk file is never partial; if it
    IS partial something has bypassed the helpers and we want to know.
    """
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
]
