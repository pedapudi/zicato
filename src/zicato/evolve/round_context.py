"""Assemble proposal inputs from the round's accepted workspace records.

Candidate combination and proposal ancestry share one experiment walk and one
ranking query. Their selectors remain independent; only combination reads task
outcomes, and only training outcomes contribute to its counts. The proposer
receives the selected edits and summaries through its existing context.

Screening and critic calibration retain their own inputs. Disabled channels
perform no reads, and unavailable optional evidence does not stop a round.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Experiment, Generation
from zicato.evolve.ingest import _index_db_path
from zicato.evolve.lifecycle_services import _beat
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import WorkspaceLock
from zicato.workspace import WorkspaceLayout, generation_ids

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
    writer: WorkspaceLock | None = None,
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
            epoch_id=epoch_id,
            generation_id=parent_gen.id,
            round_index=round_index,
            phase=f"screening:r{round_index}",
        )
        return await run_candidate_screen(
            writer=writer,
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


def _build_candidate_history(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    train_entry_ids: frozenset[str],
    mutations: list[Any],
) -> tuple[Any, tuple[Any, ...]]:
    """Share experiment and ranking reads across the enabled proposal channels."""
    if not (
        (weights.experimental.recombine and weights.proposer_quality.best_of_n > 1)
        or weights.experimental.genealogy > 0
    ):
        return None, ()
    from zicato.epoch.journal import read_epoch_experiments  # noqa: PLC0415

    try:
        experiments, unreadable = read_epoch_experiments(workspace_root, epoch_id)
    except Exception as exc:  # noqa: BLE001 — optional history cannot stop a round
        log.debug("candidate history unavailable for %s (%s)", epoch_id, exc)
        return None, ()
    for reason in unreadable:
        log.debug("candidate history skipped a record: %s", reason)
    elo_by_gid: dict[str, float] = {}
    try:
        from zicato.index.query import elo_for_epoch  # noqa: PLC0415

        for row in elo_for_epoch(_index_db_path(workspace_root), epoch_id):
            if row["elo"] is not None:
                elo_by_gid[str(row["generation_id"])] = float(row["elo"])
    except Exception as exc:  # noqa: BLE001 — rankings are optional
        log.debug("candidate rankings unavailable for %s (%s)", epoch_id, exc)
    pair = _build_recombination_pair(
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        parent_id=parent_id,
        train_entry_ids=train_entry_ids,
        mutations=mutations,
        experiments=experiments,
        elo_by_gid=elo_by_gid,
    )
    k = weights.experimental.genealogy
    if k <= 0:
        return pair, ()
    try:
        from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
        from zicato.proposer.genealogy import (  # noqa: PLC0415
            GenealogyRecord,
            sample_genealogy,
        )

        records: list[GenealogyRecord] = []
        for _gid, exp in experiments:
            if exp.outcome is None or exp.outcome.tournament_decision is None:
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
            return pair, ()  # no settled history yet — nothing to build a lineage from

        items = sample_genealogy(records, elo_by_gid, k, champion_id=parent_id)
        if items:
            log.debug(
                "genealogy: sampled %d item(s) (%d parent, %d inspiration)",
                len(items),
                sum(1 for it in items if it.kind == "parent"),
                sum(1 for it in items if it.kind == "inspiration"),
            )
        return pair, items
    except Exception as exc:  # noqa: BLE001 — genealogy must never fail a round
        log.debug("genealogy: sampling skipped (%s)", exc)
        return pair, ()


def _build_recombination_pair(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    train_entry_ids: frozenset[str],
    mutations: list[Any],
    experiments: Sequence[tuple[str, Experiment]],
    elo_by_gid: dict[str, float],
) -> Any:
    """Select complementary rejected edits using shared history and training outcomes."""
    quality = weights.proposer_quality
    experimental = weights.experimental
    if (
        not experimental.recombine
        or quality.best_of_n <= 1
        or experimental.recombine_pool != "rejects"
    ):
        return None
    try:
        from zicato.epoch.recombine import (  # noqa: PLC0415
            RECOMBINE_POOL_MAX,
            eligible_parents,
            rank_pairs,
        )

        pool: list[Experiment] = []
        for gid, exp in reversed(experiments):
            # A prior mint is NEVER a pool candidate itself (predicate #4
            # would drop it later anyway) — skipping here keeps it from
            # wasting one of the RECOMBINE_POOL_MAX slots and a grid read.
            if gid == parent_id or len(exp.recombined_from) == 2:
                continue
            if exp.outcome is None or exp.outcome.tournament_decision != "rejected":
                continue
            if len(pool) < RECOMBINE_POOL_MAX:
                pool.append(exp)
        if len(pool) < 2:
            return None

        candidates = _parent_candidates(
            pool, workspace_root, epoch_id, parent_id, train_entry_ids, elo_by_gid
        )
        manifest_ids = frozenset(str(m.id) for m in mutations)
        eligible = eligible_parents(candidates, champion_id=parent_id, manifest_ids=manifest_ids)
        # The merge mode gates the disjointness predicate: "llm" relaxes #7
        # for pair selection so an OVERLAPPING pair (which only an LLM merge
        # can compose) is eligible, while "mechanical" (the default) keeps #7
        # hard and selects only disjoint pairs.
        pair = rank_pairs(
            eligible,
            tried_pairs=_tried_pairs(experiments, parent_id),
            merge_mode=experimental.recombine_merge,
        )
        return _recombination_pair_value(pair, pool)
    except Exception as exc:  # noqa: BLE001 — recombination must never fail a round
        log.debug("recombine: pair selection skipped (%s)", exc)
        return None


def _tried_pairs(
    experiments: Sequence[tuple[str, Experiment]], parent_id: str
) -> frozenset[frozenset[str]]:
    """Every pair a persisted mint other than the champion records (predicate #5)."""
    return frozenset(
        frozenset(exp.recombined_from)
        for gid, exp in experiments
        if gid != parent_id and len(exp.recombined_from) == 2
    )


def _parent_candidates(
    pool: Sequence[Experiment],
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    train_entry_ids: frozenset[str],
    elo_by_gid: dict[str, float],
) -> list[Any]:
    """Each pool experiment as selector data, with its TRAIN flips against the champion."""
    from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
    from zicato.epoch.recombine import ParentCandidate  # noqa: PLC0415
    from zicato.tournament.scoring import pass_flips  # noqa: PLC0415
    from zicato.workspace.reads import read_generation_losses  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace_root)
    parent_losses = read_generation_losses(layout, epoch_id, parent_id)
    candidates: list[ParentCandidate] = []
    for exp in pool:
        child_losses = read_generation_losses(layout, epoch_id, exp.generation_id)
        improved, regressed = pass_flips(
            {e: row.get("pass_fail") for e, row in parent_losses.items()},
            {e: row.get("pass_fail") for e, row in child_losses.items()},
            train_entry_ids,
        )
        hyp = exp.hypothesis
        candidates.append(
            ParentCandidate(
                generation_id=exp.generation_id,
                decision=exp.outcome.tournament_decision or "" if exp.outcome else "",
                parent_generation_id=exp.parent_generation_id,
                is_placebo=hyp.core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER),
                is_recombined=bool(exp.recombined_from),
                patch_mutation_ids=frozenset(p.mutation_id for p in exp.patches),
                improved_entry_ids=improved,
                regressed_entry_ids=regressed,
                elo=elo_by_gid.get(exp.generation_id),
                patches=exp.patches,
                core_idea=hyp.core_idea,
                expected_metric_movements=hyp.expected_metric_movements,
            )
        )
    return candidates


