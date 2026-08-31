"""Bridge between the ``epoch/`` domain and the record-level storage seam.

The ``epoch/`` modules that persist *records* — ``journal.py`` (the
typed :class:`~zicato.core.types.Experiment` and its per-patch files),
``lineage.py`` (the cross-epoch DAG), and the ``config.json`` /
``scoring.json`` writes in ``lifecycle.py`` — historically did direct,
partly non-atomic file I/O: ``path.write_text(json.dumps(...))`` with no
``.tmp`` + ``fsync`` + :func:`os.replace`. A crash mid-write could leave
a truncated ``experiment.json`` / ``lineage.json`` / ``config.json``.

As of the storage-roadmap pass those modules route every record
read/write through :class:`~zicato.storage.StorageBackend` instead —
this module is the thin adapter that makes that routing ergonomic
without changing any public ``epoch/`` signature. It is the exact
mirror of :mod:`zicato.runtime._storage`.

Two responsibilities:

* **Backend selection.** :func:`backend_for` constructs the canonical
  file backend for a workspace root. It is the single seam where
  ``epoch/`` decides which backend it uses.
* **Key computation.** ``epoch/`` records live under the ``epochs/``
  namespace (per-epoch and per-generation records) or directly under
  the workspace root (``lineage.json``, the ``current_epoch`` marker).
  The ``*_key`` helpers turn an ``(epoch, generation, …)`` coordinate
  into the logical storage key — the exact mirror of the path helpers
  in :mod:`zicato.core.workspace`, but yielding a backend *key* (a
  ``/``-relative string) rather than an absolute :class:`Path`.

The ``epoch/`` *generation source trees* are NOT a record kind and do
NOT go through this seam — they are directory trees behind the
:class:`~zicato.epoch.genstore.GenerationStore` protocol. See
``docs/design/STORAGE.md`` §4 for why the two seams are distinct.

Public ``epoch/`` functions keep their ``workspace_root: Path`` first
argument; internally they call :func:`backend_for` and one of the key
helpers. The on-disk layout is byte-identical to the pre-seam
implementation — a caller cannot tell the difference. The one
observable change is that every write is now atomic.
"""

from __future__ import annotations

from pathlib import Path

from zicato.storage import FileStorageBackend, StorageBackend

#: The logical namespace per-epoch records sit under, mirroring
#: :func:`zicato.core.workspace.epoch_dir`.
EPOCHS_NS = "epochs"


def backend_for(workspace_root: Path) -> StorageBackend:
    """Return the canonical storage backend for a workspace.

    ``epoch/`` records are the typed canonical evolutionary record —
    experiments, lineage, per-epoch config. Files are their canonical
    store: small, human-readable, diffable, and written at generation
    granularity by a single writer (the orchestrator) per epoch. This
    returns a :class:`~zicato.storage.FileStorageBackend` rooted at the
    workspace.

    The backend is intentionally *not* started here: ``epoch/`` writers
    create the directory tree they need (``new_epoch`` makes the epoch
    directory; the journal/genstore helpers create generation
    directories), and the file backend's :meth:`write_json` creates any
    missing parent on write anyway. A cheap unstarted backend keeps
    read-only callers side-effect-free.
    """
    return FileStorageBackend(workspace_root)


# --- key helpers (mirror zicato.core.workspace, but yield storage keys) ----


def _epoch_ns(epoch_id: str) -> str:
    """Storage-key prefix for one epoch's records."""
    return f"{EPOCHS_NS}/{epoch_id}"


def _generation_ns(epoch_id: str, generation_id: str) -> str:
    """Storage-key prefix for one generation's records."""
    return f"{_epoch_ns(epoch_id)}/generations/{generation_id}"


def epoch_config_key(epoch_id: str) -> str:
    """Storage key for one epoch's ``config.json``."""
    return f"{_epoch_ns(epoch_id)}/config.json"


def scoring_key(epoch_id: str) -> str:
    """Storage key for one epoch's frozen ``scoring.json``."""
    return f"{_epoch_ns(epoch_id)}/scoring.json"


def journal_key(epoch_id: str) -> str:
    """Storage key for one epoch's running ``journal.md``."""
    return f"{_epoch_ns(epoch_id)}/journal.md"


#: Version stamped into the CANONICAL JSON records (``experiment.json``,
#: each epoch's ``config.json``, ``lineage.json``) at write time.
#: A record with NO ``format_version`` key is treated as version 1 in this
#: release, so a workspace or fixture written before the stamp keeps
#: reading. The refusal therefore targets FUTURE incompatible shapes only.
#: A record stamped with a HIGHER version was written by a newer zicato
#: whose shape this build cannot promise to interpret, so the reader
#: refuses with a clear error instead of silently misreading it. There are
#: NO migration shims; bumping this constant is a deliberate format break.
RECORD_FORMAT_VERSION = 1


class RecordFormatError(RuntimeError):
    """A canonical JSON record's ``format_version`` is not readable here."""


def check_record_format(body: dict[str, object], record_name: str) -> None:
    """Refuse a canonical record whose ``format_version`` this build cannot read.

    ``body`` is the parsed JSON record; ``record_name`` names it in the
    error (e.g. ``"experiment.json"``). Absent ⇒ version 1 (pre-stamp
    records — accepted this release); equal ⇒ fine; anything else raises
    :class:`RecordFormatError` with the upgrade guidance.
    """
    raw = body.get("format_version")
    if raw is None:
        return  # pre-stamp record — version 1 by definition this release
    if isinstance(raw, int) and not isinstance(raw, bool) and raw == RECORD_FORMAT_VERSION:
        return
    raise RecordFormatError(
        f"{record_name}: format_version {raw!r} is not readable by this "
        f"zicato (expects {RECORD_FORMAT_VERSION}); the record was written "
        "by an incompatible (likely newer) version — upgrade zicato rather "
        "than letting an old reader misinterpret it"
    )


def lineage_key() -> str:
    """Storage key for the workspace-level ``lineage.json``."""
    return "lineage.json"


def current_epoch_key() -> str:
    """Storage key for the workspace ``current_epoch`` marker file."""
    return "current_epoch"


def experiment_key(epoch_id: str, generation_id: str) -> str:
    """Storage key for a generation's ``experiment.json``."""
    return f"{_generation_ns(epoch_id, generation_id)}/experiment.json"


def patches_prefix(epoch_id: str, generation_id: str) -> str:
    """Storage-key prefix the per-patch JSON records sit under."""
    return f"{_generation_ns(epoch_id, generation_id)}/patches"


def patch_key(epoch_id: str, generation_id: str, patch_id: str) -> str:
    """Storage key for one patch's JSON record."""
    return f"{patches_prefix(epoch_id, generation_id)}/{patch_id}.json"


__all__ = [
    "EPOCHS_NS",
    "RECORD_FORMAT_VERSION",
    "RecordFormatError",
    "backend_for",
    "check_record_format",
    "epoch_config_key",
    "scoring_key",
    "journal_key",
    "lineage_key",
    "current_epoch_key",
    "experiment_key",
    "patches_prefix",
    "patch_key",
]
