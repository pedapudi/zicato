"""Epoch lifecycle, journaling, analysis, and lineage.

The epoch is the unit of evaluation contract: a frozen board, a frozen
proposer brief, and a frozen scoring configuration. Generations within
an epoch are directly comparable; cross-epoch comparison is fuzzy.
Pattern aggregates reset at epoch boundaries.

This subpackage owns five concerns:

* :mod:`zicato.epoch.lifecycle` — create / close / list / switch.
* :mod:`zicato.epoch.journal` — append-only narrative per experiment.
* :mod:`zicato.epoch.analysis` — at-close LLM retrospective.
* :mod:`zicato.epoch.lineage` — cross-cutting DAG persisted to
  ``.zicato/lineage.json``.
* :mod:`zicato.epoch.genstore` — the :class:`GenerationStore` seam:
  generation source trees behind the shipped directory and Git backends.
  See ``docs/design/STORAGE.md`` §4-§5.

The first four persist *records* and route through
:class:`zicato.storage.StorageBackend`; the fifth persists source
trees and is a separate, peer seam — the two are kept distinct on
purpose (``docs/design/STORAGE.md`` §4).

Downstream callers should import from ``zicato.epoch`` rather than the
individual submodules so the surface stays stable.
"""

from __future__ import annotations

from zicato.epoch.analysis import REQUIRED_SECTIONS, generate_analysis
from zicato.epoch.genstore import (
    DirectoryGenerationStore,
    GenerationStore,
    default_generation_store,
)
from zicato.epoch.journal import (
    ExperimentRecordError,
    append_journal_entry,
    experiment_body,
    read_experiment,
    read_experiment_if_present,
    read_journal,
    update_experiment_outcome,
    write_experiment,
)
from zicato.epoch.lifecycle import (
    close_epoch,
    close_epoch_async,
    current_epoch_id,
    list_epochs,
    load_epoch,
    new_epoch,
    set_epoch_goal,
    set_epoch_noise_floor,
    set_epoch_preflight,
    switch_epoch,
)
from zicato.epoch.lineage import (
    append_to_lineage,
    load_lineage,
    mark_closed,
    register_epoch,
    render_lineage_summary,
)

__all__ = [
    # lifecycle
    "new_epoch",
    "close_epoch",
    "close_epoch_async",
    "list_epochs",
    "switch_epoch",
    "current_epoch_id",
    "load_epoch",
    "set_epoch_goal",
    "set_epoch_noise_floor",
    "set_epoch_preflight",
    # journal
    "append_journal_entry",
    "read_journal",
    "write_experiment",
    "ExperimentRecordError",
    "experiment_body",
    "read_experiment",
    "read_experiment_if_present",
    "update_experiment_outcome",
    # analysis
    "REQUIRED_SECTIONS",
    "generate_analysis",
    # generation store
    "GenerationStore",
    "DirectoryGenerationStore",
    "default_generation_store",
    # lineage
    "register_epoch",
    "mark_closed",
    "append_to_lineage",
    "load_lineage",
    "render_lineage_summary",
]
