"""Backward-compatible re-export of the atomic-write helpers.

The atomic JSON/text write helpers moved to :mod:`zicato.storage._atomic`
when the storage abstraction landed — the storage package owns the one
definition of "atomic file write" and ``runtime`` is now a consumer of
the storage seam rather than the home of its mechanism.

This shim re-exports the same three names so any caller still importing
``zicato.runtime._atomic`` (e.g. read-only state consumers, tests) keeps
working unchanged. New code should import from :mod:`zicato.storage`.
"""

from __future__ import annotations

from zicato.storage._atomic import (
    atomic_write_json,
    atomic_write_text,
    read_json,
)

__all__ = [
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
]
