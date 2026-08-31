"""The file storage backend — the canonical, default zicato persistence.

This backend IS the current file-based mechanism. It maps a logical *key*
onto a file under a workspace root and writes through the established
``.tmp`` + ``fsync`` + :func:`os.replace` atomic discipline. A workspace
laid out by this backend is byte-for-byte identical to the on-disk tree
zicato produced before the storage seam existed — the abstraction
formalises the file mechanism, it does not change the bytes.

Why files remain canonical (and this is the default): each keyed record
is its own file, so a crash or a corrupt write touches exactly one
record. A misbehaving tournament run cannot take down a sibling run's
state because they are different files. That failure-independence is a
deliberate property of zicato's design; the file backend preserves it.

The atomic-write logic lives in :mod:`zicato.storage._atomic` and is
reused verbatim here — there is one definition of "atomic JSON write" in
the codebase and this backend is one of its callers rather than a fork of it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from zicato.storage._atomic import atomic_write_json, atomic_write_text, read_json
from zicato.storage.base import StorageBackend


def _normalise_key(key: str) -> str:
    """Validate and normalise a logical key into a safe relative path.

    A key is a ``/``-separated relative path. This rejects the two ways a
    key could escape the backend's root — an absolute path or a ``..``
    component — so a buggy caller cannot write outside the workspace. The
    leading/trailing slashes are trimmed; empty keys are rejected.
    """
    cleaned = key.strip().strip("/")
    if not cleaned:
        raise ValueError("storage key must be a non-empty relative path")
    parts = cleaned.split("/")
    if any(p in ("", "..", ".") for p in parts):
        raise ValueError(f"storage key {key!r} must not contain empty, '.' or '..' components")
    return cleaned


class FileStorageBackend(StorageBackend):
    """File-backed :class:`StorageBackend` rooted at a workspace directory.

    Keys are resolved to ``root / key`` paths. ``root`` is whatever the
    caller passes — by zicato convention the ``.zicato/`` workspace
    directory — and the backend never prepends or invents layout above it.
    """

    def __init__(self, root: Path | str) -> None:
        """Create a backend rooted at ``root``.

        ``root`` is not created here — :meth:`start` does that — so a
        backend can be constructed cheaply for path introspection without
        a filesystem side effect.
        """
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory every key resolves under."""
        return self._root

    def _path(self, key: str) -> Path:
        """Resolve a logical key to its concrete file path under ``root``."""
        return self._root / _normalise_key(key)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Ensure the root directory exists. Idempotent."""
        self._root.mkdir(parents=True, exist_ok=True)

    # close() inherits the no-op default — the filesystem holds no handle.

    # -- JSON records -------------------------------------------------------

    def read_json(self, key: str) -> Any | None:
        """Read JSON from ``key``; return ``None`` if the file is absent.

        Delegates to :func:`zicato.storage._atomic.read_json`, which
        propagates :class:`json.JSONDecodeError` on a malformed file rather
        than masking it — a partial file means something bypassed the
        atomic-write seam and the caller should hear about it loudly.
        """
        return read_json(self._path(key))

    def write_json(self, key: str, data: Any) -> None:
        """Atomically write ``data`` as ``indent=2, sort_keys=True`` JSON."""
        atomic_write_json(self._path(key), data)

    # -- text records -------------------------------------------------------

    def read_text(self, key: str) -> str | None:
        """Read UTF-8 text from ``key``; return ``None`` if the file is absent."""
        path = self._path(key)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_text(self, key: str, content: str) -> None:
        """Atomically write UTF-8 ``content`` to ``key``."""
        atomic_write_text(self._path(key), content)

    # -- existence / deletion ----------------------------------------------

    def exists(self, key: str) -> bool:
        """Return ``True`` iff a file is stored at ``key``."""
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        """Delete the file at ``key``; return whether one was removed.

        Idempotent: a missing key returns ``False`` without raising. After
        removing the file, an emptied parent directory is *not* pruned —
        directory pruning is a domain decision (the control protocol does
        its own tidy-up) and the backend stays out of it.
        """
        path = self._path(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # -- listing ------------------------------------------------------------

    def list_keys(self, prefix: str) -> list[str]:
        """Return the keys of files directly under ``prefix``, sorted.

        Non-recursive: only the immediate children of the prefix directory
        are returned, and only regular files — subdirectories are skipped.
        ``.tmp`` files (the in-flight artefacts of an atomic write) are
        filtered out so a racing write never surfaces as a phantom key.
        An absent prefix directory yields an empty list.
        """
        norm = _normalise_key(prefix)
        directory = self._root / norm
        if not directory.is_dir():
            return []
        out: list[str] = []
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if entry.name.endswith(".tmp"):
                continue
            out.append(f"{norm}/{entry.name}")
        return out

    # -- JSONL streams ------------------------------------------------------

    def append_jsonl(self, key: str, record: Any) -> None:
        """Append one compact JSON line to the JSONL stream at ``key``.

        Creates the stream file (and any parent directories) on first
        append. The line is compact (no indentation, no embedded newlines)
        and ``\\n``-terminated — the newline-delimited-JSON convention the
        telemetry reducer reads back.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_jsonl(self, key: str) -> Iterator[Any]:
        """Yield the decoded records of the JSONL stream at ``key``.

        Yields nothing for an absent stream. Blank lines (e.g. a trailing
        newline) are skipped.
        """
        path = self._path(key)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                yield json.loads(stripped)


__all__ = ["FileStorageBackend"]
