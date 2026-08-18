"""Per-replicate evidence for ONE ``(generation, board entry)`` cell.

A board entry can be executed several times against the same candidate: the
worker writes ``loss.json`` for the first draw and a sibling
``loss.r<N>.json`` for each further one. Every reader that needs
replicate-level precision on a cell enumerates the SAME files through this
module, so "what counts as a draw for this cell" has one definition:

* the eval matrix's replicate count / evidence tier
  (:func:`zicato.query.eval_view.build_eval_matrix`), and
* the matchup grid's per-entry score standard error
  (:func:`zicato.query.tournament_view.build_matchup_grid`).

Best-effort throughout (DQ3): an unreadable file is skipped and a pruned or
absent run directory yields an empty list, never an error.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from zicato.query.paths import WorkspacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zicato.core.loss import LossProfile

# Which replicate-index ranges count as EVIDENCE FOR A CELL (EVAL-VIEW.md §2.1 /
# §4.1). The board unit's replicate slots are reserved by purpose: real duel
# replicates count up from 0 (r0 = the canonical loss.json, plus the
# holdout-ladder confirmation re-runs, which reuse the low duel slots), the
# evidence-gate's paired draws sit at 4000+ (EVIDENCE_REPLICATE_BASE). Those two
# ranges are FRESH measurements of THIS cell, so they raise its evidence tier.
# EXCLUDED: A/A calibration at 1000+ (that is the champion NOISE-FLOOR trace — it
# feeds the flip badge, not the cell), the contract pre-flight at 2000+, the
# pre-tournament candidate screen at 3000+ (an ephemeral veto probe), and
# reflection draws at 5000+ (a meta-evaluation of the judges, not the candidate).
CELL_EVIDENCE_REPLICATE_RANGES: tuple[tuple[int, int], ...] = ((0, 1000), (4000, 5000))


def replicate_index(name: str) -> int | None:
    """The replicate index of a ``loss.json`` / ``loss.r<N>.json`` file, else ``None``.

    ``loss.json`` is replicate 0 (the canonical worker output); ``loss.r<N>.json``
    is replicate ``N`` (the sibling slot the worker writes). Any other filename is
    not a replicate loss file.
    """
    if name == "loss.json":
        return 0
    if name.startswith("loss.r") and name.endswith(".json"):
        mid = name[len("loss.r") : -len(".json")]
        if mid.isdigit():
            return int(mid)
    return None


def cell_replicate_draws_indexed(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[tuple[int, LossProfile]]:
    """The qualifying draws for ONE cell as ``(replicate index, profile)``, ascending.

    THE enumeration; :func:`cell_replicate_draws` is this without the
    indices. A reader that must NAME a draw (the execution plan renders one
    node per replicate) needs the index the filename carries, and deriving
    it a second time is how two surfaces start disagreeing about which
    files count.
    """
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    run_dir = loss_profile_path(paths.root, epoch_id, generation_id, entry_id).parent
    if not run_dir.is_dir():
        return []
    draws: list[tuple[int, LossProfile]] = []
    for child in sorted(run_dir.iterdir()):
        if not child.is_file():
            continue
        idx = replicate_index(child.name)
        if idx is None or not any(lo <= idx < hi for lo, hi in CELL_EVIDENCE_REPLICATE_RANGES):
            continue
        try:
            profile = read_loss_profile(child)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        draws.append((idx, profile))
    draws.sort(key=lambda pair: pair[0])
    return draws


def cell_replicate_draws(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[LossProfile]:
    """The qualifying per-replicate loss profiles for ONE cell, in index order.

    The DURABLE evidence source for a cell's replicate count: the loss files that
    actually exist under the run directory, filtered to
    :data:`CELL_EVIDENCE_REPLICATE_RANGES`. This is not the ``loss_profiles``
    index row count, which is always 1 (that table's key is ``run_id`` = one row
    per ``(generation, entry)``).
    """
    indexed = cell_replicate_draws_indexed(paths, epoch_id, generation_id, entry_id)
    return [profile for _, profile in indexed]


def _finite_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def replicate_scores(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, entry_id: str
) -> list[float]:
    """The continuous ``score`` of each qualifying replicate draw for one cell.

    A draw whose profile predates the continuous outcome (``score is None``) or
    carries a non-finite value contributes nothing, so a bool-only board yields
    ``[]`` rather than a fabricated 0.0.
    """
    scores: list[float] = []
    for profile in cell_replicate_draws(paths, epoch_id, generation_id, entry_id):
        value = _finite_score(getattr(profile, "score", None))
        if value is not None:
            scores.append(value)
    return scores


def standard_error(values: Sequence[float]) -> float | None:
    """Standard error of the mean over ``values``: sample sd / sqrt(n).

    Uses the SAMPLE standard deviation (Bessel-corrected, ``n - 1`` denominator),
    so this is the spread of the *mean* of the draws, not of one draw. ``None``
    for fewer than two values: a single draw measures no spread and must render
    as "unavailable", never as ``±0.000``.
    """
    if len(values) < 2:
        return None
    try:
        sd = statistics.stdev(values)
    except statistics.StatisticsError:
        return None
    if sd != sd or sd in (float("inf"), float("-inf")):
        return None
    return sd / math.sqrt(len(values))
