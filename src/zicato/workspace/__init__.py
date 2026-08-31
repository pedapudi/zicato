"""Typed canonical-read layer for the ``.zicato/`` workspace.

The single seam that owns reading the on-disk workspace into typed objects.
Its first responsibility — the one this package was introduced to settle —
is **epoch / generation enumeration and ordering**: there is exactly one
definition of the canonical epoch ordering (:func:`epoch_sort_key`,
timestamp-first) and exactly one place that enumerates the ``epochs/``
directory (:func:`iter_epochs` / :func:`list_epoch_ids`). Every dashboard
reader routes through here rather than calling ``paths.epochs.iterdir()``
with a sort of its own, so a single ordering authority governs every
epoch-list-bearing response.

The package also owns the per-epoch / per-generation **path math**
(:class:`WorkspaceLayout`) and the small set of **typed canonical reads**
(:func:`read_epoch_config`, :func:`read_board`, :func:`read_experiments`,
:func:`read_loss`, :func:`read_gen_score`, and the measurement-history
readers :func:`read_gen_score_history` / :func:`read_events_history`)
the dashboard consumes, so the
leaf filename joins stop being re-implemented at dozens of call sites.

Design constraints (the refactor is behavior-preserving):

* Every read is **best-effort**: a missing / unreadable / malformed file
  degrades to the same empty / ``None`` value the prior inline reader
  returned, never a new exception.
* The path math is **byte-identical** to the dashboard's prior inline
  joins (no outer→inner ``.zicato`` normalization here — the dashboard
  always hands us the inner workspace root, matching the prior behavior).
* This is the dashboard-facing subset of the Phase 1c canonical-read layer
  in ``docs/design/REIMPLEMENTATION.md``; the index / telemetry / analyzer
  / orchestrator migrations are phased and intentionally deferred.
"""

from __future__ import annotations

from zicato.workspace.epochs import (
    Epoch,
    epoch_created_at,
    epoch_sort_key,
    iter_epochs,
    list_epoch_ids,
    natural_key,
    read_epoch_config,
)
from zicato.workspace.layout import WorkspaceLayout, events_replicate_index, is_events_file
from zicato.workspace.reads import (
    read_board,
    read_events_history,
    read_experiment,
    read_experiments,
    read_gen_score,
    read_gen_score_history,
    read_loss,
)

__all__ = [
    "Epoch",
    "WorkspaceLayout",
    "epoch_created_at",
    "epoch_sort_key",
    "events_replicate_index",
    "is_events_file",
    "iter_epochs",
    "list_epoch_ids",
    "natural_key",
    "read_board",
    "read_epoch_config",
    "read_events_history",
    "read_experiment",
    "read_experiments",
    "read_gen_score",
    "read_gen_score_history",
    "read_loss",
]
