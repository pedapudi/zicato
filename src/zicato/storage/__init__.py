"""zicato's storage abstraction — a clean, pluggable persistence seam.

zicato persists a handful of record shapes (live runtime state, epoch
journals, telemetry streams) and historically each domain did its own
file I/O. This package formalises that into one honest interface:
:class:`StorageBackend` — keyed read/write/list/delete of JSON records,
append to JSONL streams, atomic write semantics.

The seam is deliberately thin. Files stay the canonical store of record;
:class:`~zicato.storage.files.FileStorageBackend` IS the existing
file-based mechanism, byte-for-byte, and is the default. The abstraction
makes the mechanism *swappable for tests and future backends* — it does
not move the store-of-record anywhere.

Public surface
--------------
* :class:`StorageBackend` — the abstract interface every backend implements.
* :class:`FileStorageBackend` — the canonical, default file backend.
* :class:`InMemoryStorageBackend` — an in-process backend for tests.
* :func:`make_storage_backend` — name → backend factory.
* :func:`default_backend` — a started file backend for a workspace root.

The SQLite index (:mod:`zicato.index`) is the derived read side and is
intentionally NOT a :class:`StorageBackend` — it is rebuildable from the
canonical records and evolves independently of this seam.
"""

from __future__ import annotations

from zicato.storage.base import StorageBackend
from zicato.storage.factory import (
    DEFAULT_BACKEND,
    default_backend,
    make_storage_backend,
)
from zicato.storage.files import FileStorageBackend
from zicato.storage.memory import InMemoryStorageBackend

__all__ = [
    "StorageBackend",
    "FileStorageBackend",
    "InMemoryStorageBackend",
    "make_storage_backend",
    "default_backend",
    "DEFAULT_BACKEND",
]
