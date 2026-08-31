"""The :class:`StorageBackend` abstraction — zicato's pluggable persistence seam.

zicato's persistence is, at heart, a small set of operations repeated across
several domains: read/write/delete/list JSON records, append lines to a JSONL
event stream, and do all of it with a hard atomic-write guarantee. Today each
domain (``runtime/``, ``epoch/``, ``telemetry/``) reaches for the same handful
of helpers (atomic-write + ``Path`` math) directly. This module
formalises that into one honest interface so the mechanism can be swapped
(an in-memory backend for tests; a remote record store if one is ever needed)
without any domain caring. Generation source trees are intentionally outside
this interface and use :class:`zicato.epoch.genstore.GenerationStore`.

Design stance — read these before adding a method here:

* **Files stay canonical.** The default backend
  (:class:`zicato.storage.files.FileStorageBackend`) IS the current
  file-based mechanism, byte-for-byte. This abstraction is a clean seam,
  not a migration path toward a single shared database. Each run's state
  lives in its own keyed record so a misbehaving run's blast radius stays
  exactly one record — that failure-independence is the whole point and
  must never be weakened.
* **Honest to zicato's model.** The interface is read/write/list/delete of
  JSON records, append to JSONL streams, and atomic write semantics, all
  keyed by a logical *key* (a ``/``-separated relative path). It is
  NOT an ORM and NOT a relational schema — the store of
  record is files holding JSON, and the interface says exactly that.
* **Synchronous.** Unlike harmonograf's server-side ``Store``, zicato's
  persistence is plain in-process function calls on the orchestrator's
  hot path; an async interface would buy nothing and force every caller
  (including the synchronous supervisor-facing readers) to grow an event
  loop. The backend is synchronous on purpose.
* **The SQLite index is NOT a backend.** :mod:`zicato.index` is the
  *derived read side* — rebuildable at any time from the canonical
  records. It does not implement this protocol and must not be routed
  through it; conflating the store-of-record with its derived index
  would couple two things that fail, scale, and evolve independently.

Keys
----
A *key* is a logical path: a ``/``-separated string, no leading slash, e.g.
``"runtime/heartbeat.json"`` or ``"runtime/active_runs/run_abc.json"``. The
backend maps a key onto its concrete storage (a file under the workspace for
the file backend; a dict entry for the in-memory backend). Callers compose
keys from their own path helpers — :class:`StorageBackend` never invents
layout, it only persists what it is handed.

The atomic-write contract
-------------------------
:meth:`StorageBackend.write_json` and :meth:`StorageBackend.write_text` MUST
be atomic with respect to readers: a reader calling :meth:`read_json` /
:meth:`read_text` concurrently with a write observes either the complete
prior value or the complete new value, never a truncated mix. A crash
mid-write leaves the key either untouched or fully replaced. The file
backend delivers this with the established ``.tmp`` + ``fsync`` +
``os.replace`` discipline; every backend must deliver it somehow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any


class StorageBackend(ABC):
    """A keyed, atomic JSON/JSONL record store — zicato's persistence seam.

    Every backend round-trips the same operations with the same observable
    semantics; the cross-backend contract is pinned by the conformance
    suite (``tests/test_storage_conformance.py``). A backend that passes
    every conformance test is a drop-in for any zicato domain routed
    through this interface.

    Lifecycle: construct, :meth:`start`, use, :meth:`close`. The file
    backend's ``start``/``close`` are no-ops (the filesystem needs no
    handle); a future remote record backend could use them to open and flush.
    Backends are also usable as context managers via :meth:`__enter__` /
    :meth:`__exit__`.
    """

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Prepare the backend for use.

        Idempotent. The file backend ensures its root directory exists;
        a future networked backend would open its connection here. The
        default implementation is a no-op so trivial backends need not
        override.
        """
        return None

    def close(self) -> None:
        """Release any resources the backend holds.

        Idempotent. The default implementation is a no-op. After
        :meth:`close` the backend should not be used again.
        """
        return None

    def __enter__(self) -> StorageBackend:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- JSON records -------------------------------------------------------

    @abstractmethod
    def read_json(self, key: str) -> Any | None:
        """Return the JSON value stored at ``key``, or ``None`` if absent.

        A missing key returns ``None`` — every zicato state-record consumer
        treats "not yet written" as a valid state rather than an error.
        A *malformed* record (corrupt JSON) is a real bug and MUST raise
        :class:`json.JSONDecodeError` rather than being swallowed; the
        atomic-write contract guarantees on-disk records are never partial,
        so a parse failure means something bypassed the seam.
        """

    @abstractmethod
    def write_json(self, key: str, data: Any) -> None:
        """Atomically write ``data`` (JSON-encoded) to ``key``.

        Creates any intermediate namespace as needed. The encoding is
        ``indent=2, sort_keys=True`` for stable diffs and operator
        readability. Atomic with respect to concurrent readers — see the
        module docstring's atomic-write contract.
        """

    # -- text records (non-JSON payloads) -----------------------------------

    @abstractmethod
    def read_text(self, key: str) -> str | None:
        """Return the UTF-8 text stored at ``key``, or ``None`` if absent.

        Used for the control-protocol's flag files (empty payload) and the
        rubric-replacement payload file, which are not JSON.
        """

    @abstractmethod
    def write_text(self, key: str, content: str) -> None:
        """Atomically write UTF-8 ``content`` to ``key``.

        Same atomicity guarantee as :meth:`write_json`. An empty string is
        a valid payload (the control protocol's flag files carry no body).
        """

    # -- existence / deletion ----------------------------------------------

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return ``True`` iff a record is stored at ``key``.

        Cheap predicate — does not read or parse the record. Used by the
        control protocol's :func:`is_paused`-style flag checks.
        """

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete the record at ``key``.

        Returns ``True`` if a record was removed, ``False`` if the key was
        already absent. Idempotent — deleting a missing key is not an error.
        """

    # -- listing ------------------------------------------------------------

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """Return the keys of records directly under ``prefix``, sorted.

        ``prefix`` names a logical directory (e.g. ``"runtime/active_runs"``).
        The result is the keys of records *immediately* under it — not a
        recursive walk — each returned as a full key (prefix included),
        sorted lexicographically for a stable, deterministic order.
        Returns an empty list when the prefix holds no records.

        Half-written temporary artefacts (the ``.tmp`` files the file
        backend uses during an atomic write) are never returned.
        """

    @abstractmethod
    def list_namespaces(self, prefix: str) -> list[str]:
        """Return the sub-namespaces directly under ``prefix``, sorted.

        The companion of :meth:`list_keys` for records that are themselves
        namespaces rather than single values. A generation record is a
        namespace: ``epochs/{epoch}/generations/{id}`` holds an
        ``experiment.json``, a ``runs/`` subtree and more, so
        :meth:`list_keys` on ``epochs/{epoch}/generations`` reports nothing
        and the question "which generations exist" needs this method.

        Returns the immediately nested namespaces — not a recursive walk —
        each as a full key with ``prefix`` included, sorted
        lexicographically. Returns an empty list when the prefix names
        nothing.
        """

    # -- JSONL streams ------------------------------------------------------

    @abstractmethod
    def append_jsonl(self, key: str, record: Any) -> None:
        """Append one ``record`` as a JSON line to the JSONL stream at ``key``.

        Creates the stream if it does not exist. The record is encoded as a
        single compact JSON line (no embedded newlines) terminated by
        ``\\n``. Unlike :meth:`write_json`, this is an *append* — existing
        lines are preserved. JSONL streams are zicato's append-only event
        log shape (telemetry); a backend need only guarantee that a
        complete line is appended rather than cross-process append atomicity.
        """

    @abstractmethod
    def read_jsonl(self, key: str) -> Iterator[Any]:
        """Yield the records of the JSONL stream at ``key`` in append order.

        Yields nothing when the stream is absent. Each yielded value is one
        decoded JSON record. Blank lines are skipped (a trailing newline is
        common and benign).
        """


__all__ = ["StorageBackend"]
