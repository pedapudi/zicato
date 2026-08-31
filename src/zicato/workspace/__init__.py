"""Typed canonical-read layer for the ``.zicato/`` workspace.

The single seam that owns reading the on-disk workspace into typed objects.
Its first responsibility is **record enumeration and ordering**. Four
questions — which epochs, which generations, which board-entry runs, which
rounds — are answered in exactly one place each
(:func:`iter_epochs` / :func:`list_epoch_ids`, :func:`generation_ids`,
:func:`run_entry_ids`, :func:`round_indices`), and each answer carries one
definition of order: epochs by recorded creation time
(:func:`epoch_sort_key`), generations and board-entry runs by the
numeric-aware :func:`natural_key`, rounds by ascending index. Every reader
in the tree calls these rather than walking ``epochs/``,
``generations/``, ``runs/`` or ``rounds/`` with a sort of its own, so one
ordering authority governs every response, report and repair pass. The
enumerations read through
:meth:`~zicato.storage.StorageBackend.list_namespaces`, which is how a
record shaped as a directory of files is enumerated over the storage seam.

The package also owns the workspace root's ``config.json`` — where it is,
how it parses, and the typed shape of what is in it
(:func:`zicato.workspace.config_io.read_workspace_config`), which no other
module opens. It owns the per-epoch / per-generation **path math**
(:class:`WorkspaceLayout`) and the **typed canonical reads** the
enumerations feed (:func:`read_epoch_config`, :func:`read_board`,
:func:`read_experiment`, :func:`read_experiments`, :func:`read_loss`,
:func:`read_gen_score`, and the measurement-history readers
:func:`read_gen_score_history` / :func:`read_events_history`), so the leaf
filename joins stop being re-implemented at dozens of call sites.

Design constraints:

* Every read is **best-effort**: a missing / unreadable / malformed file
  degrades to an empty / ``None`` value, never a new exception. A record
  directory with no readable leaf file is still enumerated — the directory
  is what makes the record exist.
* The path math does no outer→inner ``.zicato`` normalization: callers hand
  this layer the inner workspace root.
* The typed canonical-read layer described in
  ``docs/design/REIMPLEMENTATION.md``. The telemetry reducer and the
  SQLite index own their own record formats and parse them directly, while
  asking this layer which records there are to parse.
"""

from __future__ import annotations

from zicato.workspace.epochs import (
    Epoch,
    epoch_created_at,
    epoch_sort_key,
    generation_round_number,
    iter_epochs,
    list_epoch_ids,
    natural_key,
    next_generation_id,
    read_epoch_config,
)
from zicato.workspace.layout import WorkspaceLayout, events_replicate_index, is_events_file
from zicato.workspace.reads import (
    generation_ids,
    read_board,
    read_events_history,
    read_experiment,
    read_experiments,
    read_gen_score,
    read_gen_score_history,
    read_loss,
    round_indices,
    run_entry_ids,
)

__all__ = [
    "Epoch",
    "WorkspaceLayout",
    "epoch_created_at",
    "epoch_sort_key",
    "events_replicate_index",
    "generation_ids",
    "generation_round_number",
    "is_events_file",
    "iter_epochs",
    "list_epoch_ids",
    "natural_key",
    "next_generation_id",
    "read_board",
    "read_epoch_config",
    "read_events_history",
    "read_experiment",
    "read_experiments",
    "read_gen_score",
    "read_gen_score_history",
    "read_loss",
    "round_indices",
    "run_entry_ids",
]
