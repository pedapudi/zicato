"""Aggregations computed from the canonical workspace files.

:mod:`zicato.workspace.reads` answers which records exist and hands back one
record's raw parsed content. This module owns the next step: the quantities
that are computed by combining several of those records, or by decoding one
record's nested structure into typed rows. Each quantity has exactly one
definition here, and every reader that computes it from the filesystem calls
that definition:

* the per-judge attribution rows a run's loss profile records, and their
  weighted-loss totals across one generation's runs;
* an epoch's board as validated entries plus its board-level
  ``disable_drift`` header;
* the cumulative scalar score along a lineage, summed from the per-generation
  deltas the outcome records carry;
* the folded round records of an epoch's settled evolve rounds.

The consumers are the analysis-report gatherer
(:mod:`zicato.analyzer.report_data`) and the query readers that open the same
canonical files (:mod:`zicato.query.eval_view`,
:mod:`zicato.query.judge_view`, :mod:`zicato.query.execution_plan`). The
query readers that answer from ``index.db`` compute different quantities over
a derived store and do not consume this module: the per-generation per-judge
table (:func:`zicato.query.judge_view.build_per_judge_for_generation`) and
the drift-loss curve (:func:`zicato.query.gate_view.build_score_trajectory`)
stay index-backed, degradation messages included.

Every reader here is best-effort in the same way as the rest of the package:
a missing, unreadable, or malformed file yields the empty or ``None`` value
rather than an exception. Numeric fields decoded off a record are handed back
verbatim, because the two consuming layers coerce them differently — the
query layer projects a JSON payload through
:func:`zicato.query.paths.coerce_float`, and the totals below coerce for
arithmetic. Only the arithmetic rule lives here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.workspace.layout import WorkspaceLayout
from zicato.workspace.reads import read_loss, round_indices, run_entry_ids

if TYPE_CHECKING:  # pragma: no cover — imported for annotations only
    from zicato.epoch.round_log import RoundLogEnvelope, RoundRecord


@dataclass(frozen=True, slots=True)
class JudgeLossRow:
    """One judge's loss attribution as a run's loss profile records it.

    Mirrors :class:`zicato.core.loss.JudgeLoss`, the record the reducer
    writes, so the field spellings live in one place. ``judge_name`` is the
    empty string for the reducer's catch-all bucket of drift it could not
    pair with a judge; a caller that displays the bucket supplies its own
    label for it.

    The three loss numbers are the values found in the file, uncoerced: a
    record written by the reducer carries floats, and a caller decides what a
    field of any other type means for its own output.
    """

    judge_name: str
    raw_loss: Any
    weight: Any
    weighted_loss: Any


@dataclass(frozen=True, slots=True)
class BoardRead:
    """One epoch's board: its validated entries and its header metadata.

    ``entries`` are :class:`zicato.core.types.BoardEntry` objects in file
    order. ``disable_drift`` is the board-level list of drift kinds the
    header switches off, as strings.
    """

    entries: tuple[Any, ...]
    disable_drift: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScalarStep:
    """One generation's place in the scalar cumulation.

    ``is_baseline`` marks a generation that seeds the lineage rather than
    inheriting from a parent. ``scalar_score_delta`` is the change in scalar
    score its tournament outcome recorded, and is ``0.0`` for a generation
    that has no outcome yet.
    """

    generation_id: str
    parent_generation_id: str
    is_baseline: bool
    scalar_score_delta: float


def _as_float(value: Any) -> float:
    """Coerce ``value`` to ``float``, defaulting to ``0.0`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def judge_loss_rows(loss: dict[str, Any] | None) -> tuple[JudgeLossRow, ...]:
    """The per-judge attribution rows one run's loss profile records.

    ``loss`` is a parsed ``loss.json`` (see
    :func:`zicato.workspace.reads.read_loss`). Rows come back in file order.
    A profile with no ``per_judge_loss`` array, or one whose array holds
    anything but objects, yields no rows — the board fired no custom judge,
    or the record predates the attribution.
    """
    if not isinstance(loss, dict):
        return ()
    raw = loss.get("per_judge_loss")
    if not isinstance(raw, list):
        return ()
    rows: list[JudgeLossRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            JudgeLossRow(
                judge_name=str(item.get("judge_name", "") or ""),
                raw_loss=item.get("raw_loss"),
                weight=item.get("weight"),
                weighted_loss=item.get("weighted_loss"),
            )
        )
    return tuple(rows)


def per_judge_loss_totals(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> tuple[tuple[str, float], ...]:
    """Weighted loss per judge, summed across one generation's runs.

    Reads every board-entry run the generation holds
    (:func:`zicato.workspace.reads.run_entry_ids`) and adds each run's
    per-judge weighted loss into a total keyed by judge name. The empty
    judge name keys the reducer's unattributed bucket and is totalled like
    any other. A run with no readable loss profile contributes nothing.

    Totals come back sorted by judge name. A weighted loss that is not a
    number contributes ``0.0``.
    """
    totals: dict[str, float] = {}
    for entry_id in run_entry_ids(layout, epoch_id, generation_id):
        loss = read_loss(layout, epoch_id, generation_id, entry_id)
        if loss is None:
            continue
        for row in judge_loss_rows(loss):
            totals[row.judge_name] = totals.get(row.judge_name, 0.0) + _as_float(row.weighted_loss)
    return tuple(sorted(totals.items()))


def read_board_entries(layout: WorkspaceLayout, epoch_id: str) -> BoardRead | None:
    """One epoch's board as validated entries, or ``None`` when it will not parse.

    Parses through :func:`zicato.board.jsonl.load_board_with_meta`, the strict
    loader that validates each entry against its discriminant. ``None``
    reports that the board is missing or that the loader rejected it — a
    board written against an older schema, a malformed line, a duplicate
    entry id. A caller that can still use the raw lines re-reads them
    tolerantly; a caller that needs validated entries treats ``None`` as an
    empty board.
    """
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415

    try:
        entries, disable_drift, _judge_only = load_board_with_meta(layout.board(epoch_id))
    except Exception:  # noqa: BLE001 — best-effort, like every reader here
        return None
    return BoardRead(
        entries=tuple(entries),
        disable_drift=tuple(str(kind) for kind in disable_drift),
    )


def cumulative_scalars(
    steps: Iterable[ScalarStep], *, baseline: float = 0.0
) -> list[tuple[str, float]]:
    """The cumulative scalar score of each step, in the order given.

    A baseline generation takes ``baseline``; every other generation takes
    its parent's cumulative score plus its own delta. A generation whose
    parent has no cumulative score yet — the parent is outside ``steps``, or
    comes after it — inherits ``baseline`` in the parent's place.

    ``steps`` is consumed in order, so the caller's order decides which
    parents are already resolved. Lineage order (parents before children) is
    what makes every cumulation land on a real parent score.

    The result is a list rather than a mapping because two records can carry
    the same generation id, and each of them still has its own score at its
    own position. A caller that wants the mapping builds it with ``dict``,
    which keeps the last score written for an id.
    """
    scores: dict[str, float] = {}
    out: list[tuple[str, float]] = []
    for step in steps:
        if step.is_baseline:
            score = baseline
        else:
            score = scores.get(step.parent_generation_id, baseline) + step.scalar_score_delta
        scores[step.generation_id] = score
        out.append((step.generation_id, score))
    return out


def read_round_log(
    workspace_root: Path, epoch_id: str, round_index: int
) -> tuple[list[RoundLogEnvelope], bool]:
    """One evolve round's logged events, and whether the log read cleanly.

    A torn tail is dropped by the log reader itself and the round still
    reads cleanly. Interior corruption violates the log's append-only
    invariant and raises there; it arrives here as ``([], False)``, which
    tells the caller the round has events it could not reach rather than no
    events at all.
    """
    from zicato.epoch.round_log import RoundLog  # noqa: PLC0415

    try:
        return RoundLog(workspace_root, epoch_id, round_index).read(), True
    except Exception:  # noqa: BLE001 — best-effort, like every reader here
        return [], False


def read_round_records(layout: WorkspaceLayout, epoch_id: str) -> tuple[RoundRecord, ...]:
    """Every settled evolve round of one epoch, folded into a record.

    Rounds come back in ascending round order
    (:func:`zicato.workspace.reads.round_indices`). A round whose log cannot
    be read to its end, or whose events will not fold, is skipped rather than
    failing the whole read. An epoch with no round yet — nothing has settled
    — yields the empty tuple, which is the honest report that the round-level
    quantities have nothing to say.
    """
    from zicato.epoch.round_log import fold_round_record  # noqa: PLC0415

    records: list[RoundRecord] = []
    for round_index in round_indices(layout, epoch_id):
        events, readable = read_round_log(layout.root, epoch_id, round_index)
        if not readable:
            continue
        try:
            records.append(fold_round_record(events))
        except Exception:  # noqa: BLE001 — one bad round never sinks the read
            continue
    return tuple(records)