def _recombination_pair_value(pair: Any, pool: Sequence[Experiment]) -> Any:
    """The envelope-clean value the proposer stack receives for a selected pair."""
    if pair is None:
        return None
    from zicato.proposer.prompts import _bucket_scalar_delta  # noqa: PLC0415
    from zicato.proposer.recombine import RecombinationPair  # noqa: PLC0415

    a, b = pair
    log.debug(
        "recombine: selected pair (%s, %s) — coverage %d, cross-regression %d",
        a.generation_id,
        b.generation_id,
        len(a.improved_entry_ids | b.improved_entry_ids),
        len(a.regressed_entry_ids | b.regressed_entry_ids),
    )
    # The LLM merge prompt carries each parent's whole-candidate
    # BANDED outcome (envelope-clean — the exact Δscalar is bucketed HERE
    # and discarded, only the coarse label reaches the pair). Reuses the
    # experiment-memory band vocabulary; "" for an unsettled delta.
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
        a_expected_metric_movements=a.expected_metric_movements,
        b_expected_metric_movements=b.expected_metric_movements,
    )


async def _build_frontier_recombination_pair(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    train_entry_ids: frozenset[str],
    mutations: list[Any],
    confirm: Callable[[str], Awaitable[Any]],
) -> Any:
    """Select a recombination pair from confirmed Pareto frontier members.

    ``None`` unless the contract sets ``experimental.recombine`` with a slate
    and ``recombine_pool: "frontier"``. The pool is then the epoch's frontier
    members, never the rejects pool: with fewer than two usable members the
    round mints nothing.

    A member is usable when it passes the selector's per-candidate predicates
    — among them that it was proposed against the CURRENT champion, so its
    own tree is the champion's plus its patch, which is what the mint applies
    — and ``confirm`` — :func:`~zicato.evolve.pareto.confirm_frontier_member`
    bound to this round — upholds it against that champion. A pair needs
    disjoint win axes; pairs rank by the number of axes won between them,
    then by the selector's remaining keys (PROPOSER.md §2.6.2).

    Confirmation is spent on the best-ranked pair's members only, one at a
    time, re-ranking after each result, and at most
    :data:`~zicato.epoch.recombine.RECOMBINE_CONFIRM_MAX` times a round.
    """
    experimental = weights.experimental
    if (
        not experimental.recombine
        or weights.proposer_quality.best_of_n <= 1
        or experimental.recombine_pool != "frontier"
    ):
        return None
    try:
        from zicato.epoch.journal import read_epoch_experiments  # noqa: PLC0415
        from zicato.epoch.pareto import load_frontier  # noqa: PLC0415
        from zicato.epoch.recombine import (  # noqa: PLC0415
            RECOMBINE_CONFIRM_MAX,
            eligible_parents,
            rank_pairs,
        )

        experiments, _unreadable = read_epoch_experiments(workspace_root, epoch_id)
        exp_by_gid = dict(experiments)
        members = {m.generation_id: m for m in load_frontier(workspace_root, epoch_id).members}
        pool = [exp_by_gid[gid] for gid in sorted(members) if gid in exp_by_gid]
        eligible = eligible_parents(
            _parent_candidates(pool, workspace_root, epoch_id, parent_id, train_entry_ids, {}),
            champion_id=parent_id,
            manifest_ids=frozenset(str(m.id) for m in mutations),
        )
        wins = {gid: frozenset(m.beats_champion_on) for gid, m in members.items()}
        confirmed = {
            gid
            for gid, m in members.items()
            if m.confirmed_round is not None and m.champion_generation_id == parent_id
        }
        tried = _tried_pairs(experiments, parent_id)
        spent = 0
        while True:
            pair = rank_pairs(
                eligible, tried_pairs=tried, merge_mode=experimental.recombine_merge, wins=wins
            )
            pending = [c.generation_id for c in pair or () if c.generation_id not in confirmed]
            if pair is None or (pending and spent >= RECOMBINE_CONFIRM_MAX):
                note = "recombine: no confirmed frontier pair (%d member(s), %d confirmation(s))"
                log.debug(
                    note + "; minting nothing, the rejects pool is not used", len(members), spent
                )
                return None
            if not pending:
                return _recombination_pair_value(pair, pool)
            spent += 1
            member = await confirm(pending[0])
            if member is None:
                eligible = [c for c in eligible if c.generation_id != pending[0]]
            else:
                confirmed.add(pending[0])
                wins[pending[0]] = frozenset(member.beats_champion_on)
    except Exception as exc:  # noqa: BLE001 — recombination must never fail a round
        log.debug("recombine: frontier pair selection skipped (%s)", exc)
        return None


def _build_calibration_summary(
    *,
    weights: Any,
    workspace_root: Path,
    epoch_id: str,
) -> Any:
    """Summarize the reign's prediction calibration, or ``None`` when OFF.

    ``None`` — the DEFAULT — unless the contract opts in with
    ``experimental.calibration_feedback > 0``: the propose path then carries
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
      ``(matches, predictions)`` COUNTS — the candidate dossier's ``hypothesis_accuracy``
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
    quality = getattr(weights, "experimental", None)
    k = int(getattr(quality, "calibration_feedback", 0) or 0)
    if k <= 0:
        return None
    try:
        from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER  # noqa: PLC0415
        from zicato.epoch.journal import read_experiment  # noqa: PLC0415
        from zicato.proposer.calibration import (  # noqa: PLC0415
            CalibrationClaim,
            sample_calibration,
        )

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
        for gid in generation_ids(WorkspaceLayout.from_root(workspace_root), epoch_id):
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
