"""Typed canonical reads of the per-epoch / per-generation files.

The small set of best-effort readers the dashboard consumes, routed through
:class:`~zicato.workspace.layout.WorkspaceLayout` so the leaf filename joins
live in one place. Each reader returns the *raw* canonical structure (the
parsed JSON dict / list, or the parsed JSONL line dicts for the board) and
leaves view-specific shaping to the caller — the goal here is to own the
path math and the degrade-graceful parsing, not to re-implement the
per-endpoint projections.

Every reader is **best-effort**, returning the same empty / ``None`` value
the prior inline readers returned on a missing / unreadable / malformed
file — never a new exception.
"""

from __future__ import annotations

import json
from typing import Any

from zicato.workspace.epochs import _read_json_value
from zicato.workspace.layout import WorkspaceLayout


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

    Walks ``generations/*`` in numeric-aware id order and yields
    ``(generation_id, experiment_dict)`` for each generation that has a
    readable ``experiment.json``. Generations without one are skipped. The
    raw experiment dict is returned untouched — callers add per-view
    shaping (patches, generation_id stamping, etc.). Returns an empty list
    when the epoch has no ``generations/`` directory.
    """
    from zicato.workspace.epochs import natural_key  # noqa: PLC0415 — avoid import cycle

    gens_dir = layout.generations_dir(epoch_id)
    if not gens_dir.is_dir():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for gen_dir in sorted(gens_dir.iterdir(), key=lambda p: natural_key(p.name)):
        if not gen_dir.is_dir():
            continue
        exp = _read_json_value(gen_dir / "experiment.json")
        if isinstance(exp, dict):
            out.append((gen_dir.name, exp))
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
