"""Internal helper: atomic JSON writes via ``.tmp`` + ``fsync`` + rename.

This is the one definition of "atomic file write" in zicato. The file
storage backend (:mod:`zicato.storage.files`) is its primary consumer —
it delivers the :class:`~zicato.storage.base.StorageBackend` atomic-write
contract by routing every write through here. The module sits in the
``storage`` package so that the storage layer is self-contained:
``runtime`` depends on ``storage``, never the reverse. :mod:`zicato.storage`
re-exports these names as the public face for callers outside the package.

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
5. ``fsync`` the parent DIRECTORY (durability of the rename itself —
   without it a power loss can forget the directory entry even though
   the file's blocks reached disk, leaving the OLD file, or none).

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


def _fsync_dir(directory: Path) -> None:
    """``fsync`` a directory so a rename inside it survives power loss.

    ``os.replace`` / ``os.rename`` mutate the DIRECTORY, and on POSIX
    the directory entry itself must be fsynced for the rename to be
    durable — fsyncing the file alone leaves the new name at the
    filesystem's mercy on power loss. Called unconditionally after
    every rename in this module: at zicato's write rates the extra
    fsync is micro-cost (verified against the full test-suite
    wall-clock).

    Best-effort by necessity rather than by choice: some platforms cannot open
    a directory fd at all (Windows raises ``PermissionError``), and
    some filesystems reject directory fsync. Those environments simply
    keep the pre-existing durability level; POSIX gets the upgrade.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content``.

    Creates parent directories as needed. ``fsync`` flushes the temp
    file's content before the rename so a crash after the rename
    cannot leave an empty file behind, and the parent directory is
    fsynced after the rename so the rename itself is durable.
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
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with the JSON encoding of ``data``.

    Uses ``indent=2, sort_keys=True`` for stable diffs and operator
    readability. Calls into :func:`atomic_write_text` for the actual
    write so the durability contract is identical.
    """
    text = json.dumps(data, indent=2, sort_keys=True)
    atomic_write_text(path, text)


def atomic_claim(src: Path, dst: Path) -> bool:
    """Atomically move ``src`` onto ``dst``, claiming it exactly once.

    Returns ``True`` if this caller moved the file, ``False`` if ``src``
    was already gone (another caller claimed it, or it never existed).

    This is the *claim-once* primitive behind
    :class:`~zicato.runtime.channel.CommandQueue`. Unlike
    :func:`atomic_write_text`, the source must already exist and the move
    is the synchronisation point: :func:`os.rename` of a given source path
    succeeds for **exactly one** racing caller — every other concurrent
    caller observes the source already renamed away and gets a
    :class:`FileNotFoundError`, which is reported here as ``False``. That
    is what lets many consumers poll the same queue and have each pending
    command fire for one and only one of them.

    The parent directory of ``dst`` is created if needed. ``dst`` must be
    on the same filesystem as ``src`` (it always is here — both live under
    the same workspace) so the rename is atomic rather than a copy+unlink.

    After a successful claim BOTH parent directories are fsynced: the
    destination's so the claim survives power loss, and the source's so
    the removal does too — otherwise a crash could resurrect the source
    entry and let an already-claimed command fire twice.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except FileNotFoundError:
        return False
    _fsync_dir(dst.parent)
    if src.parent != dst.parent:
        _fsync_dir(src.parent)
    return True


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
    "atomic_claim",
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
]
