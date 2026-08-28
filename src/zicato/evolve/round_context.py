"""Round-pipeline **round-context** stage — the pre-propose context builders.

Split out of :mod:`zicato.orchestrator` as part of the Finding-2 typed
round-pipeline decomposition (``docs/design/REIMPLEMENTATION.md``). This is the
pipeline's pre-propose seam (the plan's "screen" stage): the four builders that
assemble the round's proposer-context inputs ONCE per round, beside each other,
and thread them as plain DATA onto
:class:`~zicato.proposer.agent.ProposerContext` so the proposer stack stays
IO-free:

* :func:`_build_candidate_screen_runner` — the best-of-N candidate-screen
  closure (``proposer_quality.screen_entries`` / ``best_of_n``);
* :func:`_build_recombination_pair` (+ the pure slot rule
  :func:`_recombine_pair_for_slot`) — the WS-REC recombination pair;
* :func:`_build_genealogy_items` — the WS-GENE genealogy channel;
* :func:`_build_calibration_summary` — the WS-CAL critic-calibration channel.

Each builder is OFF by default (a contract opt-in flips it on) and every read
is best-effort, so an unbuilt index / unreadable record simply yields the OFF
value and a byte-identical round. Two names are referenced from outside
:mod:`zicato.orchestrator` (``_build_candidate_screen_runner`` from the
candidate-screen + decision-procedure tests, ``_recombine_pair_for_slot`` from
the best-of-N test), so the orchestrator re-imports every name and lists those
two in ``__all__`` for mypy's no-implicit-reexport; the three purely internal
builders are re-imported but not listed. Stable collaborators
(``ingest._index_db_path``, ``lifecycle_services._beat``, the heartbeat helper)
are direct top-level imports; the heavier ``epoch`` / ``proposer`` / ``query`` /
``index`` siblings stay lazy call-time imports exactly as they were inline. The
module logger keeps the ``zicato.orchestrator`` name so records stay
byte-identical.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Experiment, Generation
from zicato.evolve.ingest import _index_db_path
from zicato.evolve.lifecycle_services import _beat
from zicato.runtime.heartbeat import HeartbeatBeater

if TYPE_CHECKING:
    from zicato.proposer.best_of_n import ScreenRunner

log = logging.getLogger("zicato.orchestrator")


def _build_candidate_screen_runner(
    *,
    weights: Any,
    adapter: Any,
    parent_gen: Generation,
    train_board: list[Any],
    parent_losses: list[Any],
    config: Any,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    beater: HeartbeatBeater | None,
) -> ScreenRunner | None:
    """Build this round's candidate-screen closure, or ``None`` when OFF.

    ``None`` — the DEFAULT — unless the contract opts in with
    ``proposer_quality.screen_entries > 0`` AND ``best_of_n > 1`` (a
    single-sample proposer has no slate to screen): the propose path then
    carries no screen callable at all and is byte-identical.

    When built, the closure binds ONE deterministic rotating TRAIN panel
    for the whole round (:func:`zicato.epoch.screen.select_screen_entries`
    over the champion's replicate-0 baseline — the holdout is never
    eligible) so every propose site this round (the gauntlet's single
    challenger, every slot of a multi-challenger field) screens on the
    same panel. Each invocation stamps a ``screening:r{round}`` heartbeat
    phase before the panel runs, so the stall detector attributes the
    extra propose-step wall-clock honestly.
    """
    quality = weights.proposer_quality
    if quality.screen_entries <= 0 or quality.best_of_n <= 1:
        return None
    from zicato.epoch.screen import run_candidate_screen, select_screen_entries  # noqa: PLC0415

    panel = select_screen_entries(train_board, parent_losses, quality.screen_entries, round_index)

    async def _screen(candidates: Sequence[Experiment]) -> list[Any]:
        _beat(
            beater,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=parent_gen.id,
            round_index=round_index,
            phase=f"screening:r{round_index}",
        )
        return await run_candidate_screen(
            candidates=list(candidates),
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

    return _screen


def _recombine_pair_for_slot(recombine_pair: Any, offset: int) -> Any:
    """Thread one round's recombination pair onto its first candidate only.

    Every batch gives the pair to its first slot. On a multi-challenger
    field, letting every slot mint the identical
    union would collapse the extra slots into field-diversity soft-rejects
    because an exact duplicate is cut from the run slate. A one-candidate
    gauntlet naturally gives the pair to its only slot.
    """
    return recombine_pair if offset == 0 else None


def _build_recombination_pair(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    train_entry_ids: frozenset[str],
    mutations: list[Any],
) -> Any:
    """Select this round's recombination pair (WS-REC), or ``None`` when OFF.

    ``None`` — the DEFAULT — unless the contract opts in with
    ``proposer_quality.recombine`` AND ``best_of_n > 1`` (a single-sample
    proposer has no slate slot to mint into): the propose path then carries
    no pair at all and is byte-identical.

    The IO half of the recombination selector, built ONCE per round beside
    the screen builder and threaded as plain DATA
    (:attr:`~zicato.proposer.agent.ProposerContext.recombine_pair` — the
    selection depends only on round-start state, so the proposer stack
    stays IO-free). Reads, all best-effort:

    * the current epoch's durable experiment RECORDS (the ``generations/``
      records tree outlives GC — snapshots are pruned, records never are),
      each through :func:`zicato.epoch.journal.read_experiment` GUARDED —
      an unreadable/incomplete record (a missing patch file) simply drops
      that candidate. The pool is capped at the
      :data:`~zicato.epoch.recombine.RECOMBINE_POOL_MAX` most-recent
      settled REJECTS.
    * per-candidate improved/regressed entry sets via
      :func:`zicato.query.tournament_view.build_matchup_grid` (disk
      ``loss.json`` — durable, index-free), INTERSECTED with this round's
      TRAIN entry ids before they are counted: entry ids never leave this
      builder — the :class:`~zicato.proposer.recombine.RecombinationPair`
      carries counts + patches + hypothesis text only (the envelope
      boundary; a holdout entry can never influence the selection).
    * ONE best-effort Elo read (:func:`zicato.index.query.elo_for_epoch`)
      for the ranking's summed-Elo key; an absent index default-fills.

    The pure engine (:mod:`zicato.epoch.recombine`) then applies the 8
    eligibility predicates and the 4-key deterministic ranking. ANY
    exception anywhere → DEBUG log → ``None`` → a byte-identical round
    (recombination must never fail a propose step).
    """
    quality = weights.proposer_quality
    if not getattr(quality, "recombine", False) or quality.best_of_n <= 1:
        return None
    try:
        from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
        from zicato.core.workspace import generations_dir  # noqa: PLC0415
        from zicato.epoch.journal import read_experiment  # noqa: PLC0415
        from zicato.epoch.recombine import (  # noqa: PLC0415
            RECOMBINE_POOL_MAX,
            ParentCandidate,
            eligible_parents,
            rank_pairs,
        )
        from zicato.proposer.recombine import RecombinationPair  # noqa: PLC0415
        from zicato.query.paths import WorkspacePaths  # noqa: PLC0415
        from zicato.query.tournament_view import build_matchup_grid  # noqa: PLC0415

        gens_root = generations_dir(workspace_root, epoch_id)
        if not gens_root.is_dir():
            return None

        def _gen_sort_key(gid: str) -> tuple[int, str]:
            # v-numbered ids sort numerically (v10 after v9); anything else
            # falls back lexicographically after them.
            if gid.startswith("v") and gid[1:].isdigit():
                return (int(gid[1:]), "")
            return (-1, gid)

        gen_ids = sorted(
            (d.name for d in gens_root.iterdir() if d.is_dir()),
            key=_gen_sort_key,
            reverse=True,
        )

        # One pass over the records: collect the pool of most-recent
        # settled rejects (capped) and the already-tried pair set (#5 —
        # every PERSISTED recombined_from, whatever its decision: a
        # round-spending mint never re-mints; a vetoed, unpersisted one
        # may retry because it never reached disk).
        pool: list[Any] = []
        tried: set[frozenset[str]] = set()
        for gid in gen_ids:
            if gid == parent_id:
                continue
            try:
                exp = read_experiment(workspace_root, epoch_id, gid)
            except Exception as exc:  # noqa: BLE001 — unreadable record: skip
                log.debug("recombine: record %s/%s unreadable (%s)", epoch_id, gid, exc)
                continue
            if len(exp.recombined_from) == 2:
                # A prior mint records its pair as tried (any outcome) but is
                # NEVER a pool candidate itself (predicate #4 would drop it
                # later anyway) — skipping here keeps it from wasting one of
                # the RECOMBINE_POOL_MAX slots and a matchup-grid read.
                tried.add(frozenset(exp.recombined_from))
                continue
            if exp.outcome is None or exp.outcome.tournament_decision != "rejected":
                continue
            if len(pool) < RECOMBINE_POOL_MAX:
                pool.append(exp)
        if len(pool) < 2:
            return None

        # ONE best-effort Elo read for the whole pool (fresh per round —
        # the fold runs at every ingest, so the index is as settled as it
        # will get at round start). Absent index / missing rows → {}.
        elo_by_gid: dict[str, float] = {}
        try:
            from zicato.index.query import elo_for_epoch  # noqa: PLC0415

            # The canonical index location every consumer uses
            # (``zicato repair index`` reconciles ``{workspace_root}/index.db``).
            for row in elo_for_epoch(_index_db_path(workspace_root), epoch_id):
                if row["elo"] is not None:
                    elo_by_gid[str(row["generation_id"])] = float(row["elo"])
        except Exception as exc:  # noqa: BLE001 — Elo is advisory ranking material
            log.debug("recombine: Elo read skipped (%s)", exc)

        paths = WorkspacePaths(workspace_root)
        candidates: list[ParentCandidate] = []
        for exp in pool:
            grid = build_matchup_grid(paths, epoch_id, parent_id, exp.generation_id)
            improved: set[str] = set()
            regressed: set[str] = set()
            for row in grid.get("entry_grid", []):
                entry_id = str(row.get("entry_id", ""))
                if entry_id not in train_entry_ids:
                    continue  # the envelope point: holdout never counts
                # PASS-FLIP sets, not the grid's drift-only ``won_by``:
                # per-run drift folds every remaining defect into EVERY
                # entry's loss, so a strictly-better challenger "wins" all
                # entries on drift and two single-fix parents could never
                # read as complementary. The pass bit is the per-entry
                # signal a fix actually owns: improved = a champion-failing
                # entry this challenger passes; regressed = the inverse.
                # KNOWN NARROWING: a pair whose improvements are PURELY
                # drift-side (no pass flip — e.g. two independent verbosity
                # fixes on an all-passing board) is invisible to this
                # selector and never recombines mechanically. Deliberate:
                # per-entry drift deltas are noisy single-sample verdicts
                # (the same reason cross-regression is a ranking penalty,
                # not a filter). Such pairs remain reachable through the
                # in-context genealogy channel; a drift-delta-with-
                # confirmation variant is a documented future seam.
                parent_pass = row.get("parent_pass")
                child_pass = row.get("child_pass")
                if parent_pass is False and child_pass is True:
                    improved.add(entry_id)
                elif parent_pass is True and child_pass is False:
                    regressed.add(entry_id)
            hyp = exp.hypothesis
            candidates.append(
                ParentCandidate(
                    generation_id=exp.generation_id,
                    decision=(exp.outcome.tournament_decision if exp.outcome is not None else ""),
                    parent_generation_id=exp.parent_generation_id,
                    is_placebo=hyp.core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER),
                    is_recombined=bool(exp.recombined_from),
                    patch_mutation_ids=frozenset(p.mutation_id for p in exp.patches),
                    improved_entry_ids=frozenset(improved),
                    regressed_entry_ids=frozenset(regressed),
                    elo=elo_by_gid.get(exp.generation_id),
                    patches=exp.patches,
                    core_idea=hyp.core_idea,
                    expected_drift_movements=hyp.expected_drift_movements,
                    expected_metric_movements=hyp.expected_metric_movements,
                )
            )

        manifest_ids = frozenset(str(m.id) for m in mutations)
        eligible = eligible_parents(candidates, champion_id=parent_id, manifest_ids=manifest_ids)
        # WS-MERGE: the merge mode gates the disjointness predicate — "llm"
        # relaxes #7 for pair selection so an OVERLAPPING pair (which only an
        # LLM merge can compose) is eligible; "mechanical" (default) keeps #7
        # hard and selects byte-identically to before this knob.
        merge_mode = getattr(quality, "recombine_merge", "mechanical")
        pair = rank_pairs(eligible, tried_pairs=frozenset(tried), merge_mode=merge_mode)
        if pair is None:
            return None
        a, b = pair
        log.debug(
            "recombine: selected pair (%s, %s) — coverage %d, cross-regression %d",
            a.generation_id,
            b.generation_id,
            len(a.improved_entry_ids | b.improved_entry_ids),
            len(a.regressed_entry_ids | b.regressed_entry_ids),
        )
        # WS-MERGE: the LLM merge prompt carries each parent's whole-candidate
        # BANDED outcome (envelope-clean — the exact Δscalar is bucketed HERE
        # and discarded, only the coarse label reaches the pair). Reuses the
        # experiment-memory band vocabulary; "" for an unsettled delta.
        from zicato.proposer.prompts import _bucket_scalar_delta  # noqa: PLC0415

        exp_by_gid = {e.generation_id: e for e in pool}

        def _banded_outcome(gid: str) -> str:
            exp = exp_by_gid.get(gid)
            delta = exp.outcome.scalar_score_delta if exp is not None and exp.outcome else None
            return _bucket_scalar_delta(delta) if delta is not None else ""

        return RecombinationPair(
            a_generation_id=a.generation_id,
            b_generation_id=b.generation_id,
            a_patches=a.patches,
            b_patches=b.patches,
            a_core_idea=a.core_idea,
            b_core_idea=b.core_idea,
            a_improved_count=len(a.improved_entry_ids),
            b_improved_count=len(b.improved_entry_ids),
            combined_improved_count=len(a.improved_entry_ids | b.improved_entry_ids),
            combined_regressed_count=len(a.regressed_entry_ids | b.regressed_entry_ids),
            a_banded_outcome=_banded_outcome(a.generation_id),
            b_banded_outcome=_banded_outcome(b.generation_id),
            a_expected_drift_movements=a.expected_drift_movements,
            b_expected_drift_movements=b.expected_drift_movements,
            a_expected_metric_movements=a.expected_metric_movements,
            b_expected_metric_movements=b.expected_metric_movements,
        )
    except Exception as exc:  # noqa: BLE001 — recombination must never fail a round
        log.debug("recombine: pair selection skipped (%s)", exc)
        return None


def _build_genealogy_items(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
) -> tuple[Any, ...]:
    """Sample this round's genealogy items (WS-GENE), or ``()`` when OFF.

    ``()`` — the DEFAULT — unless the contract opts in with
    ``proposer_quality.genealogy > 0``: the propose path then carries no items
    at all and is byte-identical.

    The IO half of the genealogy channel, built ONCE per round beside the
    recombination + screen builders and threaded as plain DATA (a
    ``tuple[GenealogyItem, ...]`` on
    :attr:`~zicato.proposer.agent.ProposerContext.genealogy` — all best-of-N
    slots see the same items, so the proposer can merge/diverge in context).
    Reads, all best-effort:

    * the current epoch's durable experiment RECORDS (the ``generations/``
      records tree outlives GC), each through
      :func:`zicato.epoch.journal.read_experiment` GUARDED — an
      unreadable/incomplete record simply drops that candidate. Every settled
      record is a genealogy CANDIDATE; the pure sampler partitions promoted
      (the champion spine) from rejected (the inspiration pool).
    * ONE best-effort Elo read (:func:`zicato.index.query.elo_for_epoch`) for
      the greedy walk's tie-break; an absent index default-fills.

    The pure sampler (:mod:`zicato.proposer.genealogy`) then selects parents +
    the greedy max--min-Jaccard inspirations, banding every whole-candidate
    outcome and capping every diff excerpt — ENVELOPE-CLEAN by construction (no
    per-entry read happens here, so no entry id can leave). ANY exception
    anywhere → DEBUG log → ``()`` → a byte-identical round (genealogy must
    never fail a propose step).
    """
    quality = getattr(weights, "proposer_quality", None)
    k = int(getattr(quality, "genealogy", 0) or 0)
    if k <= 0:
        return ()
    try:
        from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
        from zicato.core.workspace import generations_dir  # noqa: PLC0415
        from zicato.epoch.journal import read_experiment  # noqa: PLC0415
        from zicato.proposer.genealogy import (  # noqa: PLC0415
            GenealogyRecord,
            sample_genealogy,
        )

        gens_root = generations_dir(workspace_root, epoch_id)
        if not gens_root.is_dir():
            return ()

        records: list[GenealogyRecord] = []
        for gen_dir in gens_root.iterdir():
            if not gen_dir.is_dir():
                continue
            gid = gen_dir.name
            # NB: the reigning champion (``parent_id``) is NOT skipped — the
            # pure sampler walks the champion's ``parent_generation_id`` chain
            # from ``champion_id``, so the champion's own promoted record is the
            # spine ANCHOR (the head of "the winning line to build on"). It only
            # ever surfaces as a PARENT (promoted → never the rejected
            # inspiration pool), so no anchor double-counts as an inspiration.
            try:
                exp = read_experiment(workspace_root, epoch_id, gid)
            except Exception as exc:  # noqa: BLE001 — unreadable record: skip
                log.debug("genealogy: record %s/%s unreadable (%s)", epoch_id, gid, exc)
                continue
            if exp.outcome is None:
                continue
            hyp = exp.hypothesis
            patch_text = "\n".join(p.new_content or "" for p in exp.patches)
            records.append(
                GenealogyRecord(
                    generation_id=exp.generation_id,
                    parent_generation_id=exp.parent_generation_id,
                    decision=exp.outcome.tournament_decision,
                    round_index=exp.round_index,
                    core_idea=hyp.core_idea,
                    patch_mutation_ids=frozenset(p.mutation_id for p in exp.patches),
                    patch_op_kinds=tuple(p.op for p in exp.patches),
                    patch_text=patch_text,
                    scalar_score_delta=exp.outcome.scalar_score_delta,
                    is_placebo=hyp.core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER),
                )
            )
        if not records:
            return ()  # no settled history yet — nothing to build a lineage from

        # ONE best-effort Elo read for the greedy walk's tie-break (fresh per
        # round — the fold runs at every ingest). Absent index → {}.
        elo_by_gid: dict[str, float] = {}
        try:
            from zicato.index.query import elo_for_epoch  # noqa: PLC0415

            for row in elo_for_epoch(_index_db_path(workspace_root), epoch_id):
                if row["elo"] is not None:
                    elo_by_gid[str(row["generation_id"])] = float(row["elo"])
        except Exception as exc:  # noqa: BLE001 — Elo is advisory tie-break material
            log.debug("genealogy: Elo read skipped (%s)", exc)

        # The pure sampler partitions promoted (spine) from rejected
        # (inspiration pool), re-caps the pool, and does the greedy walk.
        items = sample_genealogy(records, elo_by_gid, k, champion_id=parent_id)
        if items:
            log.debug(
                "genealogy: sampled %d item(s) (%d parent, %d inspiration)",
                len(items),
                sum(1 for it in items if it.kind == "parent"),
                sum(1 for it in items if it.kind == "inspiration"),
            )
        return items
    except Exception as exc:  # noqa: BLE001 — genealogy must never fail a round
        log.debug("genealogy: sampling skipped (%s)", exc)
        return ()


def _build_calibration_summary(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
) -> Any:
    """Summarize the reign's prediction calibration (WS-CAL), or ``None`` when OFF.

    ``None`` — the DEFAULT — unless the contract opts in with
    ``proposer_quality.calibration_feedback > 0``: the propose path then carries
    no summary at all and is byte-identical.

    The IO half of the critic-calibration channel, built ONCE per round beside
    the recombination + genealogy builders and threaded as plain DATA (a
    :class:`~zicato.proposer.calibration.CalibrationSummary` on
    :attr:`~zicato.proposer.agent.ProposerContext.calibration` — all best-of-N
    slots see the same summary). Two best-effort reads (the genealogy precedent
    — records + an advisory index read):

    * the current epoch's durable experiment RECORDS
      (:func:`zicato.epoch.journal.read_experiment`, GUARDED) for each settled
      hypothesis's ``core_idea`` + whole-candidate Δscalar + round + placebo
      flag;
    * the prediction-accuracy grader
      (:func:`zicato.tournament.detail.hypothesis_ledger`) for each hypothesis's
      ``(matches, predictions)`` COUNTS — the EXISTING ``/api/hypothesis-accuracy``
      feed, reused. An absent / unbuildable index default-fills every claim to
      ``predictions == 0`` (unresolved), so the sampler returns ``None`` (no
      graded history) and the round stays byte-identical.

    The pure sampler (:mod:`zicato.proposer.calibration`) then tallies the
    hit / miss / unresolved counts, the pooled ``hit / (hit + miss)`` fraction,
    and up to ``k`` recent graded claims — ENVELOPE-CLEAN by construction (the
    grader scores whole-candidate MOVEMENT aggregates, so no per-entry read
    happens and no entry id can leave). ANY exception anywhere → DEBUG log →
    ``None`` → a byte-identical round (calibration must never fail a propose
    step).
    """
    quality = getattr(weights, "proposer_quality", None)
    k = int(getattr(quality, "calibration_feedback", 0) or 0)
    if k <= 0:
        return None
    try:
        from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
        from zicato.core.workspace import generations_dir  # noqa: PLC0415
        from zicato.epoch.journal import read_experiment  # noqa: PLC0415
        from zicato.proposer.calibration import (  # noqa: PLC0415
            CalibrationClaim,
            sample_calibration,
        )

        gens_root = generations_dir(workspace_root, epoch_id)
        if not gens_root.is_dir():
            return None

        # ONE best-effort grader read — the reign's per-hypothesis
        # (matches, predictions), keyed by generation id. An absent / degraded
        # index leaves the map empty, so every claim grades unresolved and the
        # sampler omits the block (byte-identical).
        grades_by_gid: dict[str, tuple[int, int]] = {}
        try:
            from zicato.tournament.detail import hypothesis_ledger  # noqa: PLC0415

            for grade in hypothesis_ledger(_index_db_path(workspace_root), epoch_id):
                grades_by_gid[str(grade.generation_id)] = (grade.matches, grade.predictions)
        except Exception as exc:  # noqa: BLE001 — the grader is advisory here
            log.debug("calibration: prediction-accuracy read skipped (%s)", exc)

        claims: list[CalibrationClaim] = []
        for gen_dir in gens_root.iterdir():
            if not gen_dir.is_dir():
                continue
            gid = gen_dir.name
            try:
                exp = read_experiment(workspace_root, epoch_id, gid)
            except Exception as exc:  # noqa: BLE001 — unreadable record: skip
                log.debug("calibration: record %s/%s unreadable (%s)", epoch_id, gid, exc)
                continue
            if exp.outcome is None:
                continue  # only settled hypotheses can be graded
            matches, predictions = grades_by_gid.get(exp.generation_id, (0, 0))
            hyp = exp.hypothesis
            claims.append(
                CalibrationClaim(
                    generation_id=exp.generation_id,
                    round_index=exp.round_index,
                    core_idea=hyp.core_idea,
                    scalar_score_delta=exp.outcome.scalar_score_delta,
                    matches=int(matches),
                    predictions=int(predictions),
                    is_placebo=hyp.core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER),
                )
            )
        if not claims:
            return None

        summary = sample_calibration(claims, k)
        if summary is not None:
            log.debug(
                "calibration: %d hit / %d miss / %d unresolved (fraction %.2f)",
                summary.hit_count,
                summary.miss_count,
                summary.unresolved_count,
                summary.calibration_fraction,
            )
        return summary
    except Exception as exc:  # noqa: BLE001 — calibration must never fail a round
        log.debug("calibration: sampling skipped (%s)", exc)
        return None
