"""Epoch enumeration + the single canonical ordering definition.

This module is the **one** place that orders epochs and the **one** place
that enumerates the ``epochs/`` directory. The canonical order is
timestamp-first (:func:`epoch_sort_key`): an epoch's recorded ``created_at``
from its ``config.json``, with the numeric-aware id as a deterministic
tiebreaker and the fallback when the timestamp is absent. Generations order
by the numeric-aware key of their id within an epoch.

Enumerating ``epochs/`` at each call site invites divergent sorts — sorting
by name where the contract requires timestamp order is a real ordering bug —
so every enumeration routes through :func:`iter_epochs` /
:func:`list_epoch_ids`, and the order is uniform by construction.

The ordering primitives live here as the single definition. Epochs order by
:func:`epoch_sort_key` over :func:`epoch_created_at` and :func:`natural_key`;
generations order by :func:`natural_key` alone, which puts ``v2`` before
``v10``. :func:`generation_round_number` is the single parser of the round
number a ``vN`` id encodes, and :func:`next_generation_id` the single minter
of the next one, so one rule defines what a generation id is.
:mod:`zicato.query.paths` re-exports the epoch primitives, so an import from
either module resolves to the same function.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Best-effort JSON read; missing / empty / malformed -> ``None``. Imported
# lazily-stable here (same helper the dashboard's prior inline readers used,
# so config.json parsing degrades identically).
from zicato.storage import read_json
from zicato.workspace.layout import WorkspaceLayout

_NUM_RUN = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple[tuple[int, Any], ...]:
    """Numeric-aware sort key so ``v2`` sorts before ``v10`` (and ``e2``
    before ``e10``), instead of the lexical order that puts ``v10`` first.

    Splits the string into alternating text / digit runs and compares digit
    runs numerically. This yields chronological order for the sequentially
    minted ``eN`` epoch ids and ``vN`` generation ids, and preserves the
    already-chronological lexical order of ISO-date-prefixed epoch ids
    (``2026-04-01_slug``). The leading ``0``/``1`` tag keeps text and number
    runs from ever being compared across types.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part) for part in _NUM_RUN.split(name) if part
    )


def generation_round_number(generation_id: str) -> int | None:
    """The round number encoded in a ``vN`` generation id, or ``None`` when the
    id does not follow that scheme.

    ``v0`` is the seed generation, so the number is also the count of rounds
    that produced the generation. Callers that only need ordering use
    :func:`natural_key`, which orders ``vN`` ids by the same number without
    parsing them.
    """
    suffix = generation_id[1:]
    return int(suffix) if generation_id.startswith("v") and suffix.isdigit() else None


def next_generation_id(existing: Iterable[str]) -> str:
    """The id to mint for the generation that follows ``existing``.

    ``v`` and one past the highest round number present, or ``v0`` when none
    is. Ids outside the ``vN`` scheme take no part in the count, so a stray
    directory can never make the minter skip or reuse a round number.
    """
    numbers = [n for n in map(generation_round_number, existing) if n is not None]
    return f"v{max(numbers, default=-1) + 1}"


def _read_json_value(path: Path) -> Any | None:
    """Best-effort JSON read; missing / empty / malformed -> ``None``."""
    try:
        return read_json(path)
    except Exception:
        return None


def epoch_created_at(epoch_dir: Path) -> str:
    """The epoch's recorded creation timestamp from its ``config.json`` (an
    ISO-8601 string whose lexical order is chronological), or ``""`` when
    absent so ordering falls back to the numeric id key.
    """
    cfg = _read_json_value(epoch_dir / "config.json")
    if isinstance(cfg, dict):
        ts = cfg.get("created_at")
        if isinstance(ts, str) and ts:
            return ts
    return ""


def epoch_sort_key(epoch_dir: Path) -> tuple[str, tuple[tuple[int, Any], ...]]:
    """Order epochs by recorded creation time, with the numeric-aware id as a
    deterministic tiebreaker (and the fallback when the timestamp is missing).
    Sorting by the actual timestamp — not the id — keeps date-named or
    mixed-scheme epochs in true chronological order.
    """
    return (epoch_created_at(epoch_dir), natural_key(epoch_dir.name))


@dataclass(frozen=True)
class Epoch:
    """One epoch on disk, with its directory + cached sort key.

    A typed handle the canonical enumeration yields so callers do not
    re-derive the epoch directory or its ordering key. ``id`` is the
    directory name; ``directory`` is ``epochs/<id>``; ``created_at`` is the
    recorded ``config.json`` timestamp (``""`` when absent, mirroring the
    ordering fallback).
    """

    id: str
    directory: Path
    created_at: str

    @property
    def sort_key(self) -> tuple[str, tuple[tuple[int, Any], ...]]:
        """The canonical timestamp-first ordering key for this epoch."""
        return (self.created_at, natural_key(self.id))


def iter_epochs(layout: WorkspaceLayout) -> list[Epoch]:
    """Every epoch on disk as a typed :class:`Epoch`, in canonical order.

    The single enumeration of the ``epochs/`` directory: returns one
    :class:`Epoch` per subdirectory, sorted by :func:`epoch_sort_key`
    (timestamp-first, numeric-id tiebreaker). Returns an empty list when the
    workspace has no ``epochs/`` directory. Best-effort: an epoch whose
    ``config.json`` is missing / unreadable simply reports ``created_at=""``
    and orders by its id.
    """
    epochs_dir = layout.epochs_dir
    if not epochs_dir.is_dir():
        return []
    epochs = [
        Epoch(id=d.name, directory=d, created_at=epoch_created_at(d))
        for d in epochs_dir.iterdir()
        if d.is_dir()
    ]
    epochs.sort(key=lambda e: e.sort_key)
    return epochs


def list_epoch_ids(layout: WorkspaceLayout) -> list[str]:
    """Every epoch id on disk, in canonical (timestamp-first) order.

    The set of epochs a ``?epoch=<id>`` request may legally resolve to.
    Returns an empty list when the workspace has no ``epochs/`` directory.
    """
    return [e.id for e in iter_epochs(layout)]


def read_epoch_config(layout: WorkspaceLayout, epoch_id: str) -> dict[str, Any] | None:
    """One epoch's ``config.json`` as a dict, or ``None`` when absent/malformed.

    Best-effort: a missing, empty, malformed, or non-object ``config.json``
    yields ``None`` — never an exception.
    """
    cfg = _read_json_value(layout.epoch_config(epoch_id))
    return cfg if isinstance(cfg, dict) else None
