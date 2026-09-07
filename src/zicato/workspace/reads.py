"""Typed canonical reads of the per-epoch / per-generation records.

Two jobs live here. The first is enumeration: :func:`generation_ids`,
:func:`run_entry_ids` and :func:`round_indices` answer "which generation /
run / round records does this epoch hold", and they are the ONLY place in
the tree that asks. The second is the leaf reads the enumerations feed —
board, telemetry, loss — each routed through
:class:`~zicato.workspace.layout.WorkspaceLayout` so the filename joins live
in one place. A record whose shape has an owning codec is not read here:
experiments are decoded by :mod:`zicato.epoch.journal`, and generation scores by
:mod:`zicato.tournament.scoring`. Both consume the generation enumeration below. Each reader here
returns the *raw* canonical structure (the parsed JSON dict / list, or the
parsed JSONL line dicts for the board) and leaves view-specific shaping to
the caller.

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

from zicato.core.loss import has_execution_evidence, validate_loss_identity
from zicato.core.measurement import (
    UNKNOWN_SEED,
    BaseSeed,
    MeasurementDraw,
    iter_measurement_attempts,
    recorded_artifact_measurement,
)
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
    """Read accepted board rows, including the optional metadata header."""
    from zicato.board.jsonl import load_board_rows  # noqa: PLC0415

    return load_board_rows(layout.board(epoch_id))


def generation_base_seed(layout: WorkspaceLayout, epoch_id: str, generation_id: str) -> BaseSeed:
    """Return the aggregate's selected seed; absent provenance remains unknown."""
    from zicato.epoch._storage import RecordError  # noqa: PLC0415
    from zicato.tournament.scoring import read_gen_score  # noqa: PLC0415

    try:
        record = read_gen_score(layout, epoch_id, generation_id)
    except RecordError as exc:
        raise ValueError(str(exc)) from exc
    score = record.to_dict() if record is not None else {}
    if "base_seed" in score and score.get("generation_id") != generation_id:
        raise ValueError("generation score identity conflicts with its path")
    seed = score.get("base_seed", UNKNOWN_SEED)
    if seed is None or type(seed) is int or seed is UNKNOWN_SEED:
        return seed
    raise ValueError("generation score base_seed must be an integer or null")


def read_events_history(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int = 0,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
) -> list[list[dict[str, Any]]]:
    """One replicate's retained raw telemetry, oldest measurement first.

    Committed measurement archives come first, followed by the historical
    predecessor file and the current file. Each element contains one file's
    parsed JSONL records. Pending archive copies are excluded. The seed argument
    selects one measurement namespace; unknown history is distinct from null.

    Best-effort: unreadable files and malformed lines are skipped.
    """
    out: list[list[dict[str, Any]]] = []
    loss_path = layout.loss(epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed)
    archived_events = [
        path.with_name(path.name.replace("loss", "events", 1)).with_suffix(".jsonl")
        for path in iter_measurement_attempts(loss_path)
    ]
    for path in (
        *archived_events,
        layout.events_prev(epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed),
        layout.events(epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed),
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
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    base_seed: BaseSeed = UNKNOWN_SEED,
    replicate_index: int = 0,
) -> dict[str, Any] | None:
    """One run's ``loss.json`` as a dict, or ``None``.

    Missing, malformed, unstarted, or conflicting measurements yield ``None``.
    Raw files remain available to the execution audit.
    """
    path = layout.loss(epoch_id, generation_id, entry_id, replicate_index, base_seed=base_seed)
    loss = _read_json_value(path)
    if not isinstance(loss, dict):
        return None
    try:
        if not has_execution_evidence(loss):
            return None
        identity = recorded_artifact_measurement(
            layout.run_dir(epoch_id, generation_id, entry_id),
            path,
            measurement=MeasurementDraw.from_json(loss["measurement"])
            if "measurement" in loss
            else None,
            match_id=str(loss.get("match_id") or ""),
        )
        validate_loss_identity(
            loss,
            epoch_id=epoch_id,
            generation_id=generation_id,
            entry_id=entry_id,
            measurement=identity,
        )
    except (TypeError, ValueError):
        return None
    return loss
