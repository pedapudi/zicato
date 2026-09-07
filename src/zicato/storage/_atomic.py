"""Atomic record replacement through an exclusive temporary file and rename.

The file storage backend (:mod:`zicato.storage.files`) and workspace
configuration share this replacement helper. The module belongs to
``storage`` so that the storage layer is self-contained: ``runtime``
depends on ``storage``. :mod:`zicato.storage` re-exports these functions
for callers outside the package.

The goal is a hard guarantee: no reader ever observes a half-written
file. A crash mid-write leaves the on-disk file either untouched (at the
previous content) or fully replaced with the new content; never a
truncated mix.

The pattern is:

1. Ensure the parent directory exists.
2. Exclusively create a uniquely named sibling ending in ``.tmp`` and
   write the full payload, completing any short writes.
3. ``fsync`` the temporary file (durability of contents).
4. :func:`os.replace` it onto the final path (atomic on POSIX and
   Windows for files on the same filesystem).
5. ``fsync`` the parent DIRECTORY (durability of the rename itself —
   without it a power loss can forget the directory entry even though
   the file's blocks reached disk, leaving the OLD file, or none).

Failures before replacement leave the destination unchanged and remove
only this operation's temporary file. Competing writers each publish a
complete payload; callers still need ownership or a lock for compound
read-modify-write operations to avoid lost updates.

Readers can use :func:`read_json`, which returns ``None`` for a missing file.
"""

from __future__ import annotations

import json
import os
import stat
from errno import EIO
from pathlib import Path
from typing import Any
from uuid import uuid4


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


def atomic_write_text(path: Path, content: str, *, mode: int | None = 0o644) -> None:
    """Atomically replace ``path`` with ``content``.

    Creates parent directories as needed. An explicit ``mode`` is subject to
    the process umask. ``None`` preserves an existing regular file's permissions
    or creates a file with mode 0o666 subject to umask. The temporary file is synced
    before replacement; its parent directory is synced afterwards where
    supported. Readers observe complete payloads even when writers overlap.
    """
    remaining = memoryview(content.encode("utf-8"))
    _mkdir_durable(path.parent)
    retained_mode = None
    if mode is None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(metadata.st_mode):
                retained_mode = stat.S_IMODE(metadata.st_mode)
        mode = retained_mode if retained_mode is not None else 0o666
    while True:
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            break
        except FileExistsError:
            continue
    try:
        try:
            if retained_mode is not None:
                os.chmod(tmp, retained_mode)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError(EIO, "atomic write made no progress", str(path))
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with the JSON encoding of ``data``.

    Uses ``indent=2, sort_keys=True`` for stable diffs and operator
    readability. Calls into :func:`atomic_write_text` for the actual
    write so the durability contract is identical.
    """
    text = json.dumps(data, indent=2, sort_keys=True)
    atomic_write_text(path, text)


def sync_directory_tree(path: Path) -> None:
    """Synchronize prepared files and their directories before publication."""
    if not path.is_dir():
        raise FileNotFoundError(f"prepared directory does not exist: {path}")
    for directory, _children, files in os.walk(path, topdown=False):
        for name in files:
            fd = os.open(str(Path(directory) / name), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(Path(directory))
    _fsync_dir(path.parent)


def durable_unlink(path: Path) -> None:
    """Remove an owned file and synchronize its parent before further cleanup."""
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _mkdir_durable(path: Path) -> None:
    if path.is_dir():
        return
    _mkdir_durable(path.parent)
    path.mkdir(exist_ok=True)
    _fsync_dir(path.parent)


def publish_directory(prepared: Path, destination: Path) -> None:
    """Rename a synchronized directory into a vacant destination.

    The caller holds mutation ownership while checking vacancy and renaming.
    Both directories must be on the same filesystem. Synchronize the prepared
    tree with :func:`sync_directory_tree` before recording publication intent.
    """
    if destination.exists():
        raise FileExistsError(f"publication destination already exists: {destination}")
    _mkdir_durable(destination.parent)
    os.rename(prepared, destination)
    _fsync_dir(destination.parent)
    if prepared.parent != destination.parent:
        _fsync_dir(prepared.parent)


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
    "durable_unlink",
    "publish_directory",
    "read_json",
    "sync_directory_tree",
]
