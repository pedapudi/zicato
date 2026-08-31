"""Pre-tournament candidate screening (tryouts) — a veto that never ranks.

When best-of-N sampling yields N candidate experiments, this engine RUNS
each candidate on a small rotating TRAIN panel before the selection pass,
so a catastrophically-regressed candidate (one that breaks entries the
champion passes, or blows its wall-clock budget) is disqualified BEFORE it
can burn a tournament round. The semantics are strictly **veto-first**:

* the screen DISQUALIFIES; it does not rank. The best-of-N
  critic/heuristic still chooses among the survivors, and an all-vetoed
  slate falls back to critic-over-all — a veto can narrow but never empty
  the propose step.
* the panel scalar each candidate earns here is **selection-biased** by
  construction (a handful of champion-passing train entries, chosen for
  the veto) — it is advisory tiebreak material inside the slate only and
  is NEVER journaled as evidence, never compared against tournament
  scalars.

Relationship to racing, which it complements rather than overlaps: a racing
tournament's rung-0 halving is field-level screening DOWNSTREAM of the proposer
— it prunes applied challengers on a board slice after they are already lineage
children. This screen runs UPSTREAM, inside one propose-step's candidate slate,
before any child is minted into lineage. The two compose: the screen keeps a
broken candidate out of the field, racing prunes the mediocre field members.

Cache + lineage hygiene (the :mod:`zicato.epoch.preflight` template):
every screen run is an **ephemeral** evaluation — the candidate's patches
are applied into a tempdir scratch (never ``derive_generation``; the real
lineage is untouched), the :class:`~zicato.core.Generation` id is a
``{parent}-screen-r{round}c{i}`` name that can never match a real ``v\\d+``
generation, and the board is stamped with the reserved
:data:`SCREEN_REPLICATE_BASE` so the unit cache slots can never collide
with — or pre-seed — a real duel (0..), the A/A calibration (1000..), the
contract pre-flight (2000..) or the evidence gate (4000..); see the
reserved-ladder note on
:data:`zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE`. The
phantom ``generations/{screen-id}`` directory the unit cache creates is
removed in a ``finally:`` per candidate, and stale ``*-screen-*``
directories from a crashed prior run are swept at entry (self-heal).

Overfitting discipline: the panel is TRAIN-slice only (the caller selects
it from the train board — the holdout is never eligible), and every
result string carries COUNTS ONLY, never an entry id, so nothing here
widens the proposer's restricted visibility envelope (OVERFITTING.md §11).
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.loss import BUDGET_ABORT_CAUSE, is_infra_abort_cause
from zicato.core.types import Experiment
from zicato.proposer.best_of_n import CandidateScreenResult
from zicato.util.iso_time import now_iso as _now_iso

log = logging.getLogger("zicato.epoch.screen")

#: Replicate-index base for candidate-screen runs. Reserved on the
#: replicate ladder documented at
#: :data:`zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE`: real
#: duels count up from 0, A/A calibration draws at 1000, the contract
#: pre-flight at 2000, THIS screen at 3000, the evidence gate at 4000 —
#: so a screen run's cache slot can never collide with (or pre-seed)
#: anything a tournament, audit, pre-flight or evidence refit reads.
#: The confirm-before-veto re-run of a flipped entry uses ``+ 1`` (3001).
SCREEN_REPLICATE_BASE: int = 3000

#: Substring that marks an ephemeral screen generation id. The id shape is
#: ``{parent}-screen-r{round}c{i}`` — it can never match a real ``v\\d+``
#: generation id, and the entry-time sweep removes any directory carrying
#: this marker (crash self-heal).
_SCREEN_ID_MARKER: str = "-screen-"

#: The round + candidate a screen generation id names, parsed back out of it.
#: Built from :data:`_SCREEN_ID_MARKER` so the writer and the reader of the id
#: cannot drift apart.
_SCREEN_ID_RE = re.compile(re.escape(_SCREEN_ID_MARKER) + r"r(\d+)c(\d+)$")


def screen_generation_round(generation_id: str) -> int | None:
    """The round index a screen generation id names, else ``None``.

    An ephemeral screen snapshot is normally removed the moment its candidate
    settles, so a ``generations/{parent}-screen-r{round}c{i}`` directory only
    survives a crash. Its NAME is then the one thing on disk that states which
    round the draws inside it served — a reader that must place them has an
    anchor rather than a guess. ``None`` for every id that is not a screen id,
    including a real ``v<n>`` generation.
    """
    match = _SCREEN_ID_RE.search(generation_id)
    return int(match.group(1)) if match else None


@dataclass(frozen=True, slots=True)
class ScreenPanel:
    """The rotating train panel one round's screen runs every candidate on.

    ``entries`` is the deterministic panel (see
    :func:`select_screen_entries`); ``baseline_pass_ids`` names the subset
    the champion's replicate-0 baseline passes — the only entries a
    pass-flip veto may fire on. Panel entries OUTSIDE that set (the
    short-panel fill, or the whole panel on a cold start with no parent
    losses) serve crash detection only: with no passing baseline there is
    no flip to detect, so only a budget abort can veto there.
    """

    entries: tuple[BoardEntry, ...]
    baseline_pass_ids: frozenset[str]


def select_screen_entries(
    train_board: list[BoardEntry],
    parent_losses: list[Any],
    screen_entries: int,
    round_index: int,
) -> ScreenPanel:
    """Pick this round's screen panel. Pure and deterministic — no clock.

    The panel ROTATES across rounds so no fixed slice is mined forever:
    the champion-passing train entries (parent replicate-0 baseline,
    read off ``parent_losses``) are ordered lexicographically by id into
    a ring, and round ``r`` reads ``k`` entries starting at
    ``(r * k) % len(eligible)``. Wholly derived from
    ``(round_index, entry ids, baseline passes)`` — re-running the same
    round selects the same panel.

    * **Short panel** — fewer passing entries than ``k``: the remainder
      fills from the NON-passing train entries (same lexicographic-ring
      discipline). Fill entries carry no passing baseline, so they can
      never pass-flip-veto — they serve crash detection only.
    * **Cold start** — no parent losses at all (a baseline round): every
      train entry is fill (crash-only veto), rotated by the same ring.
    * **Holdout is never eligible**: the caller passes the TRAIN board
      only — this function never sees (and must never be handed) a
      holdout entry (OVERFITTING.md §11).
    """
    k = min(int(screen_entries), len(train_board))
    if k <= 0:
        return ScreenPanel(entries=(), baseline_pass_ids=frozenset())

    pass_ids = {
        str(loss.entry_id) for loss in parent_losses if getattr(loss, "pass_fail", None) is True
    }
    eligible = sorted((e for e in train_board if e.id in pass_ids), key=lambda e: e.id)
    fill_pool = sorted((e for e in train_board if e.id not in pass_ids), key=lambda e: e.id)

    def _ring(pool: list[BoardEntry], count: int) -> list[BoardEntry]:
        if not pool or count <= 0:
            return []
        count = min(count, len(pool))
        start = (round_index * k) % len(pool)
        return [pool[(start + j) % len(pool)] for j in range(count)]

    panel = _ring(eligible, k)
    panel.extend(_ring(fill_pool, k - len(panel)))
    return ScreenPanel(
        entries=tuple(panel),
        baseline_pass_ids=frozenset(e.id for e in panel if e.id in pass_ids),
    )


def sweep_stale_screen_dirs(workspace_root: Path, epoch_id: str) -> int:
    """Remove leftover ``*-screen-*`` phantom generation dirs. Self-heal.

    A crash between a screen run and its ``finally:`` cleanup can leave a
    phantom ``generations/{screen-id}`` directory (the unit cache writes
    its per-replicate ``loss.json`` there). Screen generations are
    ephemeral by contract — nothing may read them back — so any survivor
    is stale garbage: sweep them at screen entry. Returns the number of
    directories removed. Best-effort; a failure to remove is logged and
    skipped (the reserved replicate base keeps even a stale slot from
    colliding with anything real).
    """
    from zicato.workspace import WorkspaceLayout, generation_ids  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace_root)
    removed = 0
    candidates = [
        layout.generation_dir(epoch_id, generation_id)
        for generation_id in generation_ids(layout, epoch_id)
        if _SCREEN_ID_MARKER in generation_id
    ]
    for stale in candidates:
        try:
            shutil.rmtree(stale)
            removed += 1
        except OSError as exc:
            log.debug("stale screen dir %s not removed: %s", stale, exc)
    if removed:
        log.info("candidate screen: swept %d stale *-screen-* dir(s)", removed)
    return removed


async def run_candidate_screen(
    *,
    candidates: list[Experiment],
    adapter: Any,
    parent_gen: Generation,
    panel: ScreenPanel,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    disable_drift: tuple[Any, ...] = (),
    judge_only: bool = False,
) -> list[CandidateScreenResult]:
    """Screen every slate candidate on the round's train panel.

    Per candidate: apply its patches into a tempdir scratch (the real
    lineage is never touched), run the panel through the SAME board-unit
    runner every duel uses (reserved replicate
    :data:`SCREEN_REPLICATE_BASE`; the ephemeral checkout falls out of the
    store-unmanaged generation id automatically), aggregate the panel
    scalar, and classify:

    * candidate unit INFRA-aborted (:func:`is_infra_abort_cause`) — NO
      SIGNAL; an infra blip never vetoes.
    * candidate unit budget-aborted — IMMEDIATE veto (a wall-clock
      exhaustion is deterministic; re-running re-hits the same budget).
    * candidate FAILS an entry the champion baseline PASSES — a
      pass-flip, subject to **confirm-before-veto**: the flipped entries
      re-run ONCE at ``SCREEN_REPLICATE_BASE + 1``, and only a flip that
      flips TWICE vetoes. Under per-entry flip probability ``p`` (harness
      noise) the false-veto probability is bounded near ``p²`` per entry
      instead of ``p``.

    One result per candidate, in slate order. A per-candidate engine
    failure degrades to a no-signal, non-vetoed result — the screen must
    never fail (or empty) a propose step. Result strings carry counts
    only, never entry ids.
    """
    sweep_stale_screen_dirs(workspace_root, epoch_id)
    results: list[CandidateScreenResult] = []
    for index, candidate in enumerate(candidates):
        if not panel.entries:
            results.append(_no_signal_result("empty screen panel"))
            continue
        try:
            results.append(
                await _screen_one_candidate(
                    index=index,
                    candidate=candidate,
                    adapter=adapter,
                    parent_gen=parent_gen,
                    panel=panel,
                    weights=weights,
                    config=config,
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    round_index=round_index,
                    disable_drift=disable_drift,
                    judge_only=judge_only,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a screen error must never veto or fail
            log.debug("candidate screen: candidate %d not screened (%s)", index, exc)
            results.append(_no_signal_result("screen error (no signal)"))
    return results


def _no_signal_result(reason: str) -> CandidateScreenResult:
    """A non-vetoed, scalar-less result for a candidate the screen could not run."""
    return CandidateScreenResult(
        vetoed=False,
        reason=reason,
        scalar=None,
        entries_screened=0,
        baseline_passes=0,
        candidate_passes=0,
        confirmed=False,
    )


async def _screen_one_candidate(
    *,
    index: int,
    candidate: Experiment,
    adapter: Any,
    parent_gen: Generation,
    panel: ScreenPanel,
    weights: ScoringWeights,
    config: RuntimeConfig,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
) -> CandidateScreenResult:
    """Run ONE candidate over the panel and classify. See the caller."""
    from zicato.core.workspace import generation_dir  # noqa: PLC0415
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.scoring import aggregate_generation_score  # noqa: PLC0415
    from zicato.tournament.worker_transport import (  # noqa: PLC0415
        _stamp_disable_drift,
        _stamp_judge_only,
        _stamp_replicate_index,
    )

    screen_id = f"{parent_gen.id}{_SCREEN_ID_MARKER}r{round_index}c{index}"
    # The unit cache writes each panel unit's per-replicate loss.json under
    # generations/{screen_id}/ — a PHANTOM directory for an id that exists
    # in no lineage/index/journal. Always removed on the way out; a crash
    # in between is healed by the entry-time sweep.
    phantom_dir = generation_dir(workspace_root, epoch_id, screen_id)
    stamped = _stamp_judge_only(
        _stamp_disable_drift(list(panel.entries), disable_drift), judge_only
    )
    try:
        with tempfile.TemporaryDirectory(prefix="zicato-screen-") as scratch:
            candidate_root = Path(scratch) / "candidate"
            # apply_patches — NOT derive_generation: the scratch copy is the
            # whole world for this run; the real lineage never sees it.
            apply_patches(parent_gen.snapshot_root, list(candidate.patches), candidate_root)
            screen_gen = Generation(
                id=screen_id,
                epoch_id=epoch_id,
                parent_id=parent_gen.id,
                snapshot_root=candidate_root,
                created_at=_now_iso(),
                promoted=False,
                round_index=round_index,
            )
            losses = await _run_board_units_fast(
                adapter=adapter,
                child_gen=screen_gen,
                # Stamped like the calibration/pre-flight draws: seeded
                # harness noise derives from the STAMPED index, so a screen
                # run is an independent sample, and the reserved slot keeps
                # its cache entry out of every real duel's way.
                board=_stamp_replicate_index(stamped, SCREEN_REPLICATE_BASE),
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                match_id=f"candidate-screen:r{round_index}:c{index}",
                replicate_index=SCREEN_REPLICATE_BASE,
            )

            budget_aborts = 0
            infra_no_signal = 0
            flipped: list[BoardEntry] = []
            for entry in stamped:
                loss = losses.get(entry.id)
                if loss is None or is_infra_abort_cause(loss.abort_cause):
                    infra_no_signal += 1
                    continue
                if _is_budget_abort(loss):
                    budget_aborts += 1
                    continue
                if entry.id in panel.baseline_pass_ids and loss.pass_fail is False:
                    flipped.append(entry)

            # Confirm-before-veto: each flipped entry re-runs ONCE at the
            # reserved confirm slot; only a twice-flipped entry vetoes.
            # Skipped when a budget abort already vetoes (nothing to buy).
            confirmed_flips = 0
            if flipped and budget_aborts == 0:
                confirm_losses = await _run_board_units_fast(
                    adapter=adapter,
                    child_gen=screen_gen,
                    board=_stamp_replicate_index(flipped, SCREEN_REPLICATE_BASE + 1),
                    weights=weights,
                    config=config,
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    match_id=f"candidate-screen-confirm:r{round_index}:c{index}",
                    replicate_index=SCREEN_REPLICATE_BASE + 1,
                )
                for entry in flipped:
                    confirm = confirm_losses.get(entry.id)
                    if confirm is None or is_infra_abort_cause(confirm.abort_cause):
                        continue  # infra on the confirm run — no signal, no veto
                    if _is_budget_abort(confirm):
                        budget_aborts += 1
                        continue
                    if confirm.pass_fail is False:
                        confirmed_flips += 1

            usable = [
                loss for loss in losses.values() if not is_infra_abort_cause(loss.abort_cause)
            ]
            scalar: float | None
            if usable:
                agg = aggregate_generation_score(list(losses.values()), weights)
                scalar = float(agg.get("scalar", 0.0))
            else:
                scalar = None
            candidate_passes = sum(1 for loss in losses.values() if loss.pass_fail is True)
            vetoed = budget_aborts > 0 or confirmed_flips > 0
            confirmed = confirmed_flips > 0
            return CandidateScreenResult(
                vetoed=vetoed,
                reason=_summarize(
                    entries=len(panel.entries),
                    flips=len(flipped),
                    confirmed_flips=confirmed_flips,
                    budget_aborts=budget_aborts,
                    infra_no_signal=infra_no_signal,
                    vetoed=vetoed,
                ),
                scalar=scalar,
                entries_screened=len(panel.entries),
                baseline_passes=len(panel.baseline_pass_ids),
                candidate_passes=candidate_passes,
                confirmed=confirmed,
            )
    finally:
        shutil.rmtree(phantom_dir, ignore_errors=True)


def _is_budget_abort(loss: LossProfile) -> bool:
    """A deterministic wall-clock exhaustion — the immediate-veto class.

    Either the explicit :data:`~zicato.core.loss.BUDGET_ABORT_CAUSE` or a
    budget-exceeded profile with no infra cause (a worker whose own
    cooperative budget fired mid-run reduces cleanly with
    ``wall_clock_budget_exceeded=True`` and no ``abort_cause``).
    Re-running re-hits the same budget, so no confirm run is spent on it.
    """
    if is_infra_abort_cause(loss.abort_cause):
        return False
    return loss.abort_cause == BUDGET_ABORT_CAUSE or bool(loss.wall_clock_budget_exceeded)


def _summarize(
    *,
    entries: int,
    flips: int,
    confirmed_flips: int,
    budget_aborts: int,
    infra_no_signal: int,
    vetoed: bool,
) -> str:
    """The counts-only result summary. NEVER carries an entry id."""
    parts = [f"panel {entries}"]
    if budget_aborts:
        parts.append(f"budget-aborts {budget_aborts}")
    if flips:
        parts.append(f"pass-flips {flips} ({confirmed_flips} confirmed)")
    if infra_no_signal:
        parts.append(f"infra-no-signal {infra_no_signal}")
    verdict = "vetoed" if vetoed else "clear"
    return f"{verdict}: " + ", ".join(parts)


__all__ = [
    "SCREEN_REPLICATE_BASE",
    "ScreenPanel",
    "run_candidate_screen",
    "screen_generation_round",
    "select_screen_entries",
    "sweep_stale_screen_dirs",
]
