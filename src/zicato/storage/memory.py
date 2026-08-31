"""The in-memory storage backend — for tests and ephemeral callers.

:class:`InMemoryStorageBackend` keeps every record in process memory. It
exists so the storage seam can be exercised without touching a filesystem
— unit tests of any domain routed through :class:`StorageBackend` can run
against it for speed and isolation, and the conformance suite runs against
it side-by-side with the file backend to prove the two are interchangeable.

It is NOT a persistence backend: nothing survives the process. It mirrors
the file backend's *observable* semantics — atomic replacement, missing-key
returns ``None``, idempotent delete, sorted non-recursive listing,
append-only JSONL — but holds it all in dicts.

Records are stored as deep copies on the way in and on the way out, so a
caller mutating a value it wrote (or a value it read) cannot reach back
into the backend's state. That matches the file backend, where every
read re-parses fresh JSON and every write serialises a snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from zicato.storage.base import StorageBackend
from zicato.storage.files import _normalise_key

#: Sentinel distinguishing "key absent" from a stored value (including a
#: stored ``None``) in the in-memory backend's dict lookups.
_MISSING = object()


class InMemoryStorageBackend(StorageBackend):
    """In-memory :class:`StorageBackend` — interchangeable with the file backend.

    Two flat namespaces are kept side by side: ``_records`` for the
    single-value JSON/text keys (:meth:`write_json` / :meth:`write_text`)
    and ``_streams`` for the append-only JSONL keys (:meth:`append_jsonl`).
    They are distinct stores because a JSONL stream and a JSON record are
    different shapes — the file backend distinguishes them by access
    pattern on the same path, and this backend makes that explicit. A
    given key should be used as one or the other rather than both.
    """

    def __init__(self) -> None:
        # key -> the stored value. JSON records hold the decoded value;
        # text records hold the str. JSON values are round-tripped
        # through json.dumps/json.loads on the way in and out so callers
        # cannot mutate backend state through a returned value.
        self._records: dict[str, Any] = {}
        # key -> list of decoded JSONL records, in append order.
        self._streams: dict[str, list[Any]] = {}

    # start()/close() inherit the no-op defaults — there is nothing to
    # open or release for an in-process dict.

    # -- JSON records -------------------------------------------------------

    def read_json(self, key: str) -> Any | None:
        """Return a deep copy of the JSON value at ``key``, or ``None``.

        The value is round-tripped through :func:`json.loads` /
        :func:`json.dumps` so a returned record is exactly what a file
        backend would yield — JSON-clean, with no live references into the
        backend and no non-JSON types leaking through.
        """
        norm = _normalise_key(key)
        if norm not in self._records:
            return None
        return json.loads(json.dumps(self._records[norm]))

    def write_json(self, key: str, data: Any) -> None:
        """Store a JSON snapshot of ``data`` at ``key``, replacing any prior value.

        The value is serialised and re-parsed so what lands in the backend
        is JSON-clean (matching the file backend, which can only ever hold
        what ``json.dumps`` produced) and decoupled from the caller's
        object.
        """
        norm = _normalise_key(key)
        self._records[norm] = json.loads(json.dumps(data))

    # -- text records -------------------------------------------------------

    def read_text(self, key: str) -> str | None:
        """Return the UTF-8 text stored at ``key``, or ``None`` if absent.

        Text records are always stored as ``str`` by :meth:`write_text`;
        a missing key returns ``None``.
        """
        norm = _normalise_key(key)
        value = self._records.get(norm, _MISSING)
        return None if value is _MISSING else value

    def write_text(self, key: str, content: str) -> None:
        """Store ``content`` at ``key``, replacing any prior value."""
        self._records[_normalise_key(key)] = content

    # -- existence / deletion ----------------------------------------------

    def exists(self, key: str) -> bool:
        """Return ``True`` iff a record or stream is stored at ``key``."""
        norm = _normalise_key(key)
        return norm in self._records or norm in self._streams

    def delete(self, key: str) -> bool:
        """Delete the record or stream at ``key``; return whether one existed.

        Idempotent — a missing key returns ``False``. Removes from both the
        record and stream namespaces so a key used as either is cleared.
        """
        norm = _normalise_key(key)
        removed = self._records.pop(norm, _MISSING) is not _MISSING
        removed_stream = self._streams.pop(norm, _MISSING) is not _MISSING
        return removed or removed_stream

    # -- listing ------------------------------------------------------------

    def list_keys(self, prefix: str) -> list[str]:
        """Return keys of records/streams directly under ``prefix``, sorted.

        Mirrors the file backend's non-recursive listing: a key
        ``a/b/c.json`` is a direct child of prefix ``a/b`` but not of
        ``a``. Both record and stream keys are considered. The result is
        sorted lexicographically for a deterministic order.
        """
        norm = _normalise_key(prefix)
        wanted = norm + "/"
        out: set[str] = set()
        for key in (*self._records.keys(), *self._streams.keys()):
            if not key.startswith(wanted):
                continue
            remainder = key[len(wanted) :]
            if "/" in remainder:
                continue  # deeper than a direct child — not listed
            out.add(key)
        return sorted(out)

    # -- JSONL streams ------------------------------------------------------

    def append_jsonl(self, key: str, record: Any) -> None:
        """Append a JSON snapshot of ``record`` to the stream at ``key``.

        Creates the stream on first append. The record is round-tripped
        through JSON so the stream only ever holds JSON-clean values, as a
        file-backed JSONL stream would.
        """
        norm = _normalise_key(key)
        self._streams.setdefault(norm, []).append(json.loads(json.dumps(record)))

    def read_jsonl(self, key: str) -> Iterator[Any]:
        """Yield deep copies of the stream's records at ``key`` in append order."""
        norm = _normalise_key(key)
        for record in self._streams.get(norm, []):
            yield json.loads(json.dumps(record))


__all__ = ["InMemoryStorageBackend"]
