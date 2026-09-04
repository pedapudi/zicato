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
without changing any public ``epoch/`` signature.

What it owns is naming: the ``*_key`` helpers turn an
``(epoch, generation, …)`` coordinate into the logical storage key for one
record. ``epoch/`` records live under the ``epochs/`` namespace (per-epoch
and per-generation records) or directly under the workspace root
(``lineage.json``, the ``current_epoch`` marker).

The helpers do not re-spell those joins. Each reads its location off
:data:`~zicato.workspace.layout.WORKSPACE_RELATIVE_LAYOUT`, the workspace
layout resolved against an empty root, and
:func:`~zicato.workspace.layout.storage_key` renders the result as a
``/``-relative key. :class:`~zicato.workspace.layout.WorkspaceLayout` is
therefore the only place each record's location is declared, whether a
caller wants that location as a :class:`Path` or as a backend key.

The backend comes from :func:`zicato.storage.workspace_backend`, the one
construction path in the tree, and ``epoch/`` asks it for an unstarted
one: ``epoch/`` writers create the directory tree they need (``new_epoch``
makes the epoch directory, the journal and genstore helpers the generation
directories), the file backend's :meth:`write_json` creates any missing
parent on write, and an unstarted backend leaves readers side-effect-free.

The ``epoch/`` *generation source trees* are NOT a record kind and do
NOT go through this seam — they are directory trees behind the
:class:`~zicato.epoch.genstore.GenerationStore` protocol. See
``docs/design/STORAGE.md`` §4 for why the two seams are distinct.

Public ``epoch/`` functions keep their ``workspace_root: Path`` first
argument; internally they construct a backend and pass it one of the key
helpers. The on-disk layout is byte-identical to the pre-seam
implementation — a caller cannot tell the difference. The one
observable change is that every write is now atomic.
"""

from __future__ import annotations

from zicato.workspace.layout import WORKSPACE_RELATIVE_LAYOUT as _LAYOUT
from zicato.workspace.layout import storage_key


def epochs_prefix() -> str:
    """Storage-key prefix every epoch's records sit under."""
    return storage_key(_LAYOUT.epochs_dir)


def epoch_prefix(epoch_id: str) -> str:
    """Storage-key prefix for one epoch's records."""
    return storage_key(_LAYOUT.epoch_dir(epoch_id))


def epoch_config_key(epoch_id: str) -> str:
    """Storage key for one epoch's ``config.json``."""
    return storage_key(_LAYOUT.epoch_config(epoch_id))


def scoring_key(epoch_id: str) -> str:
    """Storage key for one epoch's frozen ``scoring.json``."""
    return storage_key(_LAYOUT.scoring(epoch_id))


def journal_key(epoch_id: str) -> str:
    """Storage key for one epoch's running ``journal.md``."""
    return storage_key(_LAYOUT.journal(epoch_id))


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


class RecordError(RuntimeError):
    """A canonical JSON record is present and cannot be understood.

    The base every record decoder's refusal shares, so a view degrading at
    its own boundary catches one type and renders ``str(exc)`` as the
    reason. Absence is NOT this: a record that was never written raises
    :class:`FileNotFoundError` or is reported as ``None`` by a reader whose
    return type says so.
    """


class RecordFormatError(RecordError):
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
    return storage_key(_LAYOUT.lineage_path)


def current_epoch_key() -> str:
    """Storage key for the workspace ``current_epoch`` marker file."""
    return storage_key(_LAYOUT.current_epoch_marker)


def rounds_prefix(epoch_id: str) -> str:
    """Storage-key prefix one epoch's per-round records sit under."""
    return storage_key(_LAYOUT.rounds_dir(epoch_id))


def round_prefix(epoch_id: str, round_index: int) -> str:
    """Storage-key prefix for one evolve round's records."""
    return storage_key(_LAYOUT.round_dir(epoch_id, round_index))


def experiment_key(epoch_id: str, generation_id: str) -> str:
    """Storage key for a generation's ``experiment.json``."""
    return storage_key(_LAYOUT.experiment(epoch_id, generation_id))


def patches_prefix(epoch_id: str, generation_id: str) -> str:
    """Storage-key prefix the per-patch JSON records sit under."""
    return storage_key(_LAYOUT.patches_dir(epoch_id, generation_id))


def patch_key(epoch_id: str, generation_id: str, patch_id: str) -> str:
    """Storage key for one patch's JSON record."""
    return storage_key(_LAYOUT.patch_json(epoch_id, generation_id, patch_id))


__all__ = [
    "RECORD_FORMAT_VERSION",
    "RecordFormatError",
    "check_record_format",
    "epochs_prefix",
    "epoch_prefix",
    "epoch_config_key",
    "scoring_key",
    "journal_key",
    "lineage_key",
    "current_epoch_key",
    "rounds_prefix",
    "round_prefix",
    "experiment_key",
    "patches_prefix",
    "patch_key",
]
