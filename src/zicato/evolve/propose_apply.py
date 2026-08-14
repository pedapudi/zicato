"""Round-pipeline **propose/apply** stage — mint one challenger, admit it.

Split out of :mod:`zicato.orchestrator` as the FINAL leg of the Finding-2 typed
round-pipeline decomposition (``docs/design/REIMPLEMENTATION.md``). This is the
pipeline's *propose → apply → admit* seam: the helpers that take a champion and
a mutation surface and produce ONE applied challenger generation, plus the pure
accept/soft-reject verdict that decides whether that challenger joins the
multi-challenger field:

* :func:`_propose_child` — build the :class:`ProposerContext` and propose one
  child (the single propose shape the gauntlet and field paths share);
* :func:`_propose_and_apply_challenger` — the field path's propose →
  post-apply-validate → derive → persist pipeline for one challenger, returning
  the applied child paired with its dashboard field-status record;
* :func:`_mint_placebo_challenger` + :func:`_maybe_run_placebo_arm_gauntlet` —
  the OVERFITTING.md #7 random-baseline placebo arm (derive + the opt-in duel);
* :class:`_AppliedChallenger` — the applied-child record both mint paths return;
* :func:`_trim_reason` / :func:`_short_reject_reason` — the tracker-reason
  string helpers the field-status records render;
* :func:`_mint_challenger_field` (+ :class:`_FieldMintDecision`) and its pure
  companions :func:`_diversity_signature`,
  :func:`_duplicates_inflight_sibling`, :func:`_max_overlap_with_accepted` —
  the field-diversity accept / soft-reject DECISION separated from its
  persistence I/O (which stays in the orchestrator's multi-challenger loop).

Four names are referenced from OUTSIDE :mod:`zicato.orchestrator`
(``_propose_and_apply_challenger`` from the multi-challenger test,
``_mint_challenger_field`` from the decomposition test, ``_diversity_signature``
from the best-of-N tree-integrity test, ``_max_overlap_with_accepted`` from the
prior-experiments test), so the orchestrator re-imports the names its own body
still calls and lists those four in ``__all__`` for mypy's
no-implicit-reexport. Stable collaborators (``ingest``'s index/track-record IO,
``lifecycle_services._beat`` / ``_now_iso``, the shared ``round`` validators,
``selection.diversity.jaccard``) are direct top-level imports; the heavier
``epoch`` / ``proposer`` / ``tournament`` / ``runtime`` siblings stay lazy
call-time imports exactly as they were inline, and the one orchestrator
generation-number helper is resolved
through the orchestrator module object at CALL time (the established
``zicato.evolve.*`` idiom). The module logger keeps the ``zicato.orchestrator``
name so records stay byte-identical.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.experiment import PriorExperiment
from zicato.core.types import Experiment, Generation, OutcomeRecord
from zicato.evolve.ingest import _ingest_experiment_into_index, _load_mutation_track_records
from zicato.evolve.lifecycle_services import _beat, _now_iso
from zicato.evolve.round import (
    build_post_apply_validator,
    build_scratch_validator_factory,
    check_patch_manifest_and_forbidden,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.selection.diversity import jaccard
from zicato.util import best_effort

if TYPE_CHECKING:
    from zicato.orchestrator import CallLLM, _RoundLogEmitter
    from zicato.proposer.agent import ProposerAgent
    from zicato.proposer.best_of_n import ScreenRunner

log = logging.getLogger("zicato.orchestrator")


@dataclass(frozen=True, slots=True)
class _AppliedChallenger:
    """One proposed-and-applied challenger generation in the field.

    Pairs the freshly-minted child generation id with the validated child
    snapshot, the proposer's :class:`Experiment`, and the generation
    record the runner mounts. ``snapshot_root`` is the tree
    ``run_matchup`` evaluates; ``experiment`` is persisted to
    ``experiment.json`` so the journal/index carry the proposer's
    hypothesis and patches exactly as the gauntlet path does.
    """

    generation_id: str
    snapshot_root: Path
    experiment: Experiment
    generation: Generation


def _trim_reason(reason: str, limit: int = 200) -> str:
    """Collapse a reason string to one whitespace-normalised tracker line.

    Shared by :func:`_short_reject_reason` (final-outcome one-liner) and the
    per-attempt ``attempt_reasons`` list the proposing tracker renders so a
    ``file_findability``-style validation message stays legible. Collapses
    internal newlines/runs of whitespace and truncates to ``limit`` chars
    with a trailing ellipsis. Empty/whitespace input ⇒ empty string.
    """
    s = " ".join(str(reason).split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _short_reject_reason(attempts: list[str]) -> str:
    """Condense a proposer's attempt log into one short tracker reason.

    The proposer records one message per failed attempt (empty response /
    invalid JSON / post-apply validation / mutation_id no longer resolves
    / forbidden-id violation). For the dashboard's proposing-step tracker
    we want a single short string, so take the LAST attempt's message
    (the final reason the proposer gave up) and trim it to a hovercard
    length. Empty list ⇒ empty string (caller falls back to ``str(exc)``).
    """
    if not attempts:
        return ""
    return _trim_reason(attempts[-1], limit=160)


async def _propose_child(
    *,
    proposer_agent: ProposerAgent,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    patterns: Any,
    mutations: Any,
    brief: Any,
    loss_summary: str,
    auxiliary_call_llm: CallLLM,
    auxiliary_model: str,
    max_proposer_retries: int,
    workspace_root: Path,
    #: The PARENT generation's materialised snapshot — the tree this round
    #: is about to patch. Threaded onto :class:`ProposerContext` so a
    #: tool-using proposer reaches it without re-deriving the store's path
    #: convention. REQUIRED (no default) on purpose: both call sites have a
    #: ``genstore`` in hand, and a defaulted ``None`` would let a future
    #: caller silently reintroduce the derivation this exists to remove.
    generation_root: Path,
    validate_experiment: Any,
    meta_loop_emitter: Any,
    custom_judge_names: frozenset[str],
    prior_experiments: tuple[PriorExperiment, ...],
    restrict_visibility: bool,
    failure_profile: str,
    round_index: int,
    process_exemplars: str = "",
    genealogy: tuple[Any, ...] = (),
    calibration: Any = None,
    round_emitter: _RoundLogEmitter | None = None,
    screen_candidates: ScreenRunner | None = None,
    recombine_pair: Any = None,
    scratch_validator_factory: Any = None,
) -> Experiment:
    """Build the :class:`ProposerContext` + propose ONE child of the champion.

    The single propose shape both pipelines share — the gauntlet's inline
    propose block and :func:`_propose_and_apply_challenger` previously built
    near-identical contexts, so a new ``ProposerContext`` field could land on
    one path only. Raises :class:`~zicato.proposer.proposer.ProposerError`
    exactly as the inner agent does (callers own the rejected-outcome /
    narrower-field handling).

    The returned experiment carries the EVOLVE ``round_index`` stamped on —
    the authoritative birth round the dashboard's round-grouping and the
    journal read (issue #16); the proposer's default is round 0.

    ``round_emitter`` (WS8, best-effort) traces the propose step onto the
    round's durable event log: the emitter callable rides
    ``ProposerContext.round_event_emitter`` so the best-of-N wrapper can
    emit ``candidate_sampled`` / ``critique_selected`` without importing the
    log module. On success one ``proposal_attempted`` (empty errors) plus
    ``experiment_minted`` + ``patches_applied`` are emitted (a success after
    internal retries records one settled attempt — per-attempt fidelity
    lives in the failure path, where ``ProposerError.attempts`` carries the
    full trail and one event per failed attempt is emitted).
    """
    from zicato.proposer.agent import ProposerContext  # noqa: PLC0415
    from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

    # The mutation-point fertility map — best-effort, {} on any failure
    # (which renders a byte-identical manifest). Settled experiments only
    # change between rounds, so every challenger in a round sees the same
    # records.
    mutation_track_records = _load_mutation_track_records(workspace_root, epoch_id)

    from zicato.telemetry.meta_loop import SPAN_PHASE, meta_span  # noqa: PLC0415

    try:
        # The propose phase span frames this challenger's slate (its slate-slot
        # spans nest under it) and the proposer LLM call (HARMONOGRAF.md §7).
        async with meta_span("propose", kind=SPAN_PHASE, meta={"generation_id": next_id}):
            experiment = await proposer_agent.propose(
                ProposerContext(
                    epoch_id=epoch_id,
                    parent_generation_id=parent_id,
                    new_generation_id=next_id,
                    patterns=tuple(patterns),
                    mutations=tuple(mutations),
                    brief_text=brief.text,
                    current_loss_summary=loss_summary,
                    aux_call_llm=auxiliary_call_llm,
                    model=auxiliary_model,
                    max_retries=max_proposer_retries,
                    forbidden_ids=brief.forbidden_ids,
                    workspace_root=workspace_root,
                    generation_root=generation_root,
                    validate_experiment=validate_experiment,
                    meta_loop_emitter=meta_loop_emitter,
                    custom_judge_names=custom_judge_names,
                    prior_experiments=prior_experiments,
                    restrict_visibility=restrict_visibility,
                    failure_profile=failure_profile,
                    process_exemplars=process_exemplars,
                    genealogy=genealogy,
                    calibration=calibration,
                    mutation_track_records=mutation_track_records,
                    round_event_emitter=(round_emitter.emit if round_emitter is not None else None),
                    screen_candidates=screen_candidates,
                    recombine_pair=recombine_pair,
                    scratch_validator_factory=scratch_validator_factory,
                )
            )
    except ProposerError as exc:
        if round_emitter is not None:
            for attempt_error in exc.attempts:
                round_emitter.emit("proposal_attempted", {"errors": (str(attempt_error),)})
        raise
    if round_emitter is not None:
        round_emitter.emit("proposal_attempted", {})
        round_emitter.emit("experiment_minted", {"experiment_id": experiment.id})
        # The proposer's validate hook derived + validated the child tree
        # before a successful return, so the patches are applied by here.
        round_emitter.emit("patches_applied", {"generation_id": next_id})
    return replace(experiment, round_index=round_index)


async def _propose_and_apply_challenger(
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    mutations: list[Any],
    patterns: list[Any],
    brief: Any,
    loss_summary: str,
    auxiliary_call_llm: CallLLM,
    auxiliary_model: str,
    max_proposer_retries: int,
    beater: HeartbeatBeater | None,
    round_index: int,
    meta_loop_emitter: Any,
    seed: int,
    proposer_agent: ProposerAgent,
    custom_judge_names: frozenset[str] = frozenset(),
    prior_experiments: tuple[PriorExperiment, ...] = (),
    restrict_visibility: bool = False,
    failure_profile: str = "",
    process_exemplars: str = "",
    genealogy: tuple[Any, ...] = (),
    calibration: Any = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    round_emitter: _RoundLogEmitter | None = None,
    screen_candidates: ScreenRunner | None = None,
    recombine_pair: Any = None,
) -> tuple[_AppliedChallenger | None, dict[str, Any]]:
    """Propose + apply ONE challenger child of the champion.

    Reuses the same propose → post-apply-validate → derive pipeline the
    gauntlet path uses for its single challenger (the proposer's
    ``validate_experiment`` hook applies the patch set into a fresh child
    snapshot and runs ``validate_post_apply``), so a challenger in the
    field is a real lineage child of the current champion. Returns the
    applied challenger (or ``None`` when the proposer exhausted its retry
    budget or the patch set failed post-apply validation — in which case
    the round simply runs a narrower field rather than crashing; the
    SelectionStrategy still resolves over whatever applied cleanly) PAIRED
    with a structured **field-status** record for the dashboard's
    proposing-step tracker: ``{generation_id, status: "applied" |
    "rejected", reason, attempts, attempt_reasons, hypothesis, seed}``. The
    ``reason`` is the same short string the evolve log carries (empty
    response / invalid JSON / post-apply validation / mutation_id no longer
    resolves); ``attempt_reasons`` is the FULL per-attempt failure list off
    :attr:`ProposerError.attempts` (so a ``file_findability``-style
    validation rejection is plainly visible on the dashboard, not just
    condensed to one line); ``attempts`` is its length; ``hypothesis`` is
    the one-line core idea when the challenger applies cleanly. A rejected
    challenger is thus legible on the dashboard rather than silently
    dropped.

    ``on_status`` — when supplied — is invoked with a ``"proposing"``-status
    record BEFORE the proposer LLM call begins, so the dashboard's
    proposing tracker shows each challenger slot enter the field LIVE (not
    only after the whole batch is minted). It is best-effort; a raising
    callback never aborts the proposal. The same callback is the seam the
    caller uses to re-publish the live envelope as each slot resolves.

    ``prior_experiments`` is the caller-assembled experiment-memory digest
    threaded into the inner :func:`propose_experiment` call — the settled
    cross-round history plus this round's already-minted in-flight
    siblings — so each challenger diversifies away from both known
    failures and its just-proposed cohort. Empty by default.

    ``proposer_agent`` is the epoch's resolved :class:`ProposerAgent`
    (built once per evolve invocation from the frozen ``proposer_path``).
    Each challenger is proposed through it, so a configured proposer's
    skills shape every challenger in the field exactly as they shape the
    gauntlet's single challenger.
    """
    from zicato.epoch import append_to_lineage, write_experiment  # noqa: PLC0415
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415
    from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

    genstore = default_generation_store(workspace_root)
    last_child_snapshot: dict[str, Path] = {}

    def _emit_status(record: dict[str, Any]) -> None:
        """Best-effort live publish of one challenger's proposal record."""
        if on_status is None:
            return
        with best_effort(
            "proposal-status publish",
            on_error=lambda exc: log.debug("proposal-status publish skipped: %s", exc),
        ):
            on_status(record)

    from zicato.runtime import progress_log  # noqa: PLC0415

    _beat(
        beater,
        workspace_root=workspace_root,
        progress=progress_log.PROPOSE,
        epoch_id=epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"proposing:round_{round_index}:{next_id}",
    )
    # Surface the slot entering the field LIVE — before the proposer LLM
    # call begins — so the dashboard's proposing tracker shows the
    # challenger "proposing…" rather than appearing only once it settles.
    _emit_status(
        {
            "generation_id": next_id,
            "status": "proposing",
            "reason": "",
            "attempts": 0,
            "attempt_reasons": [],
            "hypothesis": "",
            "seed": seed,
        }
    )

    # The post-apply validation hook — the SAME shared
    # ``build_post_apply_validator`` (``zicato.evolve.round``) the gauntlet
    # path uses; the previously-inlined closure was byte-identical.
    _validate = build_post_apply_validator(
        genstore=genstore,
        epoch_id=epoch_id,
        parent_id=parent_id,
        next_id=next_id,
        mutations=mutations,
        beater=beater,
        round_index=round_index,
        last_child_snapshot=last_child_snapshot,
    )
    # WS-CONC: per-slot scratch-validator factory (see the gauntlet path). The
    # field proposes challengers sequentially — sibling-conditioning is an
    # intentional diversity property — but each challenger's best-of-N slate
    # still gathers internally, so every slate slot needs its own scratch tree.
    _scratch_validator_factory = build_scratch_validator_factory(
        genstore=genstore,
        epoch_id=epoch_id,
        parent_id=parent_id,
        next_id=next_id,
        mutations=mutations,
        beater=beater,
        round_index=round_index,
    )

    try:
        experiment = await _propose_child(
            proposer_agent=proposer_agent,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            generation_root=genstore.snapshot_root(epoch_id, parent_id),
            patterns=patterns,
            mutations=mutations,
            brief=brief,
            loss_summary=loss_summary,
            auxiliary_call_llm=auxiliary_call_llm,
            auxiliary_model=auxiliary_model,
            max_proposer_retries=max_proposer_retries,
            workspace_root=workspace_root,
            validate_experiment=_validate,
            meta_loop_emitter=meta_loop_emitter,
            custom_judge_names=custom_judge_names,
            prior_experiments=prior_experiments,
            restrict_visibility=restrict_visibility,
            failure_profile=failure_profile,
            process_exemplars=process_exemplars,
            genealogy=genealogy,
            calibration=calibration,
            round_index=round_index,
            round_emitter=round_emitter,
            screen_candidates=screen_candidates,
            recombine_pair=recombine_pair,
            scratch_validator_factory=_scratch_validator_factory,
        )
    except ProposerError as exc:
        reason = _short_reject_reason(exc.attempts) or str(exc)
        log.warning(
            "multi-challenger field: proposer could not produce a valid "
            "challenger for %s/%s (%s); the field runs without it",
            epoch_id,
            next_id,
            "; ".join(exc.attempts) or exc,
        )
        # Carry the FULL per-attempt failure list (parse error / validation
        # error WITH its exact message / post-apply error) so the dashboard
        # can render every retry's specific reason, not just the condensed
        # one-liner. Each entry is trimmed for legibility but kept distinct.
        attempt_reasons = [_trim_reason(a) for a in exc.attempts]
        rejected = {
            "generation_id": next_id,
            "status": "rejected",
            "reason": reason,
            "attempts": len(exc.attempts),
            "attempt_reasons": attempt_reasons,
            "hypothesis": "",
            "seed": seed,
        }
        _emit_status(rejected)
        return None, rejected

    check_patch_manifest_and_forbidden(experiment, mutations, brief.forbidden_ids)

    child_snapshot = last_child_snapshot["path"]
    # ``_propose_child`` already stamped the EVOLVE round onto the
    # experiment (round_index is the authoritative birth round the
    # dashboard's round-grouping reads — issue #16).
    # Persist the proposer-side experiment.json (outcome still None) and
    # fold it into the live index, exactly as the gauntlet path does for
    # its single challenger before the tournament finishes.
    write_experiment(workspace_root, epoch_id, next_id, experiment)
    _ingest_experiment_into_index(workspace_root, epoch_id, next_id)

    child_gen = Generation(
        id=next_id,
        epoch_id=epoch_id,
        parent_id=parent_id,
        snapshot_root=child_snapshot,
        created_at=_now_iso(),
        round_index=round_index,
    )
    # Append the challenger to lineage.json AT CREATION with its birth
    # round_index — not only at round settle. Every queryable store
    # (lineage.json, the index, CLI status, external tooling) must reflect
    # the in-flight round continuously, not just the last settled one
    # (issue #16). The settle-time append_to_lineage upserts the same node
    # to its final promoted/rejected state; append_to_lineage is an
    # idempotent update-in-place that preserves round_index, so the
    # creation-time write and the settle-time write compose cleanly.
    #
    # The creation-time write is PENDING (promoted=null), NOT a dead branch
    # (promoted=False). The challenger has applied a snapshot but has not
    # been crowned or cut — it is still racing. ``promoted=False`` reads as
    # REJECTED, so a False default would render an in-flight racer as a dead
    # branch on /api/lineage while it is mid-tournament. Pending → null →
    # the dashboard maps it to "racing"; the settle-time append flips it to
    # the resolved bool.
    append_to_lineage(workspace_root, epoch_id, child_gen, parent_id=parent_id, pending=True)
    applied_status = {
        "generation_id": next_id,
        "status": "applied",
        "reason": "",
        "attempts": 1,
        "attempt_reasons": [],
        # The proposer's hypothesis summary so the dashboard reads WHAT a
        # successful challenger proposes, not just that it applied. Trimmed
        # to a tracker-friendly one line.
        "hypothesis": _trim_reason(experiment.hypothesis.core_idea),
        "seed": seed,
    }
    _emit_status(applied_status)
    return (
        _AppliedChallenger(
            generation_id=next_id,
            snapshot_root=child_snapshot,
            experiment=experiment,
            generation=child_gen,
        ),
        applied_status,
    )


def _mint_placebo_challenger(
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    point: Any,
    round_index: int,
) -> _AppliedChallenger:
    """Derive + persist the random-baseline placebo challenger.

    The same derive → ``experiment.json`` → lineage pipeline every real
    challenger goes through (:mod:`zicato.evolve.placebo` builds the
    marked hypothesis + the semantics-preserving no-op patch), so the
    placebo is a genuine lineage child with a genuine snapshot — the gate
    scores it exactly like any challenger. Shared by the gauntlet's extra
    scheduled duel and the multi-challenger field's extra slot.
    """
    from zicato.epoch import append_to_lineage, write_experiment  # noqa: PLC0415
    from zicato.evolve.placebo import (  # noqa: PLC0415
        build_placebo_experiment,
        derive_placebo_snapshot,
    )

    experiment = build_placebo_experiment(
        epoch_id=epoch_id,
        generation_id=next_id,
        parent_id=parent_id,
        point=point,
        round_index=round_index,
    )
    child_snapshot = derive_placebo_snapshot(
        workspace_root,
        epoch_id=epoch_id,
        parent_id=parent_id,
        generation_id=next_id,
        patches=experiment.patches,
    )
    write_experiment(workspace_root, epoch_id, next_id, experiment)
    _ingest_experiment_into_index(workspace_root, epoch_id, next_id)
    child_gen = Generation(
        id=next_id,
        epoch_id=epoch_id,
        parent_id=parent_id,
        snapshot_root=child_snapshot,
        created_at=_now_iso(),
        round_index=round_index,
    )
    append_to_lineage(workspace_root, epoch_id, child_gen, parent_id=parent_id, pending=True)
    return _AppliedChallenger(
        generation_id=next_id,
        snapshot_root=child_snapshot,
        experiment=experiment,
        generation=child_gen,
    )


async def _maybe_run_placebo_arm_gauntlet(
    *,
    workspace_root: Path,
    epoch_id: str,
    adapter: Any,
    parent_gen: Generation,
    parent_id: str,
    round_id: str,
    mutations: list[Any],
    board: list[Any],
    weights: Any,
    config: Any,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    fast_mode: bool,
    round_index: int,
    total_rounds: int,
) -> None:
    """Run the opt-in placebo duel after a settled gauntlet round.

    OVERFITTING.md #7 on the single-challenger path: when the contract
    sets ``overfitting.random_baseline_every_n`` and this round's
    epoch-cumulative number is a cadence tick, one EXTRA scheduled duel
    runs after the round — champion vs a semantics-preserving no-op copy
    of itself (id ``{vN}-placebo``, deliberately non-``vN`` so round
    numbering / id minting are untouched). The duel goes through the
    unchanged runner + gate; its outcome persists to ``experiment.json``
    (before the round's health assessment reads it) and to lineage as a
    dead branch — the placebo NEVER advances the champion pointer, even
    on the alarm outcome. A promoted placebo surfaces as the CRITICAL
    ``placebo_promoted`` loop-health finding. Best-effort by contract:
    any failure here never aborts the round.
    """
    from zicato.evolve.generation_phase import round_number  # noqa: PLC0415
    from zicato.evolve.placebo import placebo_round_due  # noqa: PLC0415

    every_n = int(getattr(weights.overfitting, "random_baseline_every_n", 0))
    round_n = round_number(round_id)
    if not placebo_round_due(every_n, round_n) or not mutations:
        return

    from zicato.epoch import append_to_lineage, update_experiment_outcome  # noqa: PLC0415
    from zicato.tournament.runner import run_matchup  # noqa: PLC0415

    placebo_id = f"{round_id}-placebo"
    with best_effort(
        "random-baseline placebo arm",
        on_error=lambda exc: log.warning("random-baseline placebo arm skipped: %s", exc),
    ):
        challenger = _mint_placebo_challenger(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=placebo_id,
            point=mutations[0],
            round_index=round_index,
        )
        result = await run_matchup(
            adapter=adapter,
            left_gen=parent_gen,
            right_gen=challenger.generation,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast_mode,
            round_index=round_index,
            total_rounds=total_rounds,
            match_id=f"placebo:{placebo_id}",
        )
        decision = result.outcome.decision
        promoted = str(decision) == "promoted"
        update_experiment_outcome(
            workspace_root,
            epoch_id,
            placebo_id,
            OutcomeRecord(
                ran_at=_now_iso(),
                drift_movements=(),
                pass_rate_delta=result.outcome.delta_pass_rate,
                drift_loss_delta=0.0,
                scalar_score_delta=result.outcome.delta_scalar,
                tournament_decision=decision,
                rejection_reason="" if promoted else result.outcome.reason,
            ),
        )
        _ingest_experiment_into_index(workspace_root, epoch_id, placebo_id)
        # Lineage: ALWAYS a dead branch. Even a (pathological) promoted
        # verdict never advances the champion pointer — the arm measures
        # the gate; the alarm is the health finding, not a crowning.
        append_to_lineage(
            workspace_root,
            epoch_id,
            replace(challenger.generation, promoted=False),
            parent_id=parent_id,
        )
        if promoted:
            log.warning(
                "random-baseline placebo %s was PROMOTED by the gate "
                "(Δscalar=%.6g) — the decision procedure is promoting noise; "
                "the CRITICAL placebo_promoted health finding will fire. The "
                "champion pointer was NOT advanced.",
                placebo_id,
                result.outcome.delta_scalar,
            )
        else:
            log.info(
                "random-baseline placebo %s rejected as expected (%s) — gate "
                "discrimination confirmed this cadence tick",
                placebo_id,
                result.outcome.reason,
            )


def _diversity_signature(experiment: Experiment) -> tuple[frozenset[str], str]:
    """The field-diversity signature of a challenger: (modulating set, core idea).

    Two siblings in the same field COLLAPSE the field when they propose the
    same mutation (EXPERIMENT-MEMORY.md §2.2): a field of N becomes fewer than
    N distinct experiments, wasting tournament compute on duplicates. The
    signature is the *declared* targeted mutation-point id-SET (order-
    insensitive) joined with a whitespace/-case-normalized core idea, so a
    duplicate is detected by what the hypothesis TOUCHES and SAYS, not by
    incidental ordering or capitalisation.
    """
    ids = frozenset(experiment.hypothesis.modulating)
    core = " ".join(experiment.hypothesis.core_idea.split()).casefold()
    return ids, core


def _duplicates_inflight_sibling(
    experiment: Experiment, sibling_signatures: list[tuple[frozenset[str], str]]
) -> bool:
    """Whether a challenger duplicates an already-minted in-flight sibling.

    The field-diversity constraint (FUNCTIONALITY-RECOMMENDATIONS.md §4.3): a
    challenger whose ``modulating`` id-set AND core idea both match a sibling
    already minted THIS round is a duplicate that collapses the field, so the
    caller soft-rejects it. A challenger that touches the same ids but with a
    genuinely different idea (or the same idea on different ids) is NOT a
    duplicate — it is a legitimately distinct experiment and is kept. An empty
    ``modulating`` set never collapses the field (there is nothing to
    duplicate), so it is never rejected on this basis.
    """
    ids, core = _diversity_signature(experiment)
    if not ids:
        return False
    return any(ids == s_ids and core == s_core for s_ids, s_core in sibling_signatures)


def _max_overlap_with_accepted(
    candidate: frozenset[str], accepted: list[frozenset[str]]
) -> tuple[float, int]:
    """The largest Jaccard overlap of ``candidate`` against the accepted set.

    Returns ``(max_overlap, index)`` where ``index`` is the position of the
    accepted sibling that overlaps the most (``-1`` when ``accepted`` is
    empty). Used by the multi-challenger field-diversity enforcement to
    decide whether a freshly-applied challenger overlaps an already-accepted
    sibling beyond the operator's ``diversity_tolerance``.
    """
    best = 0.0
    best_idx = -1
    for idx, prior in enumerate(accepted):
        score = jaccard(candidate, prior)
        if score > best:
            best = score
            best_idx = idx
    return best, best_idx


@dataclass(frozen=True, slots=True)
class _FieldMintDecision:
    """The pure accept/soft-reject verdict for one proposed field slot.

    ``action`` is one of ``"accept"`` (the challenger joins the run slate),
    ``"reject_duplicate"`` (exact in-flight duplicate — same modulating
    id-set + core idea as a minted sibling), or ``"reject_overlap"`` (the
    opt-in Jaccard-overlap ceiling fired; ``overlap`` /
    ``overlap_peer_index`` locate the most-overlapping ACCEPTED sibling).
    """

    action: str
    overlap: float = 0.0
    overlap_peer_index: int = -1


def _mint_challenger_field(
    experiment: Experiment,
    sibling_signatures: list[tuple[frozenset[str], str]],
    accepted_mutation_sets: list[frozenset[str]],
    diversity_tolerance: float | None,
) -> _FieldMintDecision:
    """Decide whether a freshly-applied challenger joins the field — PURE.

    The field-diversity DECISION separated from its persistence I/O
    (FUNCTIONALITY-RECOMMENDATIONS.md §4.3 / EXPERIMENT-MEMORY.md §2.2), so
    the previously e2e-only branches are unit-testable:

    1. An exact duplicate of an already-minted in-flight sibling (same
       modulating id-set AND core idea) collapses the field ⇒ soft-reject.
    2. With a configured ``diversity_tolerance``, a non-empty mutation-id
       set whose Jaccard overlap with an already-ACCEPTED sibling's exceeds
       the tolerance ⇒ soft-reject. ``None`` tolerance skips this check
       entirely (the default-off path).
    3. Otherwise: accept.
    """
    if _duplicates_inflight_sibling(experiment, sibling_signatures):
        return _FieldMintDecision(action="reject_duplicate")
    if diversity_tolerance is not None:
        cand_ids = frozenset(experiment.hypothesis.modulating)
        overlap, peer_idx = _max_overlap_with_accepted(cand_ids, accepted_mutation_sets)
        if cand_ids and overlap > diversity_tolerance:
            return _FieldMintDecision(
                action="reject_overlap", overlap=overlap, overlap_peer_index=peer_idx
            )
    return _FieldMintDecision(action="accept")
