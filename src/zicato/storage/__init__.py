"""zicato's storage abstraction — a clean, pluggable persistence seam.

zicato persists a handful of record shapes (live runtime state, epoch
journals, telemetry streams). Rather than each domain doing its own file
I/O, they share one interface:
:class:`StorageBackend` — keyed read/write/list/delete of JSON records,
append to JSONL streams, atomic write semantics.

The seam is thin. Files stay the canonical store of record;
:class:`~zicato.storage.files.FileStorageBackend` IS the existing
file-based mechanism, byte-for-byte, and is the default. The abstraction
makes the mechanism *swappable for tests and future backends* — it does
not move the store-of-record anywhere.

Public surface
--------------
* :class:`StorageBackend` — the abstract interface every backend implements.
* :class:`FileStorageBackend` — the canonical, default file backend.
* :class:`InMemoryStorageBackend` — an in-process backend for tests.
* :func:`workspace_backend` — the one construction path: the file backend for
  a workspace root, started or unstarted as the call site asks.
* :func:`make_storage_backend` — name → backend, for tests and future backends.
* :func:`atomic_claim` / :func:`atomic_write_json` / :func:`atomic_write_text`
  / :func:`read_json`
  — the atomic file primitives every writer/reader in the tree shares
  (temp-and-rename writes; reads that tolerate a missing file).

The SQLite index (:mod:`zicato.index`) is the derived read side and is
intentionally NOT a :class:`StorageBackend` — it is rebuildable from the
canonical records and evolves independently of this seam.
"""

from __future__ import annotations

from zicato.storage._atomic import (
    atomic_claim,
    atomic_write_json,
    atomic_write_text,
    read_json,
)
from zicato.storage.base import StorageBackend
from zicato.storage.factory import (
    DEFAULT_BACKEND,
    make_storage_backend,
    workspace_backend,
)
from zicato.storage.files import FileStorageBackend
from zicato.storage.memory import InMemoryStorageBackend

__all__ = [
    "StorageBackend",
    "FileStorageBackend",
    "InMemoryStorageBackend",
    "make_storage_backend",
    "workspace_backend",
    "DEFAULT_BACKEND",
    "atomic_claim",
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
]
