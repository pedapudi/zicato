"""Single-challenger round strategy."""

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
    OutcomeRecord,
    TournamentDecision,
)
from zicato.evolve import generation_phase
from zicato.evolve.candidate_batch import produce_candidate_batch
from zicato.evolve.gate import (
    _confirm_gauntlet_promotion,
    _gauntlet_decision_from_result,
    _integrity_block_reason,
    _registered_mutable_trees,
)
from zicato.evolve.ingest import (
    _cache_gen_score,
)
from zicato.evolve.lifecycle_services import (
    _beat,
    _now_iso,
)
from zicato.evolve.pareto import record_round_frontier
from zicato.evolve.persist import (
    _finalize_generation,
    _persist_rejected_round,
    _rejected_proposer_experiment,
    _round_epilogue,
    _skipped_round_outcome,
)
from zicato.evolve.promote_hook import fire_on_promote
from zicato.evolve.propose_apply import (
    _maybe_run_placebo_arm_gauntlet,
)
from zicato.evolve.round_context import (
    _build_calibration_summary,
    _build_candidate_screen_runner,
    _build_genealogy_items,
    _build_recombination_pair,
)
from zicato.runtime.control_consumer import (
    claim_gate_override,
    claim_skip_round,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.resume import ResumePlan
from zicato.util import best_effort

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

from zicato.evolve.decision_support import (
    _build_events_paths,
    _count_infra_aborted_runs,
    _defer_round_infra_outage,
    _generalization_fields,
    _load_parent_losses,
    _render_failure_profile,
    _render_loss_summary,
    _render_process_exemplars_block,
    _token_clip_state,
    build_metric_priorities,
)
from zicato.evolve.round_api import EvolveRoundOutcome, _declared_custom_judge_names
from zicato.evolve.round_baseline import (
    _dump_mutations_snapshot,
    _ensure_baseline_snapshot,
    _load_historical_aggregate,
)
from zicato.evolve.round_prepare import (
    _maybe_calibrate_noise_floor,
    _maybe_contract_preflight,
    _warn_margin_below_noise_floor,
)
from zicato.evolve.round_reporting import (
    _emit_gate_evaluated,
    _emit_harness_loaded,
    _emit_tournament_units,
    _promoted_entry_regressions,
    _RoundLogEmitter,
)


async def evolve_once(
    *,
    workspace_root: Path,
    epoch_id: str | None = None,
    harness_call_llm: CallLLM,
    auxiliary_call_llm: CallLLM,
    instance_id: str = "default",
    fast_mode: bool = False,
    max_proposer_retries: int = 2,
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
    total_rounds: int = 0,
    meta_loop_emitter: Any = None,
    resume_plan: ResumePlan | None = None,
    workspace_checked: bool = False,
) -> EvolveRoundOutcome:
    """Run ONE evolve round against the current epoch.

    ``workspace_checked`` says the caller has already run the pre-spend
    workspace gate for this invocation. :func:`evolve_n_rounds` passes it
    so a multi-round run pays for the gate once rather than per round.
    Default ``False``, because this function is exported as
    :func:`zicato.orchestrator.evolve_once` and a library caller entering
    here spends a full round: it is a spend boundary in its own right and
    gates itself accordingly.

    ``resume_plan`` — when supplied by :func:`evolve_n_rounds` for the
    FIRST round of a resumed invocation — carries the conservative
    crash-resume decision (``runtime/resume.py``). When it
    ``resumes_in_place`` for the generation this round would mint, the
    propose + apply step is skipped and the persisted
    ``experiment.json`` + patches are reused: the snapshot is re-derived
    from those same patches (idempotent) and the tournament cache-HITs
    every board unit that already has a ``loss.json`` on disk, so only
    the entries that did not finish are re-run. ``None`` (the default, and
    every round after the first) is byte-identical to a cold start.

    ``beater`` — when supplied by :func:`evolve_n_rounds` — receives a
    :meth:`HeartbeatBeater.update` call at every phase transition
    (proposing / applying / tournament / done) stamped with the real
    ``epoch_id``, the generation id being worked on, and the
    ``round_index``, so the dashboard header reflects live progress.
    When ``None`` (a standalone ``evolve_once`` call) the heartbeat
    plumbing is simply skipped. ``round_index`` / ``total_rounds`` are
    also threaded into :func:`run_tournament` so the published
    tournament state can render "round N of M".

    Steps:

    1. Load the workspace config and the current epoch (board, proposer
       brief, scoring, adapter via the workspace's adapter factory).
    2. Resolve the current promoted generation as the parent.
    3. Re-enumerate mutation points against the parent's snapshot.
    4. Detect cross-run patterns over the parent's loss profiles.
    5. Render a short loss summary for the proposer.
    6. Call :func:`zicato.proposer.proposer.propose_experiment` with
       the auxiliary callable.
    7. Cross-check every patch's ``mutation_id`` against the
       re-enumerated mutation manifest.
    8. Apply the patches into a fresh
       ``generations/{new_gen}/snapshot/`` via the mutation applier.
    9. Validate the new snapshot via the mutation validator.
    10. Run the tournament (full or fast mode).
    11. Persist ``experiment.json`` + ``patches/{id}.json`` with the
        outcome populated.
    12. On promotion: update lineage and bump the current_epoch's
        promoted-head marker (a per-epoch ``current_generation`` file).
    13. Append a journal entry for the experiment.

    Returns
    -------
    EvolveRoundOutcome
        Always returned (one round, one outcome). Exceptions only
        propagate for unrecoverable errors (e.g. the proposer raises
        :class:`ProposerError` after exhausting retries; the validator
        rejects the new snapshot; the patch applier hits a stale
        mutation manifest).
    """
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
    from zicato.tournament.runner import (  # noqa: PLC0415
        run_fast_mode,
        run_tournament,
    )

    # The mandatory pre-spend gate, unless the caller already ran it (see
    # ``workspace_checked``). Imported per call, not at module scope: suites
    # that drive a round against a deliberately minimal fixture workspace
    # patch ``zicato.check.require_workspace_valid``, and hoisting this would
    # bind the function once and silently defeat every one of those patches.
    if not workspace_checked:
        from zicato.check import require_workspace_valid  # noqa: PLC0415

        require_workspace_valid(
            workspace_root,
            epoch_id=epoch_id,
            live_contract=epoch_id is None,
        )

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

    # --- 0. Operator skip_round (control protocol, RUNTIME-V2 Phase 2) ---
    # A clean safe point — the epoch is resolved but nothing has been
    # proposed and no tournament write is in flight. A pending skip_round
    # flag aborts this round cleanly, exactly like a wall-clock budget cut:
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
    # loop. Both the gauntlet path and the multi-challenger field reuse the
    # same agent.
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
    # after the RuntimeConfig is built, because it now threads the config's
    # WS-ENS ensemble-role callables into the wrapper (see there).
    # --- 0b. Durable per-round event log (WS8) ---
    # The round's store-of-record trace at
    # ``epochs/{epoch}/rounds/{round_index}/round_log.jsonl``, opened here
    # with the frozen contract hash. Every emission is best-effort: a log
    # failure can never fail the round (the index dual-write precedent).
    round_log = _RoundLogEmitter(workspace_root, resolved_epoch_id, round_index)
    round_log.emit("round_opened", {"contract_hash": _epoch_cfg.contract_hash or ""})
    # Custom judges declared on the board / per_judge_weights are valid
    # ``drift:<judge_name>`` metric targets in a proposer hypothesis even
    # though they are not built-in goldfive drift kinds.
    custom_judge_names = _declared_custom_judge_names(board, weights)
    # The per-epoch tournament structure (gauntlet by default). It lives
    # on the frozen ScoringWeights; reading it off the loaded weights
    # keeps it in lockstep with the contract hash. The gauntlet path
    # below preserves today's exact behaviour; non-gauntlet structures
    # drive a multi-challenger field through resolve_tournament.
    tournament_spec = weights.tournament_structure

    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
    )
    # The factory already enforced this but the runner re-checks.
    # We do nothing more here.
    if config.instance_id != instance_id:
        config = replace(config, instance_id=instance_id)
    # Per-round token budget (WS-H): mint a FRESH ledger for this round and
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
    # WS-ENS ensemble roles: the wrapper routes slate SAMPLING to the breadth
    # callable and CRITIQUE + REVISE to the depth callable, both read off the
    # RuntimeConfig (built just above). It ALSO threads the paired model-name
    # strings so the default ADK proposer (which binds ``ctx.model``, not
    # ``ctx.aux_call_llm``) honors a spec-configured role. Absent a
    # ``models.proposer_{breadth,depth}`` block the callables AND model names
    # are ``None`` and the wrapper falls back to the round's auxiliary callable
    # + the context's own model — byte-identical. A models/endpoint change
    # never rolls the epoch.
    from zicato.proposer.best_of_n import wrap_with_proposer_quality  # noqa: PLC0415

    # WS-CONC: the best-of-N slate SAMPLES fan out under
    # ``config.propose_parallelism`` (a runtime-only knob, never part of the
    # frozen contract). ``1`` runs the slate serially, byte-identically to the
    # pre-concurrency wrapper; the deterministic post-gather pass makes any
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
    # the A/A floor AND the degradation signal (champion vs a deliberately-
    # degraded ephemeral copy of itself) once per epoch and persist the
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
        # The margin-window failures used to refuse too, and used to need their
        # own sentence here; they are warnings now (issue #119) because their
        # upper comparison is against DEGRADATION headroom, which bounds a
        # challenger's improvement from neither side.
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
    # outcome data) renders the EMPTY STRING, so the proposer prompt is
    # byte-identical to today (OVERFITTING.md §11.4).
    failure_profile = _render_failure_profile(losses, weights)

    # --- 5a''. Opt-in process-exemplar block (PROCESS-EXEMPLARS.md) ---
    # When the contract opts in (proposer_quality.process_exemplars > 0),
    # extract up to that many drift-anchored, mechanically-REDACTED event
    # windows from the champion's TRAIN-slice events.jsonl files — the same
    # parent + train partition the patterns above used — so the proposer
    # can see HOW a detected failure unfolds, never WHICH entry it unfolded
    # on. Best-effort: any failure renders the empty string (the "omit this
    # section" sentinel) and the round proceeds untouched. OFF by default:
    # no extraction runs and the prompt is byte-identical to today.
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

    # --- 5a''. Optional recombination pair (WS-REC) ---
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

    # --- 5a'''. Optional genealogy channel (WS-GENE) ---
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

    # --- 5a''''. Optional critic-calibration channel (WS-CAL) ---
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
    # The gauntlet (the default and back-compat baseline) has field_size
    # == 1: one champion, one challenger, one full-board duel. Steps 6-13
    # below preserve that path byte-for-byte. A non-gauntlet structure with
    # a wider field (field_size > 1) is driven by the SelectionStrategy:
    # the orchestrator proposes + applies N challengers and runs the
    # strategy's scheduled matchups through resolve_tournament (each via the
    # same board-unit runner + unchanged promote gate). The §5 inter-round
    # stopping stays in evolve_n_rounds, OUTSIDE the strategy.
    from zicato.selection.registry import make_strategy  # noqa: PLC0415

    # Internal field matchups run exclusively on the train board. Inject its
    # ids so a board-aware structure (racing) never allocates a rung
    # position to a confirmation-only holdout entry; an explicit
    # ``params["board_ids"]`` still overrides. Board-agnostic structures
    # (gauntlet, single/double-elim, swiss) ignore the metadata.
    strategy = make_strategy(
        tournament_spec,
        board_ids=[e.id for e in train_board],
    )
    prepared = generation_phase.PreparedRound(
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
    if strategy.field_size() > 1:
        from zicato.evolve.field import evolve_field_round

        return await evolve_field_round(prepared)

    # --- 6-9. Produce the gauntlet's one-candidate batch ---
    candidate_batch = await produce_candidate_batch(prepared, 1, resume_plan=resume_plan)
    next_id = candidate_batch.base_generation_id
    if not candidate_batch.challengers:
        from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

        rejection = candidate_batch.rejections[0]
        error = rejection.proposer_error
        assert isinstance(error, ProposerError)
        experiment = _rejected_proposer_experiment(
            resolved_epoch_id,
            parent_id,
            next_id,
            error,
        )
        return await _persist_rejected_round(
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            experiment=experiment,
            validation_errors=list(error.attempts),
            proposer_retries_exhausted=True,
            board=board,
            round_index=round_index,
            auxiliary_call_llm=auxiliary_call_llm,
            auxiliary_model=str(workspace_config.get("auxiliary_model", "")),
            beater=beater,
            round_log=round_log,
        )

    challenger = candidate_batch.challengers[0]
    experiment = challenger.experiment
    child_snapshot = challenger.snapshot_root
    child_gen = challenger.generation
    resumed_experiment = experiment if next_id in candidate_batch.resumed_generation_ids else None

    # --- 10. Run the tournament ---
    from zicato.runtime import progress_log  # noqa: PLC0415

    _beat(
        beater,
        workspace_root=workspace_root,
        progress=progress_log.TOURNAMENT_START,
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"tournament:round_{round_index}:{next_id}",
    )

    # Fast mode reuses the parent/champion's cached aggregate instead of
    # re-running it every round. The very first round of a fresh epoch
    # has no cache yet, so fast mode degrades to a single full A/B
    # tournament for that round — which scores the parent and writes the
    # cache below — and every subsequent fast round reuses it. This
    # makes ``--mode fast`` safe as the default without an operator
    # having to seed the cache with a manual full round first.
    parent_historical: dict[str, Any] | None = None
    if fast_mode:
        try:
            parent_historical = _load_historical_aggregate(
                workspace_root, resolved_epoch_id, parent_id
            )
        except (FileNotFoundError, ValueError) as exc:
            log.info(
                "fast-mode evolve: no cached parent aggregate (%s); "
                "running a full tournament this round to seed the cache",
                exc,
            )
            parent_historical = None
    # Diff-complexity (parsimony / MDL) input, OVERFITTING.md §5 / §12 #4. The
    # challenger's diff size from its patch records, threaded into the
    # CHALLENGER aggregate only. Folds a ``diff_complexity`` component into the
    # challenger's scalar ONLY when ``weights.diff_complexity_weight > 0``;
    # ``aggregate_generation_score`` treats it as exactly absent at the default,
    # so computing it here is harmless for a contract that does not opt in. Fast
    # mode compares against a cached whole-board champion aggregate and does not
    # re-derive the challenger scalar through this path, so the term applies on
    # the full A/B path (where the gate actually re-scores the challenger).
    #
    # The PARENT-side text of every mutation point is threaded in so the term
    # measures the EDIT rather than the replacement (issue #120): a whole-file
    # point hands the proposer the entire file and takes the entire file back,
    # so without the parent text every proposal is charged for the template it
    # was required to preserve. ``mutations`` was enumerated against the parent
    # snapshot above, so this is content already in memory — the scoring layer
    # stays pure and never re-reads the tree.
    from zicato.scoring.diff_complexity import diff_size as _diff_size  # noqa: PLC0415

    child_diff_size = _diff_size(experiment, {m.id: m.content for m in mutations})
    if fast_mode and parent_historical is not None:
        # The contract's replication knob reaches the gauntlet fast path
        # (issue #109): the challenger board runs ``replicates`` times and
        # the per-entry losses are folded, exactly as ``run_matchup``
        # already does under ``fast=True``. Before this the parameter did
        # not exist on ``run_fast_mode`` at all, so the default
        # configuration — ``--mode fast`` is the CLI default and the
        # gauntlet's default ``replicates`` is 2 — silently executed as 1.
        #
        # The champion side stays ONE frozen cached draw (that is what fast
        # mode IS), so the contrast is a replicated challenger against an
        # unreplicated champion. Say so out loud rather than letting the
        # operator infer a symmetric √K from the contract: an operator who
        # wants independent draws on both sides wants --mode full.
        fast_replicates = strategy.replicates()
        if fast_replicates > 1:
            log.warning(
                "fast-mode gauntlet round: replicating the CHALLENGER board %d× "
                "(contract replicates=%d), but the champion side is a single "
                "frozen cached aggregate — the noise reduction is one-sided. "
                "Use --mode full for independent draws on both sides.",
                fast_replicates,
                fast_replicates,
            )
        tournament_result = await run_fast_mode(
            adapter=adapter,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            parent_historical_agg=parent_historical,
            disable_drift=disable_drift,
            judge_only=judge_only,
            round_index=round_index,
            total_rounds=total_rounds,
            replicates=fast_replicates,
        )
    else:
        tournament_result = await run_tournament(
            adapter=adapter,
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            disable_drift=disable_drift,
            judge_only=judge_only,
            # ``--mode full`` (not fast_mode) re-samples both sides for noise,
            # so force-fresh the champion too. The fast-mode seeding fallback
            # (no cached parent aggregate this round) cache-reads the immutable
            # champion (default) — reusing its prior-round / seed-scoring units
            # instead of re-running it every round (§2 item 3).
            #
            # A conservative crash-resume (``resumed_experiment is not None``)
            # is the OTHER cache-read case: the interrupted round's completed
            # champion units are on disk and MUST be reused, or resume is no
            # longer nearly free. So a resumed round suppresses the full-mode
            # champion re-sample and cache-reads the champion regardless of
            # mode — the union of the §2-item-3 win and the resume protocol.
            champion_force_fresh=(not fast_mode) and resumed_experiment is None,
            round_index=round_index,
            total_rounds=total_rounds,
            # Conservative crash-resume: read the per-unit loss.json cache
            # so an interrupted round's completed board units are HITS and
            # only the unfinished entries re-run. A cold start keeps the
            # historical force-fresh full A/B evaluation byte-for-byte.
            force_fresh=resumed_experiment is None,
            # Opt-in parsimony / MDL term: the challenger's diff size. A no-op
            # at the default ``diff_complexity_weight == 0.0`` (the term is
            # exactly absent), so this is byte-identical for contracts that do
            # not opt in.
            child_diff_size=child_diff_size,
            # The structure's resolved per-duel replication (the strategy
            # resolves params["replicates"] against its default — 2 for the
            # gauntlet, the noise-aware posture; averaged paired runs). Pin
            # "replicates": 1 in the contract for the historical single-run
            # duel.
            replicates=strategy.replicates(),
        )

    # --- 10a. Endpoint-outage circuit (WS-H) ------------------------------
    # BEFORE anything downstream consumes the duel (the fast-mode score
    # caches, the round events, the gate routing): when the runtime opted
    # in (``infra_abort_round_threshold >= 1``) and this round's
    # INFRA-aborted run count reached it, the verdict is meaningless — the
    # aborted runs scored worst-case, so feeding the gate would burn the
    # experiment on an endpoint outage. Defer instead: the
    # ``experiment.json`` written above stays UN-OUTCOMED on disk, exactly
    # the shape :mod:`zicato.runtime.resume` reconciles (resume-in-place
    # when some units completed, discard-no-progress when none did).
    # Threshold 0 (the default) skips this block entirely — byte-identical.
    infra_threshold = int(getattr(config, "infra_abort_round_threshold", 0) or 0)
    if infra_threshold > 0:
        infra_aborted = _count_infra_aborted_runs(tournament_result)
        if infra_aborted >= infra_threshold:
            return _defer_round_infra_outage(
                workspace_root=workspace_root,
                epoch_id=resolved_epoch_id,
                parent_id=parent_id,
                next_id=next_id,
                board=board,
                round_index=round_index,
                infra_aborted=infra_aborted,
                infra_threshold=infra_threshold,
                beater=beater,
                round_log=round_log,
            )

    # Cache gen_score.json for future fast-mode runs. The round is threaded
    # through so the archived measurement beside it (gen_score.history.jsonl)
    # names the round it was taken in — the champion defends across many
    # rounds under one generation id, so the round is the only thing that
    # tells two of its measurements apart (issue #122).
    _cache_gen_score(
        workspace_root,
        resolved_epoch_id,
        parent_id,
        tournament_result.parent_agg,
        round_index=round_index,
    )
    _cache_gen_score(
        workspace_root,
        resolved_epoch_id,
        next_id,
        tournament_result.child_agg,
        round_index=round_index,
    )

    # WS8: the duel's board units (aggregate — see _emit_tournament_units),
    # the gate verdict, and — when the runner consulted a holdout — the
    # Ladder's release, all onto the round's durable event log.
    _emit_tournament_units(round_log, tournament_result)
    _emit_harness_loaded(round_log, workspace_root, resolved_epoch_id, tournament_result)
    _emit_gate_evaluated(
        round_log,
        tournament_result.outcome,
        parent_agg=tournament_result.parent_agg,
        child_agg=tournament_result.child_agg,
        weights=weights,
    )
    _holdout_block = getattr(tournament_result, "holdout", None)
    if _holdout_block is not None:
        round_log.emit(
            "holdout_released",
            {"confirmed": bool(_holdout_block.get("confirmed"))},
        )

    # Progress transition: the tournament settled (a verdict is resolvable).
    # Advances the orchestrator liveness seq on genuine progress — never on
    # a timer — so a slow tournament reads as "between transitions", not
    # stalled. Best-effort; a write failure must not abort the round.
    with best_effort(
        "progress-log tournament-settle",
        on_error=lambda exc: log.debug("progress-log tournament-settle skipped: %s", exc),
    ):
        progress_log.append_progress(workspace_root, progress_log.TOURNAMENT_SETTLE)

    # --- 10b. Route the duel's verdict through the SelectionStrategy ---
    # The structure owns scheduling/advance/stopping; the gate is reused
    # verbatim (run_tournament/run_fast_mode already ended in
    # evaluate_gate). For the gauntlet — the default and the back-compat
    # baseline — there is exactly one champion-vs-challenger duel, so we
    # feed the single TournamentResult into the gauntlet strategy and read
    # its SelectionDecision. This makes the decision swappable while
    # reproducing today's promote-on-gate behaviour byte-for-byte; the
    # strategy never re-decides the duel.
    selection_decision = _gauntlet_decision_from_result(
        tournament_spec, parent_id, next_id, child_snapshot, tournament_result
    )

    # --- 10b'. Evidence-gate confirmation of the crowning promote ---------
    # When the contract opts into ``promote_confidence_threshold`` (the
    # scaffolded contracts do; the bare default is off — the gate is a
    # soundness device whose CI separation needs a long win streak, see
    # zicato.selection.evidence_gate), a train-promote (post holdout
    # confirmation — the gate outcome above is already Ladder-mediated) is
    # confirmed by the SAME Bradley--Terry defer→replicate→inconclusive
    # adjudication the multi-challenger driver runs, before anything is
    # persisted. CIs that never separate within the replicate budget leave
    # the champion standing (a DEFERRED outcome + a dead-letter record).
    # Unset ⇒ a no-op pass-through, byte-identical to the plain gate.
    selection_decision, gate_evidence = await _confirm_gauntlet_promotion(
        selection_decision,
        tournament_spec=tournament_spec,
        adapter=adapter,
        parent_gen=parent_gen,
        child_gen=child_gen,
        train_board=train_board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        disable_drift=disable_drift,
        judge_only=judge_only,
        fast_mode=fast_mode,
        round_index=round_index,
        total_rounds=total_rounds,
        beater=beater,
    )
    # WS8: one ``evidence_replicated`` per evidence-gate refit (the
    # ``ci_history`` trace rows the pre-gate accumulated while it deferred).
    if gate_evidence is not None:
        for _ci_row in gate_evidence.get("ci_history", []):
            round_log.emit("evidence_replicated", {"ci_state": dict(_ci_row)})

    # --- 10b''. Opt-in integrity blocking modes (default OFF) -------------
    # Both checks (diff containment + gate-contradiction re-derivation)
    # guard the GATE-DECIDED promotion only, BEFORE the operator-override
    # claim below: an explicit force-promote remains the operator's
    # recorded prerogative. Default-off keeps this branch inert and the
    # round byte-identical (alarm-only supervisor posture).
    if selection_decision.decision == "promoted":
        _block_reason = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=parent_gen.snapshot_root,
            child_snapshot_root=child_snapshot,
            mutable_trees=_registered_mutable_trees(workspace_config),
            delta_scalar=tournament_result.outcome.delta_scalar,
        )
        if _block_reason is not None:
            log.warning(
                "evolve: integrity block — generation %s promotion refused (%s)",
                next_id,
                _block_reason,
            )
            selection_decision = replace(
                selection_decision,
                promoted_generation_id=None,
                decision=TournamentDecision.REJECTED,
                reason=_block_reason,
            )

    # --- 10c. Operator gate override (control protocol, RUNTIME-V2 Phase 2) ---
    # The gate has settled but the outcome is not yet persisted — the safe
    # point at which an operator's force-promote / force-reject of THIS
    # generation can override the verdict. A matching command is claimed +
    # archived to control_log/; the override is recorded explicitly on the
    # OutcomeRecord below (never a silent flip). No pending override (the
    # common case) leaves the gate's decision untouched.
    decision = selection_decision.decision
    override_reason = selection_decision.reason
    operator_override = False
    operator_override_reason = ""
    gate_override = claim_gate_override(workspace_root, next_id)
    if gate_override is not None:
        log.warning(
            "evolve: operator override — generation %s force-%s "
            "(gate said %r); recording as an explicit override. reason=%s",
            next_id,
            gate_override.decision,
            decision,
            gate_override.reason,
        )
        # ``GateOverride.decision`` is a control-protocol WIRE token, and this
        # is the boundary where it enters a typed record: ``OutcomeRecord``
        # declares the enum, so an override round must not be the one round
        # that writes a bare str into it (issue #132 — the coercion is what
        # keeps a live record ``isinstance``-indistinguishable from the same
        # record read back off disk, which the journal hydrator already
        # coerces). The consumer builds the token from its own two command
        # constants, so the coercion is total.
        decision = TournamentDecision(gate_override.decision)
        operator_override = True
        operator_override_reason = gate_override.reason
        # The rejection_reason field carries the override note on a forced
        # reject so the journal one-liner is legible; a forced promote clears
        # it (a promotion has no rejection reason).
        override_reason = (
            f"operator override: {gate_override.reason}"
            if gate_override.decision == "rejected"
            else ""
        )

    # --- 11. Persist outcome ---
    # "deferred" → treat as a non-promotion for evolve loop bookkeeping.
    bookkeeping_decision = "promoted" if decision == "promoted" else "rejected"
    parent_scalar = float(tournament_result.parent_agg.get("scalar", 0.0))
    child_scalar = float(tournament_result.child_agg.get("scalar", 0.0))
    _gen_fields = _generalization_fields(child_scalar, tournament_result)
    outcome_record = OutcomeRecord(
        ran_at=_now_iso(),
        drift_movements=(),  # detailed per-kind movements out-of-scope for v0
        pass_rate_delta=tournament_result.outcome.delta_pass_rate,
        drift_loss_delta=(
            float(tournament_result.child_agg.get("drift_loss_mean", 0.0))
            - float(tournament_result.parent_agg.get("drift_loss_mean", 0.0))
        ),
        scalar_score_delta=tournament_result.outcome.delta_scalar,
        tournament_decision=decision,
        rejection_reason=override_reason,
        # Operator override (control protocol, RUNTIME-V2 Phase 2): when an
        # operator force-promoted / force-rejected THIS generation, ``decision``
        # above is the forced verdict and these two fields make that explicit
        # in the journal/index so the override is never silent.
        operator_override=operator_override,
        operator_override_reason=operator_override_reason,
        # Record the structure the duel was decided under so the journal /
        # index carry it; gauntlet leaves the remaining fields at their
        # back-compat defaults (no bracket path to describe).
        structure=tournament_spec.structure,
        # The runner's champion-eval provenance is authoritative
        # (``full`` / ``fast`` / ``fast-degraded``); record it on the
        # gauntlet path too so a fast round is not journalled as ``full``.
        champion_eval_mode=tournament_result.champion_eval_mode,
        # The Ladder/holdout evidence block (OVERFITTING.md §12 #2) the runner
        # assembled for this duel — ``None`` when no holdout was consulted.
        # Journaled verbatim under the stable ``holdout`` key the dashboard
        # reads (see :func:`zicato.tournament.ladder.holdout_record`).
        holdout=getattr(tournament_result, "holdout", None),
        # Per-generation train/holdout loss + gap (OVERFITTING.md §12 #5).
        # ``train_loss`` is the child's TRAIN-slice scalar (the score that
        # gated it); ``holdout_loss`` its HOLDOUT-slice scalar (``None`` when
        # no holdout existed); ``generalization_gap`` their difference.
        train_loss=_gen_fields["train_loss"],
        holdout_loss=_gen_fields["holdout_loss"],
        generalization_gap=_gen_fields["generalization_gap"],
        # Evidence-gate resolution for the crowning duel (rating CIs +
        # ci_history), or ``None`` when the pre-gate is off / passed through
        # without a credible terminal.
        evidence=gate_evidence,
    )
    # --- 11b + 12 + 13. Persist outcome → index → lineage → journal ---
    # One shared write pipeline (`_finalize_generation`) for every round
    # tail. A rejected generation is still recorded in lineage (as a dead
    # branch) so the operator can see it in `zicato epoch list`; the
    # current-generation marker advances only on promotion.
    finalised_gen = Generation(
        id=next_id,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        snapshot_root=child_snapshot,
        created_at=child_gen.created_at,
        promoted=bookkeeping_decision == "promoted",
        round_index=child_gen.round_index,
    )
    _finalize_generation(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        outcome=outcome_record,
        lineage_generation=finalised_gen,
        lineage_parent_id=parent_id,
        # The duel's two numbers, recorded on the lineage node beside the
        # reason the gate gave (issue #124).
        lineage_parent_scalar=parent_scalar,
        lineage_child_scalar=child_scalar,
        advance_current_generation=bookkeeping_decision == "promoted",
    )
    # Post-promotion adapter hook (#125). Fired one statement after the
    # champion marker advanced inside `_finalize_generation` — the first
    # moment the promotion is durable — so a target whose real state lives
    # outside the mutable tree can fold the new champion into it. Fires on
    # the TRANSITION, so exactly once per settled promotion; best-effort,
    # so a failure yields a health finding rather than touching the round.
    on_promote_failure: tuple[str, str, str] | None = None
    if bookkeeping_decision == "promoted":
        on_promote_failure = await fire_on_promote(
            adapter,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            generation_id=next_id,
            parent_generation_id=parent_id,
            snapshot_root=child_snapshot,
        )
    # WS8: the round's terminal decision + provenance (overrides explicit).
    round_log.emit(
        "decision_recorded",
        {
            "decision": str(decision),
            "provenance": {
                "structure": tournament_spec.structure,
                "reason": outcome_record.rejection_reason,
                "operator_override": operator_override,
                "operator_override_reason": operator_override_reason,
                "parent_generation_id": parent_id,
                "promoted_generation_id": (next_id if bookkeeping_decision == "promoted" else None),
            },
        },
    )

    # --- 13a'. Pareto frontier RECORD (docs/design/PARETO-FRONTIER.md) ----
    # The scalar kept one generation; this records the one it discarded when
    # that generation beat the champion on an axis the weighted sum outvoted.
    # Placed here so the champion it evaluates against is the champion the
    # round actually ENDED with (post holdout confirmation, post integrity
    # block, post operator override). Record-only: it never touches the gate,
    # selection, the proposer, or the champion pointer, and it can never fail
    # a round.
    record_round_frontier(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        round_index=round_index,
        weights=weights,
        champion_generation_id=(next_id if bookkeeping_decision == "promoted" else parent_id),
        aggregates={
            parent_id: tournament_result.parent_agg,
            next_id: tournament_result.child_agg,
        },
        round_log=round_log,
    )

    # --- 13b. Optional random-baseline placebo arm (OVERFITTING.md #7) ---
    # One EXTRA scheduled duel after the settled round, on the opt-in
    # cadence ``overfitting.random_baseline_every_n``: champion vs a
    # semantics-preserving no-op copy of itself. Runs BEFORE the health
    # assessment below so a promoted placebo raises its CRITICAL finding
    # in THIS round's report. Best-effort; never advances the champion.
    await _maybe_run_placebo_arm_gauntlet(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        adapter=adapter,
        parent_gen=parent_gen,
        parent_id=parent_id,
        round_id=next_id,
        mutations=mutations,
        board=board,
        weights=weights,
        config=config,
        disable_drift=disable_drift,
        judge_only=judge_only,
        fast_mode=fast_mode,
        round_index=round_index,
        total_rounds=total_rounds,
    )

    # --- 14 + 15 + 16. Shared round epilogue ---
    # Per-round loop-health check + best-effort decision-telemetry
    # analyzer + best-effort epoch analysis report regeneration — the
    # same `_round_epilogue` the multi-challenger path runs, so a new
    # epilogue step can never land on one pipeline only.
    health_summary, health_critical = await _round_epilogue(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch_id,
        board=board,
        round_n=generation_phase.round_number(next_id) or round_index,
        analyzer_round=generation_phase.round_number(next_id),
        mutations=mutations,
        auxiliary_call_llm=auxiliary_call_llm,
        auxiliary_model=str(workspace_config.get("auxiliary_model", "")),
        meta_loop_emitter=meta_loop_emitter,
        token_clip=_token_clip_state(config),
        attributable_regressions=_promoted_entry_regressions(tournament_result),
        on_promote_failure=on_promote_failure,
    )

    _beat(
        beater,
        workspace_root=workspace_root,
        progress=(
            progress_log.PROMOTE if bookkeeping_decision == "promoted" else progress_log.REJECT
        ),
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{next_id}:{bookkeeping_decision}",
    )
    round_log.emit("round_closed")

    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=next_id,
        tournament_decision=bookkeeping_decision,
        rejection_reason=outcome_record.rejection_reason,
        parent_scalar=parent_scalar,
        child_scalar=child_scalar,
        delta_scalar=child_scalar - parent_scalar,
        health_summary=health_summary,
        health_critical=health_critical,
    )


# ---------------------------------------------------------------------------
# Multi-challenger field (non-gauntlet structures)
# ---------------------------------------------------------------------------
