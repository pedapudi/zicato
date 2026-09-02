"""Typed canonical reads of the per-epoch / per-generation records.

Two jobs live here. The first is enumeration: :func:`generation_ids`,
:func:`run_entry_ids` and :func:`round_indices` answer "which generation /
run / round records does this epoch hold", and they are the ONLY place in
the tree that asks. The second is the leaf reads the enumerations feed —
board, experiment, generation score, telemetry, loss — each routed through
:class:`~zicato.workspace.layout.WorkspaceLayout` so the filename joins live
in one place. Each reader returns the *raw* canonical structure (the parsed
JSON dict / list, or the parsed JSONL line dicts for the board) and leaves
view-specific shaping to the caller.

Enumeration goes over the storage seam
(:meth:`~zicato.storage.StorageBackend.list_namespaces`) rather than a bare
``Path.iterdir()``. Each of these records is a directory of files rather
than a single file, so :meth:`~zicato.storage.StorageBackend.list_keys` on
``generations/`` reports nothing at all and cannot answer. Routing through
the seam is what makes "the storage backend answers which records exist"
true of records as well as patches.

Order is the reason the enumerations are worth centralising. A generation
directory named ``v10`` sorts between ``v1`` and ``v2`` lexically and after
``v9`` numerically, and readers that disagreed about this presented the same
epoch's lineage in two different orders. Generations and board-entry run
directories come back in :func:`~zicato.workspace.epochs.natural_key` order
(numeric-aware, so ``v2`` precedes ``v10`` and entry ``t2`` precedes ``t10``);
round directories come back as ascending integers.

Every reader is **best-effort**: a missing directory, an unreadable one, a
malformed leaf file, or an id that cannot name a single record directory
yields the empty / ``None`` value rather than an exception. A record whose
directory exists but whose ``experiment.json`` was never written (an interrupted
round) is still enumerated — the directory IS the record's existence — and
simply drops out of the readers that need the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.storage import workspace_backend
from zicato.workspace.epochs import _read_json_value, natural_key
from zicato.workspace.layout import WORKSPACE_RELATIVE_LAYOUT, WorkspaceLayout, storage_key


def _namespace_names(layout: WorkspaceLayout, namespace: Path, *ids: str) -> list[str]:
    """The names of the record namespaces directly under one layout directory.

    ``namespace`` is a directory resolved off
    :data:`~zicato.workspace.layout.WORKSPACE_RELATIVE_LAYOUT`, so it already
    reads as the storage key to enumerate, and ``ids`` are the ids that were
    substituted into it. Returns bare names in the backend's lexical order;
    each enumeration below imposes the canonical order on top. An id that
    cannot name exactly one directory — empty, or carrying a path separator
    or ``..`` — names no records and yields the empty list rather than
    reaching outside the subtree it was meant to address.
    """
    if any(not i or "/" in i or "\\" in i or i in (".", "..") for i in ids):
        return []
    keys = workspace_backend(layout.root, start=False).list_namespaces(storage_key(namespace))
    return [key.rsplit("/", 1)[-1] for key in keys]


def generation_ids(layout: WorkspaceLayout, epoch_id: str) -> list[str]:
    """Every generation id one epoch holds a record for, in round-number order.

    A generation's record directory is written by the journal under both
    generation-source backends and survives source pruning
    (:mod:`zicato.epoch.gc`), so this is the durable answer to "which
    generations did this epoch mint" and the way to tell a pruned generation
    (recorded, no source tree) from one that never existed.
    :meth:`~zicato.epoch.genstore.GenerationStore.list_generations` answers
    the different question of which generations still have a source tree.

    Order is numeric-aware, so ``v2`` precedes ``v10``.
    """
    return sorted(
        _namespace_names(layout, WORKSPACE_RELATIVE_LAYOUT.generations_dir(epoch_id), epoch_id),
        key=natural_key,
    )


def run_entry_ids(layout: WorkspaceLayout, epoch_id: str, generation_id: str) -> list[str]:
    """Every board-entry id one generation holds a run record for, in order.

    One directory per board entry the generation was measured on. Order is
    numeric-aware, so entry ``t2`` precedes entry ``t10``. The board file
    remains the authority on which entries the contract defines; this
    reports which of them left a run on disk.
    """
    return sorted(
        _namespace_names(
            layout,
            WORKSPACE_RELATIVE_LAYOUT.runs_dir(epoch_id, generation_id),
            epoch_id,
            generation_id,
        ),
        key=natural_key,
    )


def round_indices(layout: WorkspaceLayout, epoch_id: str) -> list[int]:
    """Every evolve round index one epoch holds a record directory for, ascending.

    A directory whose name is not a decimal integer is not a round record
    and is skipped. An epoch with no ``rounds/`` directory yields the empty
    list, which is the honest report that nothing ran rather than an error.
    """
    out: list[int] = []
    for name in _namespace_names(layout, WORKSPACE_RELATIVE_LAYOUT.rounds_dir(epoch_id), epoch_id):
        try:
            out.append(int(name))
        except ValueError:
            continue
    return sorted(out)


def read_board(layout: WorkspaceLayout, epoch_id: str) -> list[dict[str, Any]] | None:
    """One epoch's board as the raw parsed JSONL line dicts (header included).

    Returns the list of per-line dict objects from ``board.jsonl`` (the
    ``board_meta`` header line is included as-is; callers that want only
    entries filter it). Returns ``None`` when the file is missing or
    unreadable, and silently skips blank / non-JSON / non-dict lines —
    mirroring the dashboard's prior inline board parsing.
    """
    try:
        text = layout.board(epoch_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        lines.append(obj)
    return lines


def read_experiment(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> dict[str, Any] | None:
    """One generation's ``experiment.json`` as a dict, or ``None``.

    Best-effort: a missing / malformed / non-object file yields ``None``.
    """
    exp = _read_json_value(layout.experiment(epoch_id, generation_id))
    return exp if isinstance(exp, dict) else None


def read_experiments(layout: WorkspaceLayout, epoch_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every generation's raw ``experiment.json`` for one epoch, in order.

    Enumerates the epoch's generation records (:func:`generation_ids`, so
    numeric-aware order) and yields ``(generation_id, experiment_dict)`` for
    each generation that has a readable ``experiment.json``. Generations
    without one are skipped. The raw experiment dict is returned untouched —
    callers add per-view shaping (patches, generation_id stamping, etc.).
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for generation_id in generation_ids(layout, epoch_id):
        exp = read_experiment(layout, epoch_id, generation_id)
        if exp is not None:
            out.append((generation_id, exp))
    return out


def read_gen_score(layout: WorkspaceLayout, epoch_id: str, generation_id: str) -> dict[str, Any]:
    """One generation's cached ``gen_score.json`` aggregate, or ``{}``.

    Returns the raw aggregate dict, or ``{}`` when the file is absent or
    malformed — matching the dashboard's prior ``_read_gen_score``.
    """
    score = _read_json_value(layout.gen_score(epoch_id, generation_id))
    return score if isinstance(score, dict) else {}


def read_gen_score_history(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> list[dict[str, Any]]:
    """Every aggregate ever written for one generation, oldest last.

    The parsed ``gen_score.history.jsonl`` lines (issue #122): one FULL
    aggregate per write — ``per_entry`` included — each stamped with the
    ``round_index`` it was measured in and a monotonic ``seq``. The last
    element is the measurement the flat ``gen_score.json`` still holds;
    the ones before it are the measurements it overwrote, which is the
    only way to see that an unchanged champion scored differently across
    its defences.

    Best-effort like every reader here: a missing / unreadable file
    yields ``[]`` and a malformed line is skipped, never raised.
    """
    try:
        text = layout.gen_score_history(epoch_id, generation_id).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_events_history(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int = 0,
) -> list[list[dict[str, Any]]]:
    """One replicate's retained raw telemetry, oldest measurement first.

    Returns one element per retained events file for that replicate — the
    archived predecessor (``events.prev.jsonl`` / ``events.r{n}.prev.jsonl``,
    when a re-measurement displaced one) followed by the current file — each
    element being that file's parsed JSONL records. A replicate measured
    once yields a single element; one never measured yields ``[]``.

    Best-effort: unreadable files and malformed lines are skipped.
    """
    out: list[list[dict[str, Any]]] = []
    for path in (
        layout.events_prev(epoch_id, generation_id, entry_id, replicate_index),
        layout.events(epoch_id, generation_id, entry_id, replicate_index),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        records: list[dict[str, Any]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
        out.append(records)
    return out


def read_loss(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str, entry_id: str
) -> dict[str, Any] | None:
    """One run's ``loss.json`` as a dict, or ``None``.

    Best-effort: a missing / malformed / non-object file yields ``None``.
    """
    loss = _read_json_value(layout.loss(epoch_id, generation_id, entry_id))
    return loss if isinstance(loss, dict) else None
