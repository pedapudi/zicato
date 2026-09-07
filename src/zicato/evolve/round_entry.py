"""Prepare one evolve round before structure-independent evaluation."""

# ruff: noqa: E402
from __future__ import annotations

import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.core.types import (
    Generation,
)
from zicato.evolve import generation_phase
from zicato.evolve.invocation import InvocationContext, validated_invocation
from zicato.evolve.lifecycle_services import (
    _now_iso,
)
from zicato.evolve.persist import (
    _skipped_round_outcome,
)
from zicato.evolve.round_context import (
    _build_calibration_summary,
    _build_candidate_screen_runner,
    _build_genealogy_items,
    _build_recombination_pair,
)
from zicato.runtime.control_consumer import (
    claim_skip_round,
)
from zicato.runtime.effective_settings import (
    TOURNAMENT_REPLICATES_KEY,
    effective_settings,
    recorded_setting,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import validate_workspace_lock
from zicato.runtime.resume import ResumePlan
from zicato.util.best_effort import best_effort

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

from zicato.evolve.decision_support import (
    _build_events_paths,
    _load_parent_losses,
    _render_failure_profile,
    _render_loss_summary,
    _render_process_exemplars_block,
    build_metric_priorities,
)
from zicato.evolve.round_api import EvolveRoundOutcome, _declared_custom_judge_names
from zicato.evolve.round_baseline import (
    _dump_mutations_snapshot,
    _ensure_baseline_snapshot,
)
from zicato.evolve.round_prepare import (
    _maybe_calibrate_noise_floor,
    _maybe_contract_preflight,
    _resolve_replicates_in_effect,
    _warn_margin_below_noise_floor,
)
from zicato.evolve.round_reporting import (
    _RoundLogEmitter,
)


async def evolve_once(
    *,
    workspace_root: Path,
    epoch_id: str | None = None,
    target_call_llm: CallLLM | None = None,
    evaluation_call_llm: CallLLM | None = None,
    instance_id: str = "default",
    fast_mode: bool = False,
    max_proposer_retries: int = 2,
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
    total_rounds: int = 0,
    meta_loop_emitter: Any = None,
    resume_plan: ResumePlan | None = None,
) -> EvolveRoundOutcome:
    """Run one round while owning the workspace from validation through teardown.

    An explicit epoch selects the evaluation contract. Callables override the
    configured target and evaluation roles. A completed or rejected round returns
    an outcome; unrecoverable workspace, adapter and persistence failures propagate.

    A resume plan reuses an interrupted generation's recorded experiment and
    completed measurements. The heartbeat and round counters let an embedding
    caller report progress through the same execution path as the multi-round API.
    """
    async with validated_invocation(workspace_root, epoch_id, instance_id) as invocation:
        return await _evolve_once(
            invocation=invocation,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            fast_mode=fast_mode,
            max_proposer_retries=max_proposer_retries,
            beater=beater,
            round_index=round_index,
            total_rounds=total_rounds,
            meta_loop_emitter=meta_loop_emitter,
            resume_plan=resume_plan,
        )


async def _evolve_once(
    *,
    invocation: InvocationContext,
    epoch_id: str | None = None,
    target_call_llm: CallLLM | None = None,
    evaluation_call_llm: CallLLM | None = None,
    fast_mode: bool = False,
    max_proposer_retries: int = 2,
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
    total_rounds: int = 0,
    meta_loop_emitter: Any = None,
    resume_plan: ResumePlan | None = None,
) -> EvolveRoundOutcome:
    """Execute a round using the invoking caller's validated ownership."""
    writer = invocation.writer
    workspace_root = writer.workspace_root
    instance_id = writer.instance_id
    validate_workspace_lock(writer, workspace_root)
    # Lazy imports — see module docstring.
    from zicato import (  # noqa: PLC0415
        adapter_factory,
        runtime_factory,
        workspace_loader,  # noqa: PLC0415
    )
    from zicato.epoch import load_epoch  # noqa: PLC0415
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.patterns.detectors import (  # noqa: PLC0415
        ALL_DETECTORS,
        DetectorInput,
        detect_patterns,
    )
    from zicato.proposer.agent import build_proposer_agent  # noqa: PLC0415
    from zicato.proposer.external import external_proposer_config  # noqa: PLC0415
    from zicato.proposer.skills import resolve_proposer_spec  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    # --- 1. Workspace + epoch artifacts ---
    workspace_config = workspace_loader.load_workspace_config(workspace_root)
    if epoch_id is None:
        resolved_epoch_id = current_epoch_id(workspace_root)
        if resolved_epoch_id is None:
            raise FileNotFoundError(
                f"no current_epoch marker under {workspace_root}; "
                "pass epoch_id explicitly or run `zicato epoch new`"
            )
    else:
        resolved_epoch_id = epoch_id

    # --- 0. Operator skip_round (the control protocol's operator override) ---
    # A clean safe point — the epoch is resolved but nothing has been
    # proposed and no tournament write is in flight. A pending skip_round
    # flag aborts this round cleanly, the same way a wall-clock budget cut does:
    # the round produces a synthetic aborted outcome (no proposer call, no
    # tournament) and the loop moves on. The flag is consumed (archived to
    # control_log/) so it fires once. The common case (no skip queued)
    # returns ``None`` and the round runs normally.
    _skip_reason = claim_skip_round(workspace_root)
    if _skip_reason is not None:
        parent_for_skip = generation_phase.safe_parent(workspace_root, resolved_epoch_id)
        log.warning(
            "evolve: round %d skipped by operator (skip_round); reason=%s",
            round_index,
            _skip_reason or "(none)",
        )
        return _skipped_round_outcome(parent_for_skip, _skip_reason)

    board, disable_drift, judge_only = workspace_loader.load_current_board_with_meta(workspace_root)
    weights = workspace_loader.load_current_scoring(workspace_root)
    brief = workspace_loader.load_current_brief(workspace_root)
    # Resolve the epoch's proposer ONCE per evolve invocation — reading the
    # frozen ``proposer_path`` off the epoch config and turning it into a
    # skills-aware :class:`ProposerAgent`. ``proposer_path is None`` (the
    # default) yields the built-in single-shot agent with no skills, so the
    # propose call is byte-identical to before this surface existed. The
    # resolution reads the skill files once here, never inside the retry
    # loop. Every tournament structure reuses the same agent.
    _epoch_cfg = load_epoch(workspace_root, resolved_epoch_id)
    # ``runtime.proposer_agent`` (absent for every workspace that
    # configures none) resolved from the SAME builder the contract hash
    # used, so the identity that was hashed and the agent that runs cannot
    # be resolved from different inputs.
    _external_cfg = external_proposer_config(workspace_config, workspace_root)
    proposer_spec = resolve_proposer_spec(_epoch_cfg.proposer_path, _external_cfg)
    # Thread the frozen ``proposer_path`` so a custom-agent spec (Design A)
    # can load ``proposers/<name>/agent.py`` from the same dir the spec was
    # resolved from. ``None`` (the default / skill-only proposer) yields the
    # single-shot built-in unchanged.
    proposer_agent = build_proposer_agent(
        proposer_spec,
        proposer_path=_epoch_cfg.proposer_path,
        external_config=_external_cfg,
    )
    # NOTE: the best-of-N proposer-quality wrapper is interposed BELOW, right
    # after the RuntimeConfig is built, because it threads the config's
    # breadth/depth ensemble-role callables into the wrapper (see there). ---
    # 0b. Durable per-round event log --- The round's store-of-record trace at
    # ``epochs/{epoch}/rounds/{round_index}/round_log.jsonl``, opened here with
    # the frozen contract hash. Every emission is best-effort: a log failure
    # can never fail the round (the index dual-write precedent).
    round_log = _RoundLogEmitter(workspace_root, resolved_epoch_id, round_index)
    round_log.emit("round_opened", {"contract_hash": _epoch_cfg.contract_hash or ""})
    # Custom judges declared on the board / per_judge_weights are valid
    # ``drift:<judge_name>`` metric targets in a proposer hypothesis even
    # though they are not built-in goldfive drift kinds.
    custom_judge_names = _declared_custom_judge_names(board, weights)
    # The per-epoch tournament structure lives on the frozen ScoringWeights;
    # reading it from the loaded weights keeps execution in lockstep with the
    # contract hash.
    tournament_spec = weights.tournament_structure

    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace_root,
        target_call_llm=target_call_llm,
        evaluation_call_llm=evaluation_call_llm,
    )
    # The factory already enforced this but the runner re-checks.
    # We do nothing more here.
    if config.instance_id != instance_id:
        config = replace(config, instance_id=instance_id)
    # Record what this round is operating under, and where each setting came
    # from, onto the heartbeat (issue #309). Stamped here because this is
    # where the runtime configuration is resolved, so the recorded values are
    # the ones in force rather than a second reading of the same files; the
    # beater carries the map forward on every later bump. Best-effort, like
    # every other observability write on this path: composing a report must
    # never fail a round.
    run_settings: dict[str, dict[str, Any]] = {}
    if beater is not None:
        with best_effort(
            "effective-settings record",
            on_error=lambda exc: log.debug("effective-settings record skipped: %s", exc),
        ):
            run_settings = effective_settings(config, workspace_config.get("runtime", {}) or {})
            beater.update(settings=run_settings)
    # Per-round token budget: mint a FRESH ledger for this round and
    # rebind it onto the config, so every runner seam that already receives
    # the config — the full/fast board-unit schedulers, the candidate
    # screen, the evidence-gate replicate duels — shares one tally with no
    # signature changes. Knob off (0 — the default) binds nothing and no
    # scheduler ever consults a ledger: byte-identical.
    if config.max_tokens_per_round > 0:
        from zicato.core.runtime import RoundTokenLedger  # noqa: PLC0415

        config = replace(config, token_ledger=RoundTokenLedger(config.max_tokens_per_round))

    # Proposer-quality levers (FUNCTIONALITY-RECOMMENDATIONS.md §4.1): with
    # ``proposer_quality.best_of_n > 1`` — the DEFAULT is 3 — interpose a
    # best-of-N + self-critique wrapper around the resolved agent. A contract
    # that pins ``best_of_n: 1`` (scripted/deterministic proposers do) gets
    # the agent back UNCHANGED — the historical single-sample propose path.
    # The critic sees ONLY the same restricted proposer context (never the
    # holdout), so best-of-N stays inside the overfitting-visibility envelope.
    #
    # Ensemble roles: the wrapper routes slate SAMPLING to the breadth
    # callable and CRITIQUE + REVISE to the depth callable, both read off the
    # RuntimeConfig (built just above). It ALSO threads the paired model-name
    # strings so the default ADK proposer (which binds ``ctx.model``, not
    # ``ctx.aux_call_llm``) honors a spec-configured role. Absent a
    # ``models.proposer_{breadth,depth}`` block the callables AND model names
    # are ``None`` and the wrapper falls back to the round's evaluation callable
    # + the context's own model — byte-identical. A models/endpoint change
    # never rolls the epoch.
    from zicato.proposer.best_of_n import wrap_with_proposer_quality  # noqa: PLC0415

    # Slate concurrency: the best-of-N slate SAMPLES fan out under
    # ``config.propose_parallelism`` (a runtime-only knob, never part of the
    # frozen contract). ``1`` runs the slate serially; the deterministic
    # post-gather pass makes any
    # value produce the same slate + event stream regardless of completion
    # order. Each slot validates into its OWN scratch tree (the factory the
    # propose builders thread on the context), so the samples never race on the
    # shared ``next_id`` derive.
    proposer_agent = wrap_with_proposer_quality(
        proposer_agent,
        weights.proposer_quality,
        breadth_call_llm=config.proposer_breadth_call_llm,
        depth_call_llm=config.proposer_depth_call_llm,
        breadth_model=config.proposer_breadth_model,
        depth_model=config.proposer_depth_model,
        propose_parallelism=config.propose_parallelism,
    )

    # --- 2. Parent generation ---
    # Materialise a v0 baseline snapshot from the registered mutable
    # trees if the epoch has no generations yet. The seed snapshot is
    # the byte-for-byte copy of the operator's registered source tree —
    # subsequent rounds patch into copies of this baseline. Without
    # this step the orchestrator's first round would have nothing to
    # diff against; the operator-facing alternative was to require a
    # manual ``zicato baseline`` invocation, but materialising it on
    # demand here keeps the CLI surface narrow.
    _ensure_baseline_snapshot(workspace_root, resolved_epoch_id, workspace_config)
    # An interrupted challenger is already the newest generation record. When
    # no champion marker exists, ``current_generation`` therefore falls back
    # to that challenger even though it has not settled. Its persisted
    # experiment is the authority on the parent the resumed tournament must
    # compare against.
    if resume_plan is not None and resume_plan.resumes_in_place:
        resume_experiment = resume_plan.resume_experiment
        if resume_experiment is None or not resume_experiment.parent_generation_id:
            raise RuntimeError("resume plan lacks its validated parent generation")
        parent_id = resume_experiment.parent_generation_id
    else:
        parent_id = generation_phase.current_generation(workspace_root, resolved_epoch_id)
    parent_gen = Generation(
        id=parent_id,
        epoch_id=resolved_epoch_id,
        parent_id=None,
        snapshot_root=generation_phase.snapshot_root(workspace_root, resolved_epoch_id, parent_id),
        created_at=_now_iso(),
        promoted=True,
    )

    # --- 2a. Optional A/A noise-floor calibration (epoch-open step) -------
    # When the workspace opts in (config.json: ``"calibrate_noise_floor": K``)
    # and this epoch has no measured floor yet, duel the champion against
    # itself K times through the same board-unit workers and persist the
    # measured spread onto the epoch record. Idempotent (the persisted record
    # short-circuits every later round) and best-effort. ``zicato board
    # audit`` is the manual surface for the same measurement.
    await _maybe_calibrate_noise_floor(
        writer=writer,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        epoch_cfg=_epoch_cfg,
        workspace_config=workspace_config,
        adapter=adapter,
        parent_gen=parent_gen,
        board=board,
        weights=weights,
        config=config,
        disable_drift=disable_drift,
        judge_only=judge_only,
        # The calibration owns the heartbeat while it draws: this round's
        # phase would otherwise stand over a serial measurement that has not
        # proposed or duelled anything (issue #175).
        beater=beater,
        round_index=round_index,
    )

    # --- 2a'. Contract pre-flight (epoch-open step) ----------------------
    # DEFAULT-ON (issue #84): unless runtime.preflight_gate == "off", measure
    # the A/A floor AND the degradation signal (champion against an ephemeral
    # copy of itself degraded on purpose) once per epoch and persist the
    # verdict. A below-floor / saturated / inert verdict — or a promote_margin
    # outside the floor/signal window (issue #112) — is LOUDLY warned (inside
    # the helper) and flows into the per-round health report. Under the opt-in
    # HARD gate (preflight_gate == "refuse") a refuse-worthy verdict STOPS the
    # run here, before rounds burn budget on a contract that cannot be
    # optimized. Only the FLOOR-based verdict refuses: the window's upper
    # comparison is against degradation headroom, which does not bound what a
    # challenger can achieve (issue #119).
    # ``zicato board preflight`` is the manual surface.
    _preflight_verdict = await _maybe_contract_preflight(
        writer=writer,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        epoch_cfg=_epoch_cfg,
        workspace_config=workspace_config,
        adapter=adapter,
        parent_gen=parent_gen,
        board=board,
        weights=weights,
        config=config,
        disable_drift=disable_drift,
        judge_only=judge_only,
        # The pre-flight owns the heartbeat while it measures, for the same
        # reason the calibration above does: this round's phase would
        # otherwise stand over a serial step that has proposed and duelled
        # nothing (issue #276).
        beater=beater,
        round_index=round_index,
    )
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_REFUSE,
        PreflightRefusedError,
    )

    if (
        str(getattr(config, "preflight_gate", "warn") or "warn") == "refuse"
        and _preflight_verdict == VERDICT_REFUSE
    ):
        # One cause reaches here, and it is the honestly-measured one: the
        # contract's own measured movement does not clear its own A/A noise.
        # The margin-window failures warn rather than refuse (issue #119),
        # because their upper comparison is against DEGRADATION headroom,
        # which bounds a challenger's improvement from neither side.
        raise PreflightRefusedError(
            f"contract pre-flight REFUSE for epoch {resolved_epoch_id}: the "
            "contract's measured signal is at or below its measured A/A noise "
            "floor, so every duel would be decided by noise. Strengthen the "
            "board / reduce evaluation noise. Refusing the run before it spends "
            "rounds (runtime.preflight_gate='refuse'); set "
            "runtime.preflight_gate='warn' to proceed anyway."
        )

    # --- 2b. Margin-vs-noise-floor sanity check (once per invocation) -----
    # When a measured floor exists and the contract's promote_margin sits
    # inside it, say so loudly at evolve start — a duel decided by the margin
    # alone cannot distinguish a real improvement from an A/A re-roll. Never
    # hard-refuses; the per-round health report carries the matching finding.
    if round_index == 0:
        _warn_margin_below_noise_floor(workspace_root, resolved_epoch_id)

    # --- 2c. Replicates in effect -------------------------------------------
    # The count every duel of this epoch runs: the contract's pinned value,
    # else the smallest count whose minimum detectable effect at the floor
    # persisted above is within promote_margin, else the structure's default.
    # Resolved after the floor is known and stamped onto the effective-settings
    # record with its source; the strategy below resolves the same count. A
    # derived count is a runtime record and never enters the contract.
    replicate_setting = _resolve_replicates_in_effect(
        workspace_root, resolved_epoch_id, tournament_spec, log_once=round_index == 0
    )
    if beater is not None:
        with best_effort(
            "effective-settings record",
            on_error=lambda exc: log.debug("effective-settings record skipped: %s", exc),
        ):
            run_settings[TOURNAMENT_REPLICATES_KEY] = recorded_setting(
                replicate_setting.replicates, replicate_setting.source
            )
            beater.update(settings=run_settings)

    # --- 3. Mutations ---
    mutations = enumerate_mutations(
        generation_phase.mutable_trees(adapter, parent_gen.snapshot_root)
    )
    if not mutations:
        raise RuntimeError(
            f"no mutation points enumerated under {parent_gen.snapshot_root}; "
            "did the adapter declare its mutable_trees?"
        )
    # Best-effort: snapshot the enumerated mutation surface so the
    # dashboard can render it for the in-progress epoch. A failure to
    # write the snapshot must never abort the round.
    _dump_mutations_snapshot(workspace_root, resolved_epoch_id, mutations)
    # --- 4. Patterns ---
    # The proposer + detectors + loss summary see the TRAIN slice ONLY
    # (OVERFITTING.md §11.1, §12 #1): the holdout's per-entry behaviour is
    # never surfaced to the proposer, so it cannot be memorized. When the
    # board is too small to split (the default-safe degrade), the train
    # slice IS the full board and every downstream artifact is byte-
    # identical to the pre-split behaviour. The mutation manifest (code
    # spans) is unrelated to the split and is left untouched.
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    # Thread the epoch id as the rotation seed (OVERFITTING.md §12 #6) so the
    # holdout slice is stable within this epoch but rotates across epochs.
    # ``rotation_seed`` returns ``None`` (the unseeded, byte-identical split)
    # when ``rotate_holdout`` is off.
    train_seed = rotation_seed(weights.overfitting, resolved_epoch_id)
    train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=train_seed)
    train_id_set = set(train_ids)
    train_board = [e for e in board if e.id in train_id_set]
    losses = _load_parent_losses(
        workspace_root, resolved_epoch_id, parent_id, train_board, read_loss_profile
    )
    events_paths = _build_events_paths(workspace_root, resolved_epoch_id, parent_id, train_board)
    detector_input = DetectorInput(
        losses=losses,
        entries={e.id: e for e in train_board},
        events_paths=events_paths,
    )
    patterns = detect_patterns(detector_input, detectors=ALL_DETECTORS)

    # --- 5. What this contract scores, and the loss summary that reports it ---
    # The operator already answered "what should this round work on" by setting
    # the scoring weights; resolving them here — the one place the frozen
    # contract, the board and the round's own losses are all in scope — is what
    # lets the prompt pass that answer along. The weights themselves stay on
    # this side: only the BANDED render crosses into ProposerContext, because a
    # raw coefficient hands every custom proposer agent the objective function.
    from zicato.proposer.prompts import render_metric_priorities_block  # noqa: PLC0415

    metric_priorities = build_metric_priorities(board, weights, losses)
    metric_priorities_block = render_metric_priorities_block(metric_priorities)
    loss_summary = _render_loss_summary(losses, metric_priorities)

    # --- 5a. Outcome-marginal failure-mode profile (issue #18 cap 2) ---
    # Aggregate the SAME train-slice ``losses`` (holdout already excluded
    # above) into board-anonymous outcome marginals — generic failure modes
    # (empty / terse / looping / pass-rate / score bands) plus, when
    # Capability 1's per-entry metrics carry them, the recall/precision
    # decomposition. The optional operator summarizer hook contributes extra
    # marginals, sanitized + banded by zicato so its output cannot leak. The
    # rendered block is bucketed + identity-free; an empty slice (or no
    # outcome data) renders the EMPTY STRING, so the proposer prompt then
    # carries no such section (OVERFITTING.md §11.4).
    failure_profile = _render_failure_profile(losses, weights)

    # --- 5a''. Opt-in process-exemplar block (PROCESS-EXEMPLARS.md) ---
    # When the contract opts in (proposer_quality.process_exemplars > 0),
    # extract up to that many drift-anchored, mechanically-REDACTED event
    # windows from the champion's TRAIN-slice events.jsonl files — the same
    # parent + train partition the patterns above used — so the proposer
    # can see HOW a detected failure unfolds, never WHICH entry it unfolded
    # on. Best-effort: any failure renders the empty string (the "omit this
    # section" sentinel) and the round proceeds untouched. OFF by default:
    # no extraction runs and the prompt carries no such section.
    process_exemplars_block = _render_process_exemplars_block(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        patterns=patterns,
        train_entry_ids=[e.id for e in train_board],
        weights=weights,
    )

    # --- 5a'. Optional pre-tournament candidate screen (tryouts) ---
    # ONE closure per round, built only when the contract opts in
    # (proposer_quality.screen_entries > 0 AND best_of_n > 1) — otherwise
    # ``None`` and no screen callable even exists on the propose path. It
    # binds this round's rotating TRAIN panel (never the holdout), the
    # parent's replicate-0 baseline passes, and the frozen weights; the
    # best-of-N wrapper calls it GUARDED once its slate settles, so a
    # catastrophically-regressed candidate is vetoed before selection.
    screen_candidates = _build_candidate_screen_runner(
        writer=writer,
        weights=weights,
        adapter=adapter,
        parent_gen=parent_gen,
        train_board=train_board,
        parent_losses=losses,
        config=config,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        round_index=round_index,
        disable_drift=disable_drift,
        judge_only=judge_only,
        beater=beater,
    )

    # --- 5a''. Optional recombination pair ---
    # ONE selection per round, built only when the contract opts in
    # (proposer_quality.recombine AND best_of_n > 1) — otherwise ``None``
    # and no pair even rides the propose path. Plain DATA (not a callable):
    # the selection depends only on round-start state, so the proposer
    # stack stays IO-free. Best-effort by contract — any failure inside
    # degrades to ``None`` and the round is byte-identical.
    recombine_pair = _build_recombination_pair(
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        train_entry_ids=frozenset(e.id for e in train_board),
        mutations=mutations,
    )

    # --- 5a'''. Optional genealogy channel ---
    # ONE sampling per round, built only when the contract opts in
    # (proposer_quality.genealogy > 0) — otherwise () and no items ride the
    # propose path. Read-side only (the meter is untouched): the sampler reads
    # the reign's durable records + the Elo fold and returns already-banded,
    # already-capped candidate-lineage items (PARENTS = the champion's promoted
    # spine; INSPIRATIONS = diverse rejected reign candidates). ALL best-of-N
    # slots see the same items (in-context evolution — the LLM can merge ideas
    # itself). Best-effort — any failure inside degrades to () and the round is
    # byte-identical.
    genealogy_items = _build_genealogy_items(
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
    )

    # --- 5a''''. Optional critic-calibration channel ---
    # ONE summary per round, built only when the contract opts in
    # (proposer_quality.calibration_feedback > 0) — otherwise ``None`` and no
    # summary rides the propose path. Read-side only (the meter is untouched):
    # the builder joins the reign's durable records with the prediction-accuracy
    # grader's ledger and returns an already-banded, aggregate-count summary of
    # how the proposer's OWN past predictions landed. ALL best-of-N slots see
    # the same summary. Best-effort — any failure inside degrades to ``None``
    # and the round is byte-identical.
    calibration_summary = _build_calibration_summary(
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
    )

    # --- 5b. Tournament-structure dispatch ---
    # The strategy defines field width and matchup topology. Every structure,
    # including the one-challenger gauntlet, enters the same candidate,
    # evaluation, evidence-confirmation, and settlement pipeline. Inter-round
    # stopping remains in evolve_n_rounds, outside the strategy.
    from zicato.selection.registry import make_strategy  # noqa: PLC0415

    # Internal field matchups run exclusively on the train board. Inject its
    # ids so a board-aware structure (racing) never allocates a rung
    # position to a confirmation-only holdout entry; an explicit
    # ``params["board_ids"]`` still overrides. Board-agnostic structures
    # (gauntlet, single/double-elim, swiss) ignore the metadata.
    strategy = make_strategy(
        tournament_spec,
        board_ids=[e.id for e in train_board],
        replicates=replicate_setting.replicates,
        noise_floor_delta_std=replicate_setting.delta_std,
        experimental_structures=weights.experimental.tournament_structures,
    )
    prepared = generation_phase.PreparedRound(
        writer=writer,
        workspace_root=workspace_root,
        workspace_config=workspace_config,
        epoch_id=resolved_epoch_id,
        round_index=round_index,
        total_rounds=total_rounds,
        instance_id=instance_id,
        parent_generation=parent_gen,
        adapter=adapter,
        config=config,
        weights=weights,
        board=tuple(board),
        train_board=tuple(train_board),
        tournament_spec=tournament_spec,
        strategy=strategy,
        brief=brief,
        mutations=tuple(mutations),
        patterns=tuple(patterns),
        loss_summary=loss_summary,
        failure_profile=failure_profile,
        metric_priorities=metric_priorities_block,
        process_exemplars=process_exemplars_block,
        genealogy=tuple(genealogy_items),
        calibration=calibration_summary,
        disable_drift=tuple(disable_drift),
        judge_only=judge_only,
        fast_mode=fast_mode,
        max_proposer_retries=max_proposer_retries,
        beater=beater,
        meta_loop_emitter=meta_loop_emitter,
        proposer_agent=proposer_agent,
        round_log=round_log,
        screen_candidates=screen_candidates,
        recombine_pair=recombine_pair,
        custom_judge_names=custom_judge_names,
    )
    from zicato.evolve.field import evolve_field_round

    return await evolve_field_round(prepared, resume_plan=resume_plan)
