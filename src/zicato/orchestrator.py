"""End-to-end evolve loop: one round, then N rounds.

The orchestrator is the integration point that ties together every
other zicato subsystem: it loads the workspace and the current epoch,
enumerates mutations and detects loss patterns, calls the proposer,
applies the resulting patches into a fresh snapshot, validates the
snapshot, runs the tournament, and persists the experiment with its
outcome. The CLI's ``zicato evolve`` command is a thin shell over
:func:`evolve_once` / :func:`evolve_n_rounds`.

Module imports are kept lightweight at top-level; heavier siblings
(:mod:`zicato.tournament.runner`, :mod:`zicato.mutation.applier`,
:mod:`zicato.proposer.proposer`, :mod:`zicato.patterns.detectors`)
are imported inside the body of each helper. This keeps ``zicato
--help`` fast even on a workspace whose runtime extras (goldfive,
google-adk) are not installed.

Two public entry points:

* :func:`evolve_once` — one round. Returns the
  :class:`EvolveRoundOutcome` describing what happened.
* :func:`evolve_n_rounds` — call ``evolve_once`` up to ``rounds``
  times. Bails out early after a configurable number of consecutive
  rejections (default 3) — that's a sign the proposer is stuck and
  the operator wants to look before spending more LLM calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    PriorExperiment,
    TournamentDecision,
)
from zicato.core.workspace import (
    experiment_json_path,
    generation_dir,
)
from zicato.evolve.dashboard_projection import (
    _clear_active_tournament,
    _field_entries,
    _mark_run_terminal,
    _overlay_projected_live_progress,
    _overlay_projected_standings,
    _persist_field_tournament,
    _publish_active_tournament,
    _serialise_rounds,
    _serialise_standings,
    _settle_active_tournament,
)
from zicato.evolve.epoching import (
    _component_diff_label,
    _create_epoch_from_contract,
    _promoted_head_snapshot,
    _roll_seed_marker,
    _stored_component_hashes,
    _write_component_hashes,
    ensure_epoch_for_contract,
)
from zicato.evolve.gate import (
    _apply_field_overrides,
    _confirm_crowning_on_holdout,
    _confirm_gauntlet_promotion,
    _CrowningHoldout,
    _first_aggregate_for,
    _gauntlet_decision_from_result,
    _integrity_block_reason,
    _registered_mutable_trees,
    _resolve_round_champion_mode,
)
from zicato.evolve.ingest import (
    _cache_gen_score,
    _index_db_path,
    _ingest_experiment_into_index,
    _load_prior_experiments,
)
from zicato.evolve.lifecycle_services import (
    _beat,
    _build_meta_loop_emitter_safe,
    _EnvVarRestorer,
    _LaunchedHandle,
    _NoopShutdownHandle,
    _now_iso,
    _resolve_harmonograf_url,
    _resolve_or_launch_harmonograf,
)
from zicato.evolve.persist import (
    _finalize_generation,
    _persist_rejected_round,
    _rejected_proposer_experiment,
    _round_epilogue,
    _skipped_round_outcome,
)
from zicato.evolve.propose_apply import (
    _AppliedChallenger,
    _diversity_signature,
    _max_overlap_with_accepted,
    _maybe_run_placebo_arm_gauntlet,
    _mint_challenger_field,
    _mint_placebo_challenger,
    _propose_and_apply_challenger,
    _propose_child,
    _trim_reason,
)
from zicato.evolve.round import (
    build_post_apply_validator,
    build_scratch_validator_factory,
    check_patch_manifest_and_forbidden,
)
from zicato.evolve.round_context import (
    _build_calibration_summary,
    _build_candidate_screen_runner,
    _build_genealogy_items,
    _build_recombination_pair,
    _recombine_pair_for_slot,
)
from zicato.runtime.control_consumer import (
    GateOverride,
    block_while_paused,
    claim_field_gate_overrides,
    claim_gate_override,
    claim_rubric_replacement,
    claim_skip_round,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.resume import ResumePlan
from zicato.util import best_effort
from zicato.workspace import WorkspaceLayout

if TYPE_CHECKING:
    # Annotation-only — the proposer module is imported lazily inside
    # ``evolve_once`` (see the module docstring on lazy imports), so its
    # exception type is referenced here purely for type annotations.
    from zicato.proposer.agent import ProposerAgent
    from zicato.proposer.best_of_n import ScreenRunner
    from zicato.proposer.proposer import ProposerError

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

#: The :attr:`EvolveRoundOutcome.tournament_decision` value for a round the
#: endpoint-outage circuit DEFERRED (WS-H;
#: :attr:`~zicato.core.runtime.RuntimeConfig.infra_abort_round_threshold`).
#: Distinct from ``"rejected"`` on purpose: the evolve loop must not count
#: it toward the consecutive-rejection breaker (the experiment was never
#: judged), and it backs off + re-reconciles instead of re-proposing.
DEFERRED_INFRA_DECISION = "deferred_infra"


@dataclass(frozen=True, slots=True)
class EvolveRoundOutcome:
    """One round's summary, returned by :func:`evolve_once`.

    Fields
    ------
    parent_generation_id:
        Lineage head this round challenged.
    proposed_generation_id:
        Id assigned to the child generation the proposer produced.
    tournament_decision:
        ``"promoted"`` or ``"rejected"``. ``"deferred"`` is mapped to
        ``"rejected"`` for the orchestrator's bookkeeping — the
        evolve loop only advances on promotions.
        :data:`DEFERRED_INFRA_DECISION` (``"deferred_infra"``) is the ONE
        additional value: the endpoint-outage circuit tripped, nothing
        was journaled (the experiment persists un-outcomed for the
        crash-resume reconciliation), and the loop backs off before the
        next round.
    rejection_reason:
        Symbolic / human-readable string when the round did not
        promote. Empty string on a successful promotion.
    parent_scalar:
        Parent generation's scalar score (drift + pass terms weighted).
    child_scalar:
        Child generation's scalar score.
    delta_scalar:
        ``child_scalar - parent_scalar``. Negative = improvement.
    health_summary:
        One-line summary of the round's loop-health assessment (see
        :func:`zicato.health.diagnostics.assess_loop_health`). Empty
        string when the health sibling is unavailable or the assessment
        could not be run — the round's outcome is unaffected either way.
    health_critical:
        ``True`` when the round's loop-health assessment surfaced at
        least one CRITICAL finding (e.g. degenerate scoring producing no
        signal). ``False`` otherwise, including when no assessment ran.
    """

    parent_generation_id: str
    proposed_generation_id: str
    tournament_decision: str
    rejection_reason: str
    parent_scalar: float
    child_scalar: float
    delta_scalar: float
    health_summary: str = ""
    health_critical: bool = False


def _declared_custom_judge_names(board: list[Any], weights: Any) -> frozenset[str]:
    """Return the names of the custom judges declared by the contract.

    A custom judge is addressable in a proposer hypothesis as a
    ``drift:<judge_name>`` metric even though, on the goldfive side, it
    emits under the single ``"custom"`` drift kind. The set of valid
    judge names is the union of:

    * every ``JudgeSpec.name`` on every board entry (``board[*].judges``);
    * every key of :attr:`ScoringWeights.per_judge_weights`.

    Threaded into :func:`zicato.proposer.proposer.propose_experiment` so
    the hypothesis validator accepts ``drift:<judge_name>`` for a declared
    judge and still rejects a genuinely-unknown drift kind.
    """
    names: set[str] = set()
    for entry in board:
        for judge in getattr(entry, "judges", ()) or ():
            judge_name = getattr(judge, "name", None)
            if judge_name:
                names.add(str(judge_name))
    per_judge = getattr(weights, "per_judge_weights", None) or {}
    names.update(str(k) for k in per_judge)
    return frozenset(names)


# ---------------------------------------------------------------------------
# Contract-hash auto-epoching
# ---------------------------------------------------------------------------
# The roll-at-evolve-time decision and its helpers live in
# ``zicato.evolve.epoching``; re-exported above so callers (and the test
# suite's ``orch.ensure_epoch_for_contract`` monkeypatch) keep working.


# ---------------------------------------------------------------------------
# evolve_once
# ---------------------------------------------------------------------------


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
) -> EvolveRoundOutcome:
    """Run ONE evolve round against the current epoch.

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
    from zicato.epoch import load_epoch, write_experiment  # noqa: PLC0415
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.patterns.detectors import (  # noqa: PLC0415
        ALL_DETECTORS,
        DetectorInput,
        detect_patterns,
    )
    from zicato.proposer.agent import build_proposer_agent  # noqa: PLC0415
    from zicato.proposer.proposer import ProposerError  # noqa: PLC0415
    from zicato.proposer.skills import resolve_proposer_spec  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415
    from zicato.tournament.runner import (  # noqa: PLC0415
        run_fast_mode,
        run_tournament,
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
        parent_for_skip = _safe_resolve_parent(workspace_root, resolved_epoch_id)
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
    proposer_spec = resolve_proposer_spec(_epoch_cfg.proposer_path)
    # Thread the frozen ``proposer_path`` so a custom-agent spec (Design A)
    # can load ``proposers/<name>/agent.py`` from the same dir the spec was
    # resolved from. ``None`` (the default / skill-only proposer) yields the
    # single-shot built-in unchanged.
    proposer_agent = build_proposer_agent(proposer_spec, proposer_path=_epoch_cfg.proposer_path)
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
    parent_id = _resolve_current_generation(workspace_root, resolved_epoch_id)
    parent_gen = Generation(
        id=parent_id,
        epoch_id=resolved_epoch_id,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace_root, resolved_epoch_id, parent_id),
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
    )

    # --- 2a'. Achievable-signal contract pre-flight (epoch-open step) -----
    # DEFAULT-ON (issue #84): unless runtime.preflight_gate == "off", measure
    # the A/A floor AND the achievable signal (champion vs a deliberately-
    # degraded ephemeral copy of itself) once per epoch and persist the
    # verdict. A below-floor / saturated / inert verdict — or a promote_margin
    # outside the noise < margin < achievable window (issue #112) — is LOUDLY
    # warned (inside the helper) and flows into the per-round health report.
    # Under the opt-in HARD gate (preflight_gate == "refuse") a refuse-worthy
    # verdict STOPS the run here — before rounds burn budget on a contract that
    # cannot be optimized, or on a margin nothing can clear.
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
    )
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_REFUSE,
        PreflightRefusedError,
    )

    if (
        str(getattr(config, "preflight_gate", "warn") or "warn") == "refuse"
        and _preflight_verdict == VERDICT_REFUSE
    ):
        from zicato.health.inputs import epoch_preflight_record  # noqa: PLC0415

        # Name WHICH refusal: "noise swamps the signal" and "the margin is
        # above anything the loop can produce" are both guaranteed-null runs
        # with entirely different fixes, so a single generic message would send
        # half of them to the wrong one.
        _record = epoch_preflight_record(workspace_root, resolved_epoch_id) or {}
        _cause = (
            "the configured promote_margin sits at or above the contract's "
            "measured achievable signal, so barring a compound patch no "
            "challenger could be promoted and the run would be null by "
            "construction. Lower promote_margin inside the window "
            "(noise < margin < achievable) — or, if the margin is deliberately "
            "above what a SINGLE mutation point can move (e.g. recombination is "
            "meant to union two sub-margin fixes), set "
            "runtime.preflight_gate='warn'"
            if str(_record.get("window_failure") or "") == "margin_above_achievable"
            else "the contract's achievable signal is at or below its measured "
            "A/A noise floor, so every duel would be decided by noise. "
            "Strengthen the board / reduce evaluation noise"
        )
        raise PreflightRefusedError(
            f"contract pre-flight REFUSE for epoch {resolved_epoch_id}: {_cause}. "
            "Refusing the run before it spends rounds "
            "(runtime.preflight_gate='refuse'); set runtime.preflight_gate='warn' "
            "to proceed anyway."
        )

    # --- 2b. Margin-vs-noise-floor sanity check (once per invocation) -----
    # When a measured floor exists and the contract's promote_margin sits
    # inside it, say so loudly at evolve start — a duel decided by the margin
    # alone cannot distinguish a real improvement from an A/A re-roll. Never
    # hard-refuses; the per-round health report carries the matching finding.
    if round_index == 0:
        _warn_margin_below_noise_floor(workspace_root, resolved_epoch_id)

    # --- 3. Mutations ---
    mutations = enumerate_mutations(_resolve_mutable_trees(adapter, parent_gen.snapshot_root))
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

    # --- 5. Loss summary ---
    loss_summary = _render_loss_summary(losses)

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

    # Inject the epoch's board entry ids as the default ``board_ids`` so a
    # board-aware structure (racing) slices the full epoch board out of the
    # box; an explicit ``params["board_ids"]`` still overrides. Board-agnostic
    # structures (gauntlet, single/double-elim, swiss) ignore the param.
    strategy = make_strategy(tournament_spec, board_ids=[e.id for e in board])
    if strategy.field_size() > 1:
        return await _evolve_multi_challenger(
            screen_candidates=screen_candidates,
            recombine_pair=recombine_pair,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            tournament_spec=tournament_spec,
            strategy=strategy,
            parent_id=parent_id,
            adapter=adapter,
            board=board,
            weights=weights,
            brief=brief,
            config=config,
            mutations=mutations,
            patterns=patterns,
            loss_summary=loss_summary,
            failure_profile=failure_profile,
            process_exemplars=process_exemplars_block,
            genealogy=genealogy_items,
            calibration=calibration_summary,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast_mode=fast_mode,
            auxiliary_call_llm=auxiliary_call_llm,
            workspace_config=workspace_config,
            max_proposer_retries=max_proposer_retries,
            beater=beater,
            round_index=round_index,
            total_rounds=total_rounds,
            meta_loop_emitter=meta_loop_emitter,
            proposer_agent=proposer_agent,
            round_log=round_log,
        )

    # --- 6. Propose ---
    # When resuming an interrupted round in place (runtime/resume.py), reuse
    # the SAME generation id the prior run minted — its directory still
    # exists, so ``_next_generation_id`` would otherwise skip past it to a
    # fresh vN+1 and orphan the completed loss.json units. Every other path
    # (cold start, discarded partial, post-resume rounds) picks the next
    # fresh id exactly as before.
    if (
        resume_plan is not None
        and resume_plan.resumes_in_place
        and resume_plan.resume_generation_id is not None
    ):
        next_id = resume_plan.resume_generation_id
    else:
        next_id = _next_generation_id(workspace_root, resolved_epoch_id)
    from zicato.runtime import progress_log  # noqa: PLC0415

    _beat(
        beater,
        workspace_root=workspace_root,
        progress=progress_log.PROPOSE,
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"proposing:round_{round_index}:{next_id}",
    )
    # The mutation-applier seam: the patch set is applied here so the
    # post-apply validator can see the real child tree. Materialised
    # once, reused for the tournament if validation passes.
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    genstore = default_generation_store(workspace_root)

    # --- 6a. Post-apply validation hook ---
    # A destructive proposer patch (one that drops imports, breaks
    # Python syntax, or removes a ``# zicato:mutable`` marker) used to
    # cost an entire wasted tournament round: the orchestrator applied
    # the patch, ran the validator, and rejected with no retry. Instead
    # we hand the proposer a validation hook so a post-apply failure is
    # a *retryable* feedback class — the proposer re-proposes with the
    # concrete validator strings in its prompt, within the same bounded
    # ``max_proposer_retries`` budget the parse-error retries already
    # share, so the per-run wall-clock budget is still honoured.
    #
    # The hook applies the candidate patch set into the child snapshot
    # and runs :func:`validate_post_apply`. ``last_child_snapshot``
    # captures the child tree of the last attempt — when the proposer
    # returns successfully it is the validated tree the tournament
    # mounts; no second apply is needed. The hook itself is the shared
    # ``build_post_apply_validator`` (``zicato.evolve.round``) the field
    # path also uses — the previously-inlined closure was byte-identical.
    last_child_snapshot: dict[str, Path] = {}
    _validate_experiment_post_apply = build_post_apply_validator(
        genstore=genstore,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        next_id=next_id,
        mutations=mutations,
        beater=beater,
        round_index=round_index,
        last_child_snapshot=last_child_snapshot,
    )
    # WS-CONC: the per-slot scratch-validator factory — each best-of-N slate
    # slot leases a FRESH disjoint scratch tree from this so the samples can
    # gather without racing on the shared ``next_id`` derive above. Threaded
    # onto the ProposerContext; the shared validator above still does the ONE
    # canonical ``next_id`` derive for the chosen candidate after selection.
    _scratch_validator_factory = build_scratch_validator_factory(
        genstore=genstore,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        next_id=next_id,
        mutations=mutations,
        beater=beater,
        round_index=round_index,
    )

    # Experiment memory: the settled cross-round digest for this epoch.
    # Best-effort — a missing / stale index yields an empty list and the
    # proposer simply runs without the ``## What's already been tried``
    # section. The gauntlet field is a single challenger, so there are no
    # in-flight siblings to concatenate here.
    prior = _load_prior_experiments(
        workspace_root,
        resolved_epoch_id,
        cross_epoch=weights.experiment_memory.cross_epoch,
    )

    proposer_validation_failed: ProposerError | None = None
    # --- 6r. Conservative crash-resume short-circuit (gauntlet path) ---
    # When the prior evolve was interrupted mid-tournament on THIS exact
    # generation, runtime/resume.py validated that the persisted
    # experiment + patches + snapshot are self-consistent and at least one
    # board unit completed. Reuse the persisted experiment verbatim rather
    # than re-proposing (the proposer is non-deterministic — a fresh
    # proposal would invalidate the on-disk loss.json cache). We still run
    # the SAME validate/derive hook once, so the snapshot is re-derived
    # from those same patches (idempotent) before the tournament; the unit
    # cache then HITs every entry that already has a loss.json. ``None``
    # (the common cold-start case) leaves the propose path untouched.
    resumed_experiment: Experiment | None = None
    if (
        resume_plan is not None
        and resume_plan.resumes_in_place
        and resume_plan.resume_generation_id == next_id
        and resume_plan.resume_experiment is not None
    ):
        candidate = replace(resume_plan.resume_experiment, round_index=round_index)
        # Re-derive the child snapshot from the persisted patches so the
        # tournament mounts the same tree the interrupted run scored. The
        # hook clears any stale child tree and re-applies all-or-nothing.
        resume_errors = await _validate_experiment_post_apply(candidate)
        if resume_errors:
            # The persisted patches no longer re-derive cleanly (e.g. the
            # parent tree changed underneath them). Fall back to the
            # conservative default: re-propose fresh, exactly as a cold
            # start would — never score against a tree we cannot rebuild.
            log.warning(
                "resume: persisted patches for %s failed re-validation (%s); "
                "discarding and re-proposing fresh",
                next_id,
                "; ".join(resume_errors),
            )
        else:
            log.info("resume: reusing persisted experiment for %s (no re-propose)", next_id)
            resumed_experiment = candidate

    if resumed_experiment is not None:
        # Skip the proposer entirely; the validate/derive hook above
        # already populated ``last_child_snapshot``. Fall through to the
        # shared step-7+ path below with the persisted experiment.
        experiment = resumed_experiment
    else:
        try:
            experiment = await _propose_child(
                proposer_agent=proposer_agent,
                epoch_id=resolved_epoch_id,
                parent_id=parent_id,
                next_id=next_id,
                patterns=patterns,
                mutations=mutations,
                brief=brief,
                loss_summary=loss_summary,
                auxiliary_call_llm=auxiliary_call_llm,
                auxiliary_model=str(workspace_config.get("auxiliary_model", "")),
                max_proposer_retries=max_proposer_retries,
                workspace_root=workspace_root,
                validate_experiment=_validate_experiment_post_apply,
                meta_loop_emitter=meta_loop_emitter,
                custom_judge_names=custom_judge_names,
                prior_experiments=tuple(prior),
                restrict_visibility=weights.overfitting.restrict_proposer_visibility,
                failure_profile=failure_profile,
                process_exemplars=process_exemplars_block,
                genealogy=genealogy_items,
                calibration=calibration_summary,
                round_index=round_index,
                round_emitter=round_log,
                screen_candidates=screen_candidates,
                recombine_pair=recombine_pair,
                scratch_validator_factory=_scratch_validator_factory,
            )
        except ProposerError as exc:
            # The proposer exhausted its bounded retries without producing
            # a patch set that survives post-apply validation (or parsing).
            # Fall through to the rejected-outcome path rather than crashing
            # the round — the round still produces a clean ``rejected``
            # journal entry, and the loop continues.
            proposer_validation_failed = exc
            experiment = None

    # --- 7. Validate patch set against the manifest ---
    if experiment is not None:
        check_patch_manifest_and_forbidden(experiment, mutations, brief.forbidden_ids)

    # --- 8 + 9. Apply + post-apply validation ---
    # The proposer's validation hook already applied the (final,
    # validated) patch set and ran :func:`validate_post_apply`. When the
    # proposer exhausted its bounded retries without producing a patch
    # set that survives post-apply validation, ``proposer_validation_failed``
    # carries the accumulated per-attempt errors and there is no
    # surviving experiment to score — record a rejection so a
    # destructive-proposer round still leaves a clean, append-only
    # journal entry instead of crashing the loop.
    if proposer_validation_failed is not None:
        experiment = _rejected_proposer_experiment(
            resolved_epoch_id, parent_id, next_id, proposer_validation_failed
        )
        validation_errors = list(proposer_validation_failed.attempts)
        child_snapshot = last_child_snapshot.get(
            "path", _snapshot_root(workspace_root, resolved_epoch_id, next_id)
        )
    else:
        assert experiment is not None  # narrowed: no ProposerError above
        # The hook stores the validated child tree; it always runs at
        # least once before a successful return.
        child_snapshot = last_child_snapshot["path"]
        validation_errors = []

    # --- 9. Act on validation outcome ---
    if validation_errors:
        assert experiment is not None  # narrowed: rejected placeholder above
        return await _persist_rejected_round(
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            experiment=experiment,
            validation_errors=validation_errors,
            proposer_retries_exhausted=proposer_validation_failed is not None,
            board=board,
            round_index=round_index,
            auxiliary_call_llm=auxiliary_call_llm,
            auxiliary_model=str(workspace_config.get("auxiliary_model", "")),
            beater=beater,
            round_log=round_log,
        )

    # --- 10. Run the tournament ---
    write_experiment(workspace_root, resolved_epoch_id, next_id, experiment)
    # Live index dual-write: the proposer-side experiment.json (outcome
    # still None) is on disk — fold it in so the index reflects the
    # in-progress generation before the tournament finishes.
    _ingest_experiment_into_index(workspace_root, resolved_epoch_id, next_id)
    _beat(
        beater,
        workspace_root=workspace_root,
        progress=progress_log.TOURNAMENT_START,
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"tournament:round_{round_index}:{next_id}",
    )

    child_gen = Generation(
        id=next_id,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        snapshot_root=child_snapshot,
        created_at=_now_iso(),
        round_index=round_index,
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
    from zicato.scoring.diff_complexity import diff_size as _diff_size  # noqa: PLC0415

    child_diff_size = _diff_size(experiment)
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

    # Cache gen_score.json for future fast-mode runs.
    _cache_gen_score(workspace_root, resolved_epoch_id, parent_id, tournament_result.parent_agg)
    _cache_gen_score(workspace_root, resolved_epoch_id, next_id, tournament_result.child_agg)

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
        decision = gate_override.decision
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
        advance_current_generation=bookkeeping_decision == "promoted",
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
        round_n=_round_n_from_generation_id(next_id) or round_index,
        analyzer_round=_round_n_from_generation_id(next_id),
        mutations=mutations,
        auxiliary_call_llm=auxiliary_call_llm,
        auxiliary_model=str(workspace_config.get("auxiliary_model", "")),
        meta_loop_emitter=meta_loop_emitter,
        token_clip=_token_clip_state(config),
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


async def _evolve_multi_challenger(
    *,
    workspace_root: Path,
    epoch_id: str,
    tournament_spec: Any,
    strategy: Any,
    parent_id: str,
    adapter: Any,
    board: list[Any],
    weights: Any,
    brief: Any,
    config: Any,
    mutations: list[Any],
    patterns: list[Any],
    loss_summary: str,
    failure_profile: str,
    process_exemplars: str,
    genealogy: tuple[Any, ...] = (),
    calibration: Any = None,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    fast_mode: bool,
    auxiliary_call_llm: CallLLM,
    workspace_config: Any,
    max_proposer_retries: int,
    beater: HeartbeatBeater | None,
    round_index: int,
    total_rounds: int,
    meta_loop_emitter: Any,
    proposer_agent: ProposerAgent,
    round_log: _RoundLogEmitter | None = None,
    screen_candidates: ScreenRunner | None = None,
    recombine_pair: Any = None,
) -> EvolveRoundOutcome:
    """Run ONE evolve round under a non-gauntlet tournament structure.

    The structure's :meth:`SelectionStrategy.field_size` challengers are
    proposed and applied (each a lineage child of the current champion),
    then the strategy's matchups are driven through
    :func:`zicato.selection.resolve_tournament`. Each matchup runs via the
    same board-unit runner (:func:`zicato.tournament.runner.run_matchup`)
    and ends in the UNCHANGED promote gate; the strategy reads the gate
    verdict and never re-decides a duel. On resolution the crowned
    generation (if any) advances the champion, every rejected challenger
    is recorded as a dead branch, and the live ``ActiveTournament``
    envelope + per-challenger ``OutcomeRecord`` audit + v3 index columns
    are persisted per ``docs/design/TOURNAMENT-DATA-MODEL.md``.

    ``fast_mode`` is the RUNTIME champion-eval knob (the ``--mode fast``
    setting), threaded identically to ``disable_drift`` / ``judge_only``.
    When set, every matchup reuses the champion's cached per-board
    scalars instead of re-running the champion (see
    :func:`zicato.tournament.runner.run_matchup`), so fast mode is
    structure-agnostic — it composes with racing / swiss / elim exactly
    as it does with the gauntlet. The resolved champion-eval mode is
    RECORDED in the journal for provenance (it is never a contract
    input, so flipping fast↔full does not roll the epoch).
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
    from zicato.core.types import MatchOutcome  # noqa: PLC0415
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
        update_experiment_outcome,
    )
    from zicato.selection import EvidencePreGate, resolve_tournament  # noqa: PLC0415
    from zicato.selection.dead_letter import (  # noqa: PLC0415
        InconclusiveRecord,
        record_inconclusive,
    )
    from zicato.selection.driver import EvidenceResolution  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        EVIDENCE_REPLICATE_BASE,
        rating_block,
        read_promote_confidence_threshold,
        read_replicate_budget,
    )
    from zicato.selection.strategy import (  # noqa: PLC0415
        Contestant,
        Matchup,
        MatchupResult,
    )
    from zicato.tournament.runner import confirm_crowning_holdout, run_matchup  # noqa: PLC0415

    auxiliary_model = str(workspace_config.get("auxiliary_model", ""))
    field_n = strategy.field_size()
    # WS8: a direct caller (tests) may not thread the opened emitter; bind
    # one so every emit below is uniformly best-effort. ``evolve_once`` (the
    # production caller) passes the emitter it already opened the round on.
    if round_log is None:
        round_log = _RoundLogEmitter(workspace_root, epoch_id, round_index)

    # TRAIN/HOLDOUT split (OVERFITTING.md §3/§4). The structure's internal
    # matchups (swiss rounds, elim nodes, racing rungs) — INCLUDING the final
    # champion-gate duel that decides promotion — score on the TRAIN slice
    # only, so the holdout is never consumed to *pick* the leader (mirrors the
    # gauntlet, which selects on train). The full board is retained for the
    # one Ladder-mediated holdout confirmation run after resolution. When the
    # holdout is empty (small board / split disabled / no tagged entry) the
    # train slice IS the full board, so every matchup runs on the full board
    # and the whole path is byte-identical to today's whole-board behaviour.
    _train_seed = rotation_seed(weights.overfitting, epoch_id)
    _train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=_train_seed)
    _train_id_set = set(_train_ids)
    train_board = [e for e in board if e.id in _train_id_set]

    # --- Propose + apply the N-challenger field. Ids are minted in
    # sequence so each challenger is a distinct vN child of the champion;
    # a proposer that fails for one challenger simply narrows the field.
    applied: list[_AppliedChallenger] = []
    # Per-challenger proposing-step outcomes (applied vs rejected + reason),
    # collected in mint order so the dashboard's proposing-step tracker can
    # render the field forming live and post-hoc. Persisted onto the live
    # ActiveTournament envelope (publish + settle) as ``field_status``.
    field_status: list[dict[str, Any]] = []
    # Mint ids monotonically from the highest existing vN so every
    # challenger gets a distinct id even when a proposer attempt fails
    # before it derives a snapshot (so _next_generation_id can't re-pick
    # the same vN). The first id matches what the gauntlet path would mint.
    base_id = _next_generation_id(workspace_root, epoch_id)
    base_n = _round_n_from_generation_id(base_id)
    custom_judge_names = _declared_custom_judge_names(board, weights)
    # Experiment memory: the settled cross-round digest, computed ONCE
    # before the field is minted (it does not change as siblings apply).
    # ``siblings`` accumulates an in-flight ``PriorExperiment`` per
    # successfully-applied challenger so challenger k sees the hypotheses
    # of challengers 0..k-1 this round and can diversify away from them; a
    # challenger whose proposer failed contributes no sibling.
    prior = tuple(
        _load_prior_experiments(
            workspace_root,
            epoch_id,
            cross_epoch=weights.experiment_memory.cross_epoch,
        )
    )
    siblings: list[PriorExperiment] = []
    # Field-diversity constraint (FUNCTIONALITY-RECOMMENDATIONS.md §4.3): the
    # signatures of the siblings already minted this round, so a later
    # challenger that duplicates one (same modulating id-set + core idea) is
    # soft-rejected instead of collapsing the field (EXPERIMENT-MEMORY.md
    # §2.2). Localized to this loop; no change to the SelectionStrategy.
    sibling_signatures: list[tuple[frozenset[str], str]] = []
    # Opt-in field-diversity OVERLAP enforcement (FUNCTIONALITY-
    # RECOMMENDATIONS.md §4.3): when ``config.diversity_tolerance`` is set, a
    # challenger whose targeted-mutation-id set overlaps an already-ACCEPTED
    # sibling's by a Jaccard ratio strictly greater than the tolerance is
    # soft-rejected (it would collapse a field of N into fewer real
    # experiments). ``accepted_mutation_sets`` tracks the mutation-id set of
    # each kept challenger in mint order so the overlap is measured against
    # exactly the slate that will actually run. ``None`` ⇒ enforcement OFF,
    # and every field-status record below stays byte-identical to today (no
    # ``diversity_status`` key is emitted). Enforcement composes with — and
    # runs AFTER — the exact-duplicate soft-reject above.
    diversity_tolerance = getattr(config, "diversity_tolerance", None)
    accepted_mutation_sets: list[frozenset[str]] = []
    diversity_soft_rejected = 0
    # The LIVE field-status map, keyed by generation_id, rewritten as each
    # challenger slot enters ("proposing"), retries, and settles
    # ("applied"/"rejected"). The orchestrator publishes a "proposing"-phase
    # ActiveTournament envelope on every status transition so the dashboard's
    # proposing tracker updates as each challenger is attempted — not only
    # after the whole field is minted. (OBSERVABILITY: the operator's "most
    # of what happens during proposal phase is opaque" pain.)
    live_status: dict[str, dict[str, Any]] = {}
    proposing_tournament_id = f"tourn_{epoch_id}_{base_id}"

    from zicato.runtime.state import TournamentPhase  # noqa: PLC0415

    def _publish_proposing(record: dict[str, Any]) -> None:
        live_status[str(record.get("generation_id", ""))] = dict(record)
        ordered = list(live_status.values())
        champion_only = [{"generation_id": parent_id, "seed": 1, "role": "champion"}]
        _publish_active_tournament(
            workspace_root,
            tournament_id=proposing_tournament_id,
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=champion_only,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=ordered,
            phase=TournamentPhase.PROPOSING,
            entries=_field_entries(champion_only),
        )

    def _persist_soft_reject(generation_id: str, reason: str, detail: str = "") -> None:
        # A field-diversity soft-reject drops the challenger from the run slate
        # during proposing. Persist a terminal REJECTED outcome onto its
        # ``experiment.json`` (written at proposal with ``outcome=None``) so the
        # canonical generation record — and the round-grouped lineage tree that
        # reads it — show "rejected" consistently with the live proposed-field
        # hero, instead of a stale "pending". The ``detail`` (the overlap ratio +
        # the peer sibling + the tolerance, or the duplicate explanation) is
        # folded into ``rejection_reason`` so the candidate documents WHY it was
        # cut — not just the bare "field_diversity_overlap" code. Mirrors the
        # validator path's ``"<code>: <explanation>"`` reason shape. Best-effort:
        # a missing / locked record must never abort proposing the rest of field.
        full_reason = f"{reason}: {detail}" if detail else reason
        with best_effort(f"persist soft-reject outcome for {generation_id}"):
            update_experiment_outcome(
                workspace_root,
                epoch_id,
                generation_id,
                OutcomeRecord(
                    ran_at=_now_iso(),
                    drift_movements=(),
                    pass_rate_delta=0.0,
                    drift_loss_delta=0.0,
                    scalar_score_delta=0.0,
                    tournament_decision=TournamentDecision.REJECTED,
                    rejection_reason=full_reason,
                ),
            )
            _ingest_experiment_into_index(workspace_root, epoch_id, generation_id)

    for offset in range(field_n):
        next_id = f"v{base_n + offset}" if base_n is not None else base_id
        # Seed mirrors competitors_meta: champion is seed 1, challengers
        # follow in mint order (seed 2, 3, …) regardless of apply outcome.
        challenger, status = await _propose_and_apply_challenger(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            mutations=mutations,
            patterns=patterns,
            brief=brief,
            loss_summary=loss_summary,
            auxiliary_call_llm=auxiliary_call_llm,
            auxiliary_model=auxiliary_model,
            max_proposer_retries=max_proposer_retries,
            beater=beater,
            round_index=round_index,
            meta_loop_emitter=meta_loop_emitter,
            seed=offset + 2,
            custom_judge_names=custom_judge_names,
            prior_experiments=prior + tuple(siblings),
            proposer_agent=proposer_agent,
            restrict_visibility=weights.overfitting.restrict_proposer_visibility,
            failure_profile=failure_profile,
            process_exemplars=process_exemplars,
            genealogy=genealogy,
            calibration=calibration,
            on_status=_publish_proposing,
            round_emitter=round_log,
            screen_candidates=screen_candidates,
            recombine_pair=_recombine_pair_for_slot(recombine_pair, offset),
        )
        # Field-diversity DECISION (pure — `_mint_challenger_field`): accept
        # the challenger into the run slate, or soft-reject it as an exact
        # duplicate of an in-flight sibling / an over-tolerance overlap with
        # an accepted sibling. The persistence + publish I/O for a rejected
        # slot stays here, separated from the decision.
        mint = (
            _mint_challenger_field(
                challenger.experiment,
                sibling_signatures,
                accepted_mutation_sets,
                diversity_tolerance,
            )
            if challenger is not None
            else None
        )
        if challenger is not None and mint is not None and mint.action == "reject_duplicate":
            # Field-diversity soft-reject: this challenger duplicates an
            # already-minted sibling (same modulating id-set + core idea), so
            # it would collapse the field. Drop it from the run slate and
            # record a legible rejected field-status — the SelectionStrategy
            # still resolves over the distinct challengers that remain.
            hyp = challenger.experiment.hypothesis
            dup_status = {
                "generation_id": challenger.generation_id,
                "status": "rejected",
                "reason": "field_diversity_duplicate",
                "attempts": int(status.get("attempts", 0)) if isinstance(status, dict) else 0,
                "attempt_reasons": [
                    "duplicates an in-flight sibling (same modulating ids + core idea); "
                    "soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
            }
            # The per-slot diversity status is only stamped when overlap
            # enforcement is active, so the default-off path's duplicate record
            # is byte-identical to today.
            if diversity_tolerance is not None:
                dup_status["diversity_status"] = "soft_rejected"
                dup_status["diversity_tolerance"] = diversity_tolerance
                diversity_soft_rejected += 1
            field_status.append(dup_status)
            _publish_proposing(dup_status)
            log.info(
                "multi-challenger field: %s/%s duplicates an in-flight sibling "
                "(modulating=%s); soft-rejected for field diversity",
                epoch_id,
                challenger.generation_id,
                tuple(hyp.modulating),
            )
            _persist_soft_reject(
                challenger.generation_id,
                "field_diversity_duplicate",
                "duplicates an in-flight sibling (same mutation ids + core idea)",
            )
            continue
        # Opt-in overlap soft-reject: when a tolerance is configured, drop a
        # challenger whose mutation-id set overlaps an accepted sibling beyond
        # the ceiling. Skipped entirely (no key emitted) when tolerance is
        # None, so the default path is byte-identical.
        if challenger is not None and mint is not None and mint.action == "reject_overlap":
            overlap, peer_idx = mint.overlap, mint.overlap_peer_index
            assert diversity_tolerance is not None  # narrowed: overlap fires only with a tolerance
            hyp = challenger.experiment.hypothesis
            peer_gid = applied[peer_idx].generation_id if 0 <= peer_idx < len(applied) else ""
            overlap_status = {
                "generation_id": challenger.generation_id,
                "status": "rejected",
                "reason": "field_diversity_overlap",
                "diversity_status": "soft_rejected",
                "attempts": (int(status.get("attempts", 0)) if isinstance(status, dict) else 0),
                "attempt_reasons": [
                    f"mutation-id overlap {overlap:.3f} with sibling "
                    f"{peer_gid or '(accepted)'} exceeds diversity_tolerance "
                    f"{diversity_tolerance:.3f}; soft-rejected to keep the field diverse"
                ],
                "hypothesis": _trim_reason(hyp.core_idea),
                "seed": offset + 2,
                "overlap": round(overlap, 6),
                "overlap_peer": peer_gid,
                "diversity_tolerance": diversity_tolerance,
            }
            field_status.append(overlap_status)
            _publish_proposing(overlap_status)
            diversity_soft_rejected += 1
            log.info(
                "multi-challenger field: %s/%s overlaps sibling %s by %.3f "
                "(> tolerance %.3f); soft-rejected for field diversity",
                epoch_id,
                challenger.generation_id,
                peer_gid,
                overlap,
                diversity_tolerance,
            )
            _persist_soft_reject(
                challenger.generation_id,
                "field_diversity_overlap",
                f"overlap {overlap:.3f} with sibling {peer_gid or '(accepted)'} "
                f"exceeds diversity_tolerance {diversity_tolerance:.3f}",
            )
            continue
        if challenger is not None and diversity_tolerance is not None and isinstance(status, dict):
            # Enforcement active and the challenger is kept: stamp the slot
            # ``applied`` so the per-slot diversity status is explicit. Only
            # written when a tolerance is configured, so the default path's
            # field-status records are untouched.
            status = {
                **status,
                "diversity_status": "applied",
                "diversity_tolerance": diversity_tolerance,
            }
        field_status.append(status)
        if challenger is not None:
            applied.append(challenger)
            accepted_mutation_sets.append(frozenset(challenger.experiment.hypothesis.modulating))
            hyp = challenger.experiment.hypothesis
            siblings.append(
                PriorExperiment(
                    generation_id=challenger.generation_id,
                    epoch_id=epoch_id,
                    core_idea=hyp.core_idea,
                    modulating=tuple(hyp.modulating),
                    decision="in_flight",
                    rejection_reason="",
                    scalar_score_delta=None,
                    same_contract=True,
                )
            )
            sibling_signatures.append(_diversity_signature(challenger.experiment))

    if diversity_tolerance is not None and diversity_soft_rejected:
        log.info(
            "multi-challenger field: %s soft-rejected %d challenger(s) for "
            "field-diversity overlap (tolerance %.3f); %d kept",
            epoch_id,
            diversity_soft_rejected,
            diversity_tolerance,
            len(applied),
        )

    if not applied:
        # The whole field failed to apply — nothing to run. Record a
        # rejection-shaped outcome so the round still produces a clean
        # return value and the loop continues. Still persist the
        # field-status so the dashboard's proposing-step tracker reads
        # "N proposed · 0 applied — all rejected" rather than an empty
        # idle state (the recent all-failed run that prompted this).
        champion_only = [{"generation_id": parent_id, "seed": 1, "role": "champion"}]
        _publish_active_tournament(
            workspace_root,
            tournament_id=f"tourn_{epoch_id}_{base_id}",
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=champion_only,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=field_status,
            phase=TournamentPhase.PROPOSING,
            entries=_field_entries(champion_only),
        )
        # WS8: the round's terminal decision + close — the per-challenger
        # proposal_attempted failures were already emitted as they settled.
        round_log.emit(
            "decision_recorded",
            {
                "decision": "rejected",
                "provenance": {
                    "structure": tournament_spec.structure,
                    "reason": "multi-challenger field: no challenger applied cleanly",
                    "parent_generation_id": parent_id,
                    "promoted_generation_id": None,
                },
            },
        )
        round_log.emit("round_closed")
        return EvolveRoundOutcome(
            parent_generation_id=parent_id,
            proposed_generation_id="",
            tournament_decision="rejected",
            rejection_reason="multi-challenger field: no challenger applied cleanly",
            parent_scalar=0.0,
            child_scalar=0.0,
            delta_scalar=0.0,
        )

    # --- Optional random-baseline placebo arm (OVERFITTING.md #7) --------
    # Every Nth epoch-cumulative round the field carries ONE extra slot: a
    # semantics-preserving no-op child of the champion, hypothesis marked
    # as the baseline arm (zicato.evolve.placebo). It flows through the
    # unchanged strategy + gate like any challenger; the gate must reject
    # it, and a promoted placebo raises the CRITICAL ``placebo_promoted``
    # loop-health finding. Appended AFTER the all-failed early-return above
    # (a fully-failed proposer field keeps its historical outcome) and
    # appended LAST so sibling diversity, ``first_challenger_id``, and the
    # real challengers' ids are untouched. Best-effort: a placebo mint
    # failure narrows the field back to the real challengers.
    _placebo_every_n = int(getattr(weights.overfitting, "random_baseline_every_n", 0))
    if base_n is not None and mutations:
        from zicato.evolve.placebo import placebo_round_due  # noqa: PLC0415

        if placebo_round_due(_placebo_every_n, base_n):
            _placebo_id = f"v{base_n + field_n}"
            with best_effort(
                "random-baseline placebo mint",
                on_error=lambda exc: log.warning("random-baseline placebo skipped: %s", exc),
            ):
                _placebo = _mint_placebo_challenger(
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    parent_id=parent_id,
                    next_id=_placebo_id,
                    point=mutations[0],
                    round_index=round_index,
                )
                applied.append(_placebo)
                _placebo_status = {
                    "generation_id": _placebo.generation_id,
                    "status": "applied",
                    "reason": "random_baseline",
                    "attempts": 1,
                    "attempt_reasons": [],
                    "hypothesis": _trim_reason(_placebo.experiment.hypothesis.core_idea),
                    "seed": field_n + 2,
                }
                field_status.append(_placebo_status)
                _publish_proposing(_placebo_status)
                log.info(
                    "multi-challenger field: %s/%s fielded as the random-baseline "
                    "placebo arm (cadence every_n=%d, round %d) — the gate must "
                    "reject it",
                    epoch_id,
                    _placebo.generation_id,
                    _placebo_every_n,
                    base_n,
                )

    by_id: dict[str, _AppliedChallenger] = {c.generation_id: c for c in applied}
    champion_gen = Generation(
        id=parent_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace_root, epoch_id, parent_id),
        created_at=_now_iso(),
        promoted=True,
    )

    def _generation_for(gid: str) -> Generation:
        if gid == parent_id:
            return champion_gen
        return by_id[gid].generation

    # --- request_field: hand the strategy the champion + applied field.
    async def _request_field(_n: int) -> tuple[Contestant, list[Contestant]]:
        champion = Contestant(generation_id=parent_id, role="champion")
        challengers = [
            Contestant(
                generation_id=c.generation_id,
                role="challenger",
                snapshot_root=c.snapshot_root,
                experiment=c.experiment,
            )
            for c in applied
        ]
        return champion, challengers

    # --- Champion-eval mode provenance. With the cache-first board-unit
    # runner, ``run_matchup`` reports a per-generation cached-vs-fresh
    # tally (``unit_provenance``) over BOTH sides of each duel. The
    # round-level champion-eval mode is attributed to the CHAMPION
    # (``parent_id``) specifically: a challenger-vs-challenger duel runs
    # challengers fresh (they are new generations and MUST run), which
    # says nothing about champion reuse. We therefore accumulate only the
    # champion's tally across every matchup it appears in — a RUNTIME
    # provenance field, never a contract input.
    champion_cached_units = 0
    champion_fresh_units = 0

    # --- Cross-matchup concurrency cap. A non-gauntlet structure schedules
    # SEVERAL matchups of a round concurrently (the driver fans the batch out
    # under one ``asyncio.gather``). Without a shared gate each matchup would
    # mint its own ``Semaphore(parallelism)``, so N concurrent matchups could
    # run ``N × parallelism`` board units at once — overshooting the operator's
    # parallelism intent and the LLM endpoint's concurrency. One semaphore,
    # created here per round and handed to every ``run_matchup``, makes the
    # whole round draw from ONE global cap. Sized exactly as a single
    # matchup's would be, so a round with one matchup is unchanged.
    round_unit_semaphore = asyncio.Semaphore(max(1, int(config.parallelism)))

    # --- run_matchup: one duel via the board-unit runner + unchanged gate.
    # ``replicate_base`` / ``cache_scores`` exist for the evidence pre-gate's
    # replicate duels only: a reserved replicate base keeps evidence draws
    # off the canonical cache slots, and ``cache_scores=False`` keeps a
    # single evidence draw's aggregates from overwriting the round-scored
    # ``gen_score.json`` the fast-mode champion reuse reads. Every strategy
    # matchup uses the defaults, byte-identical to before.
    async def _run_matchup(
        m: Matchup, *, replicate_base: int = 0, cache_scores: bool = True
    ) -> MatchupResult:
        _beat(
            beater,
            epoch_id=epoch_id,
            generation_id=m.right.generation_id,
            round_index=round_index,
            phase=f"tournament:round_{round_index}:{m.matchup_id}",
        )
        result = await run_matchup(
            adapter=adapter,
            left_gen=_generation_for(m.left.generation_id),
            right_gen=_generation_for(m.right.generation_id),
            # Internal selection scores on the TRAIN slice only (the holdout
            # is confirmation-only, never used to pick the leader). A racing
            # rung's ``board_subset`` is intersected against the train board
            # inside ``run_matchup``. Empty holdout ⇒ ``train_board`` IS the
            # full board ⇒ byte-identical to today.
            board=train_board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            board_subset=m.board_subset,
            replicates=m.replicates,
            replicate_base=replicate_base,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast_mode,
            round_index=round_index,
            total_rounds=total_rounds,
            match_id=m.matchup_id,
            # Opt-in wall-clock cap on this duel's TOTAL board-unit execution.
            # None (the default for every structure that does not set it) keeps
            # the run uncapped, byte-identical to today; a racing rung may pin
            # it to bound a full-board grind (see Matchup.matchup_budget_seconds).
            matchup_budget_seconds=m.matchup_budget_seconds,
            # One shared semaphore across every matchup of this round, so all
            # concurrently-scheduled matchups draw from ONE global concurrency
            # cap rather than each minting its own ``Semaphore(parallelism)``.
            unit_semaphore=round_unit_semaphore,
        )
        # Attribute the CHAMPION's cached-vs-fresh board-unit tally for
        # this matchup (if the champion played in it). ``nonlocal`` so the
        # closure mutates the round-level accumulators.
        nonlocal champion_cached_units, champion_fresh_units
        champ_prov = result.unit_provenance.get(parent_id)
        if champ_prov is not None:
            champion_cached_units += champ_prov.cached
            champion_fresh_units += champ_prov.fresh
        # Cache both sides' aggregates for fast-mode reuse, mirroring the
        # gauntlet path's _cache_gen_score calls. Skipped for evidence
        # replicate duels (``cache_scores=False``): one reserved-slot draw
        # must not overwrite the round-scored aggregates.
        if cache_scores:
            _cache_gen_score(workspace_root, epoch_id, m.left.generation_id, result.parent_agg)
            _cache_gen_score(workspace_root, epoch_id, m.right.generation_id, result.child_agg)
        # WS8: this matchup's board units + gate verdict onto the round log.
        _emit_tournament_units(round_log, result)
        _emit_harness_loaded(round_log, workspace_root, epoch_id, result)
        _emit_gate_evaluated(
            round_log,
            result.outcome,
            parent_agg=result.parent_agg,
            child_agg=result.child_agg,
            weights=weights,
        )
        return MatchupResult(
            matchup_id=m.matchup_id,
            left_id=m.left.generation_id,
            right_id=m.right.generation_id,
            left_agg=result.parent_agg,
            right_agg=result.child_agg,
            outcome=result.outcome,
            stage_index=m.stage_index,
            bracket_slot=m.bracket_slot,
        )

    # --- Publish the live ActiveTournament envelope before scheduling.
    competitors_meta = [{"generation_id": parent_id, "seed": 1, "role": "champion"}] + [
        {"generation_id": c.generation_id, "seed": i + 2, "role": "challenger"}
        for i, c in enumerate(applied)
    ]
    tournament_id = f"tourn_{epoch_id}_{applied[0].generation_id}"
    _publish_active_tournament(
        workspace_root,
        tournament_id=tournament_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        round_index=round_index,
        total_rounds=total_rounds,
        field_status=field_status,
        entries=_field_entries(competitors_meta),
    )

    # Progress transition: the field tournament started executing. One
    # round-level append (NOT per matchup) so the liveness seq advances on
    # genuine progress. Best-effort — never abort the round on a log write.
    from zicato.runtime import progress_log  # noqa: PLC0415

    with best_effort(
        "progress-log field tournament-start",
        on_error=lambda exc: log.debug("progress-log field tournament-start skipped: %s", exc),
    ):
        progress_log.append_progress(workspace_root, progress_log.TOURNAMENT_START)

    # OPEN the durable field-tournament envelope NOW, before the bracket
    # resolves (issue #16). The runtime ``active_tournament`` envelope above
    # is ephemeral (cleared on crash, overwritten next round); only the
    # durable ``tournaments/field-*.json`` record is queryable by the index,
    # ``zicato reindex``, and any external consumer. Opening it here in
    # ``in_progress`` state — with the competitor field + proposing status
    # but no resolved bracket yet — means the in-flight round is visible to
    # EVERY store the moment its challengers are minted, not only at settle.
    # The settle write below upserts this same record (same tournament_id)
    # to ``settled`` with the resolved bracket; the open + settle compose
    # idempotently, so a resume that re-opens an existing in_progress record
    # neither duplicates nor corrupts it.
    first_challenger_id = applied[0].generation_id
    _persist_field_tournament(
        workspace_root,
        field_tournament_id=f"{epoch_id}:field:{first_challenger_id}",
        first_challenger_id=first_challenger_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        rounds=[],
        standings=[],
        field_status=field_status or [],
        decision=None,
        state="in_progress",
    )

    # --- Live structure publish. Each time the driver schedules a batch
    # of pending matchups, republish the envelope with the LIVE structure:
    # the settled rounds plus the in-flight round (matches with
    # ``winner: null`` + ``pending: true``) and the standings-so-far. This
    # is what lets the dashboard's bracket/ladder/funnel exist DURING the
    # run instead of showing "being seeded" until settle. The serialisation
    # goes through the SAME _serialise_rounds / _serialise_standings the
    # settle + durable-record producers use, so the shapes are
    # byte-compatible. Best-effort — _publish_active_tournament never
    # raises, so a publish failure cannot abort the resolution.
    def _publish_live_structure(strat: Any) -> None:
        live_rounds = _serialise_rounds(strat.live_rounds())
        # Overlay the runner's authoritative per-board ``projected`` map (the
        # scorer's domain) onto the racing rung's per-lane ``live_progress``
        # topology (the strategy's domain): the strategy publishes which lanes
        # are racing + their board-slice totals; the scorer publishes each
        # lane's live ``boards_done`` + streaming ``projected_scalar``. The
        # two compose here so the rung carries one authoritative per-lane
        # progress map the dashboard consumes directly.
        _overlay_projected_live_progress(live_rounds, workspace_root)
        live_standings = _overlay_projected_standings(
            _serialise_standings(strat.live_standings()),
            live_rounds,
            workspace_root,
            tournament_spec.structure,
        )
        _publish_active_tournament(
            workspace_root,
            tournament_id=tournament_id,
            epoch_id=epoch_id,
            structure=tournament_spec.structure,
            structure_params=dict(tournament_spec.params),
            competitors=competitors_meta,
            round_index=round_index,
            total_rounds=total_rounds,
            field_status=field_status,
            rounds=live_rounds,
            standings=live_standings,
            entries=_field_entries(competitors_meta, live_standings),
        )

    # --- Opt-in Bradley--Terry promotion pre-gate (crown on evidence). When
    # ``promote_confidence_threshold`` is set in the structure params, the
    # driver holds a crowning promote until the fitted rating clears the
    # confidence bar AND the CIs separate, spending closest-CI replicates in
    # between (the defer→replicate loop). Unset ⇒ ``pre_gate`` stays ``None``
    # and the resolution is byte-identical to today.
    pre_gate: EvidencePreGate | None = None
    replicate_duel = None
    on_inconclusive = None
    bt_threshold = read_promote_confidence_threshold(tournament_spec.params)
    if bt_threshold is not None:
        pre_gate = EvidencePreGate(
            threshold=bt_threshold,
            replicate_budget=read_replicate_budget(tournament_spec.params),
        )
        evidence_replicates_run = 0

        async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
            # One extra crowning-pair duel for the pre-gate's evidence loop,
            # routed through the SAME board-unit runner + gate every other
            # duel uses (so a replicate is scored identically to the original
            # duel) — but at a RESERVED replicate index
            # (EVIDENCE_REPLICATE_BASE + j for evidence replicate j), so each
            # replicate draws BOTH sides fresh instead of cache-replaying (or,
            # in full mode, clobbering) the canonical replicate-0 slots. The
            # matchup id encodes the index; the driver's audit guard keys on
            # it. ``cache_scores=False`` keeps the single-draw aggregates out
            # of the fast-mode ``gen_score.json`` reuse.
            nonlocal evidence_replicates_run
            replicate_slot = EVIDENCE_REPLICATE_BASE + evidence_replicates_run
            evidence_replicates_run += 1
            return await _run_matchup(
                Matchup(
                    matchup_id=f"bt-replicate:r{replicate_slot}:{left_id}:{right_id}",
                    left=Contestant(generation_id=left_id, role="champion"),
                    right=Contestant(generation_id=right_id, role="challenger"),
                ),
                replicate_base=replicate_slot,
                cache_scores=False,
            )

        replicate_duel = _replicate_duel

        def _on_inconclusive(resolution: EvidenceResolution) -> None:
            # Record the unresolved crowning duel to the dead-letter queue so
            # nothing is silently dropped. Best-effort: a write failure must
            # not abort the round.
            verdict = resolution.verdict
            challenger_id = (
                verdict.challenger.generation_id
                if verdict.challenger is not None
                else first_challenger_id
            )
            champion_id = (
                verdict.champion.generation_id if verdict.champion is not None else parent_id
            )
            with best_effort(
                "dead-letter inconclusive record",
                on_error=lambda exc: log.debug("dead-letter record skipped: %s", exc),
            ):
                record_inconclusive(
                    workspace_root,
                    InconclusiveRecord(
                        generation_id=challenger_id,
                        champion_id=champion_id,
                        epoch_id=epoch_id,
                        rating=rating_block(verdict),
                        ci_history=resolution.ci_history,
                        reason=verdict.reason,
                    ),
                )
            # WS8: the ci_state trail per evidence-gate refit. On this path
            # the driver surfaces the resolution only for the inconclusive
            # terminal; a confirmed promote's replicate duels are still
            # traced through the per-matchup unit/gate events above.
            for _ci_row in resolution.ci_history:
                round_log.emit("evidence_replicated", {"ci_state": dict(_ci_row)})

        on_inconclusive = _on_inconclusive

    # --- Drive the strategy to a crowned decision.
    try:
        decision = await resolve_tournament(
            strategy,
            request_field=_request_field,
            run_matchup=_run_matchup,
            on_progress=_publish_live_structure,
            pre_gate=pre_gate,
            replicate_duel=replicate_duel,
            on_inconclusive=on_inconclusive,
        )
    except Exception:
        # A failure mid-resolution leaves no settled bracket — clear the
        # live "running" envelope so the dashboard does not show a stuck
        # tournament, then re-raise.
        _clear_active_tournament(workspace_root)
        raise

    # --- Final champion-gate Ladder-mediated holdout confirmation
    # (OVERFITTING.md §3/§4). The structure resolved its leader on the TRAIN
    # slice and ran ONE crowning champion-vs-survivor duel (also train). If
    # that duel promoted AND a holdout slice exists, the win must ALSO confirm
    # on the holdout — through the SAME Ladder-mediated machinery + the SAME
    # per-epoch ``ladder_state.json`` budget the gauntlet uses. A released
    # non-confirmation flips the crowning promote to a holdout reject; the
    # champion stands. Empty holdout (small board / split disabled) ⇒ no
    # holdout run, no Ladder move ⇒ byte-identical to today. The resulting
    # ``holdout`` block + the challenger's holdout-slice scalar are stamped on
    # the crowned/leading challenger's OutcomeRecord below (same shape as the
    # gauntlet), so #5's gap detector + the board-status surface work for
    # these structures too.
    crowning_confirm = await _confirm_crowning_on_holdout(
        decision=decision,
        parent_id=parent_id,
        champion_gen=champion_gen,
        generation_for=_generation_for,
        adapter=adapter,
        board=board,
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        disable_drift=disable_drift,
        judge_only=judge_only,
        fast_mode=fast_mode,
        confirm_fn=confirm_crowning_holdout,
    )
    promoted_id = crowning_confirm.promoted_id
    crowning_reason_override = crowning_confirm.reason_override
    crowning_holdout_block = crowning_confirm.holdout_block
    crowning_holdout_child_scalar = crowning_confirm.holdout_child_scalar
    crowning_challenger_id = crowning_confirm.challenger_id
    crowning_challenger_train_scalar = crowning_confirm.challenger_train_scalar
    # WS8: the crowning holdout release (a populated block always means a
    # holdout existed and was consulted).
    if crowning_holdout_block is not None:
        round_log.emit(
            "holdout_released",
            {"confirmed": bool(crowning_holdout_block.get("confirmed"))},
        )

    # --- Opt-in integrity blocking modes (default OFF) -------------------
    # Guard the GATE-DECIDED crowning promote before anything persists,
    # mirroring the gauntlet's 10b'' block: diff containment on the crowned
    # child's snapshot + the gate-contradiction re-derivation against the
    # crowning duel's delta. Runs BEFORE the operator-override claim below,
    # so an explicit force-promote remains the operator's recorded
    # prerogative. Default-off ⇒ this branch is inert.
    if promoted_id is not None:
        _block_reason = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=champion_gen.snapshot_root,
            child_snapshot_root=by_id[promoted_id].snapshot_root,
            mutable_trees=_registered_mutable_trees(workspace_config),
            delta_scalar=crowning_confirm.crowning_delta_scalar,
        )
        if _block_reason is not None:
            log.warning(
                "evolve: integrity block — generation %s crowning refused (%s)",
                promoted_id,
                _block_reason,
            )
            promoted_id = None
            crowning_reason_override = _block_reason

    # --- Operator gate override (control protocol) for the FIELD ---------
    # The structure has settled (train bracket + holdout confirmation) but
    # nothing is persisted yet — the safe point at which an operator's
    # force-promote / force-reject of ANY field candidate overrides the
    # verdict. Unlike the gauntlet (one in-flight generation), a field round
    # resolves a whole slate, so an override may target a non-winner, the
    # crowned leader, or SEVERAL candidates (a tie / a multi-promote).
    # `_apply_field_overrides` documents the promoted-SET semantics + the
    # no-override single-promotion byte-identity invariant; every member of
    # the set is marked promoted in lineage while only the PRIMARY head
    # moves the champion pointer (the single-head invariant the downstream
    # guards rely on).
    field_candidate_ids = [c.generation_id for c in applied]
    field_overrides: dict[str, GateOverride] = claim_field_gate_overrides(
        workspace_root, field_candidate_ids
    )
    # The override RE-RESOLUTION is pure (`_apply_field_overrides`): given
    # the claimed overrides + the post-holdout crowning state it derives the
    # promoted set, the primary head, the per-generation provenance, and the
    # EFFECTIVE decision (the post-confirmation/post-override truth every
    # durable store must describe — issue #20). Only the claim above is I/O.
    (
        promoted_id,
        promoted_ids,
        override_provenance,
        effective_decision,
    ) = _apply_field_overrides(
        workspace_root=workspace_root,
        decision=decision,
        promoted_id=promoted_id,
        crowning_reason_override=crowning_reason_override,
        field_overrides=field_overrides,
        structure=tournament_spec.structure,
    )
    # WS8: the round's terminal decision + provenance (operator overrides
    # explicit, never silent) — the post-holdout/post-override truth.
    round_log.emit(
        "decision_recorded",
        {
            "decision": str(effective_decision.decision),
            "provenance": {
                "structure": tournament_spec.structure,
                "reason": effective_decision.reason,
                "parent_generation_id": parent_id,
                "promoted_generation_id": promoted_id,
                "promoted_generation_ids": sorted(promoted_ids),
                "overrides": override_provenance,
            },
        },
    )

    # Settle the live envelope with the resolved rounds + standings so the
    # dashboard's structure reader sees the final bracket. Unlike the
    # gauntlet path (which clears its transient running record on exit),
    # the multi-challenger envelope is RETAINED with phase="completed":
    # competitors/rounds/standings are the dashboard's only live source for
    # a non-gauntlet field until the next round's tournament starts. Settled
    # with the HOLDOUT-RESOLVED decision so the dashboard never shows a crown
    # the champion pointer contradicts.
    _settle_active_tournament(
        workspace_root,
        tournament_id=tournament_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        strategy=strategy,
        decision=effective_decision,
        round_index=round_index,
        total_rounds=total_rounds,
        field_status=field_status,
    )
    # Durably persist the settled FIELD structure (one record per round's
    # non-gauntlet tournament). The runtime ``active_tournament`` envelope
    # above is EPHEMERAL — it is overwritten by the next round and cleared
    # on a crash — so a completed swiss / elim epoch would render blank from
    # the index alone. The field record carries the same shape the live
    # envelope does, so the dashboard's structure renderers serve the ladder
    # post-run unchanged. Keyed on the round's first applied challenger so a
    # multi-round epoch keeps a snapshot per round. FINALISES the
    # ``in_progress`` envelope opened before resolution (issue #16) — the
    # same tournament_id, so this is an idempotent upsert to ``settled``.
    # Persisted with the HOLDOUT-RESOLVED decision (issue #20).
    _persist_field_tournament(
        workspace_root,
        field_tournament_id=f"{epoch_id}:field:{first_challenger_id}",
        first_challenger_id=first_challenger_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        rounds=_serialise_rounds(strategy.rounds()),
        standings=_serialise_standings(effective_decision.standings),
        field_status=field_status or [],
        decision=effective_decision,
        state="settled",
        # Operator override readback (additive — omitted when no override
        # fired, so a gate-decided field round's record is byte-identical).
        override_status=override_provenance or None,
        promoted_generation_ids=(sorted(promoted_ids) if len(promoted_ids) > 1 else None),
    )

    rank_by_id = {s.generation_id: s.rank for s in decision.standings}
    matches_by_gen: dict[str, list[MatchOutcome]] = {c.generation_id: [] for c in applied}
    for mr in decision.matchups:
        delta = mr.outcome.delta_scalar
        winner = mr.lower_scalar_id()
        if mr.left_id in matches_by_gen:
            matches_by_gen[mr.left_id].append(
                MatchOutcome(
                    match_id=mr.matchup_id,
                    opponent=mr.right_id,
                    won=(winner == mr.left_id),
                    delta_scalar=-delta,
                )
            )
        if mr.right_id in matches_by_gen:
            matches_by_gen[mr.right_id].append(
                MatchOutcome(
                    match_id=mr.matchup_id,
                    opponent=mr.left_id,
                    won=(winner == mr.right_id),
                    delta_scalar=delta,
                )
            )

    champion_agg = _first_aggregate_for(parent_id, decision)
    parent_scalar = float(champion_agg.get("scalar", 0.0)) if champion_agg else 0.0

    # Resolve a single round-level champion-eval provenance from the
    # CHAMPION's accumulated cached-vs-fresh board-unit tally. ``full``
    # when fast was not requested; ``fast`` when the champion was reused
    # for every board unit it needed (no fresh champion run this round);
    # ``fast-degraded`` when the champion had to run live at least once
    # (the seed/first champion, or a not-yet-covered subset) to seed the
    # cache. Recorded on each challenger's OutcomeRecord for the journal —
    # never a contract input.
    resolved_champion_eval_mode = _resolve_round_champion_mode(
        champion_cached_units,
        champion_fresh_units,
        fast_requested=fast_mode,
    )

    finalised_by_id: dict[str, Experiment] = {}
    child_scalar_crown = parent_scalar
    for challenger in applied:
        gid = challenger.generation_id
        # A generation is crowned if it is in the (possibly multi-element)
        # promoted set — see `_apply_field_overrides` for the no-override
        # single-promotion invariant (the set is exactly ``{promoted_id}``).
        is_crowned = gid in promoted_ids
        gid_override = field_overrides.get(gid)
        gen_decision = "promoted" if is_crowned else "rejected"
        agg = _first_aggregate_for(gid, decision)
        gen_scalar = float(agg.get("scalar", 0.0)) if agg else 0.0
        if gid == promoted_id:
            child_scalar_crown = gen_scalar
        # The crowning challenger (the survivor that reached the final
        # champion-gate duel) carries the Ladder/holdout evidence block + the
        # per-generation train/holdout/gap fields, mirroring the gauntlet's
        # OutcomeRecord. A holdout-demoted crown carries the
        # ``holdout_not_confirmed`` reason from the confirmation step instead
        # of the strategy's crowning reason. Every other challenger (a dead
        # bracket branch) keeps the back-compat defaults (no holdout).
        is_crowning_challenger = gid == crowning_challenger_id
        if is_crowning_challenger:
            rejection_reason = (
                ""
                if is_crowned
                else (crowning_reason_override if crowning_reason_override else decision.reason)
            )
            holdout_block = crowning_holdout_block
            # Pair the crowning duel's TRAIN scalar with its HOLDOUT scalar so
            # the gap is measured on the same duel (falls back to the standings
            # aggregate only if the crowning train scalar is somehow absent).
            crown_train = (
                crowning_challenger_train_scalar
                if crowning_challenger_train_scalar is not None
                else gen_scalar
            )
            gen_fields = _generalization_fields_from_scalars(
                crown_train, crowning_holdout_child_scalar
            )
        else:
            rejection_reason = "" if is_crowned else decision.reason
            holdout_block = None
            gen_fields = _generalization_fields_from_scalars(gen_scalar, None)
        # An operator override on THIS generation makes its verdict explicit
        # (the reject reason carries the override note; a forced promote
        # clears it). Only stamped when an override fired.
        operator_override = gid_override is not None
        operator_override_reason = gid_override.reason if gid_override is not None else ""
        if gid_override is not None:
            rejection_reason = f"operator override: {gid_override.reason}" if not is_crowned else ""
        outcome_record = OutcomeRecord(
            ran_at=_now_iso(),
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=gen_scalar - parent_scalar,
            tournament_decision=gen_decision,  # type: ignore[arg-type]
            rejection_reason=rejection_reason,
            operator_override=operator_override,
            operator_override_reason=operator_override_reason,
            structure=tournament_spec.structure,
            final_rank=rank_by_id.get(gid),
            match_record=tuple(matches_by_gen.get(gid, ())),
            champion_eval_mode=resolved_champion_eval_mode,
            holdout=holdout_block,
            train_loss=gen_fields["train_loss"],
            holdout_loss=gen_fields["holdout_loss"],
            generalization_gap=gen_fields["generalization_gap"],
        )
        # Shared outcome→index pipeline. Lineage + journal are deferred to
        # the loops below so the multi-challenger write ORDER is preserved:
        # every outcome persists, THEN the crowning invariant is checked,
        # THEN lineage + the champion marker advance, THEN the journal —
        # an invariant violation must abort before any lineage write.
        finalised_by_id[gid] = _finalize_generation(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            generation_id=gid,
            outcome=outcome_record,
            journal=False,
        )

    # --- Crowning invariant (issue #20): the durable bracket and the
    # champion state MUST agree. A settled bracket that records ``promoted``
    # with a ``promoted_generation_id`` the champion pointer + lineage never
    # advance to (or the inverse) is a silent correctness bug, so FAIL LOUDLY
    # before writing lineage rather than persisting a contradiction. The
    # persisted ``effective_decision`` is the post-holdout truth; ``promoted_id``
    # is what drives lineage + ``current_generation`` below — these two are the
    # same value by construction, and this guard makes that contract explicit
    # and catches any future code path that lets them drift apart.
    _bracket_promoted = effective_decision.decision == "promoted"
    if _bracket_promoted != (promoted_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{effective_decision.decision!r} (promoted_generation_id="
            f"{effective_decision.promoted_generation_id!r}) disagrees with the "
            f"champion to be crowned ({promoted_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
    if promoted_id is not None and promoted_id not in by_id:
        raise RuntimeError(
            "crowning invariant violated: settled bracket promotes "
            f"{promoted_id!r} but no such challenger applied this round; "
            "refusing to advance the champion to a generation with no snapshot"
        )

    # --- Lineage: every PROMOTED generation on the spine (promoted=True),
    # every other challenger recorded as a dead branch (rejected child of
    # the champion). An operator multi-promote marks each advanced candidate
    # promoted while current_generation still advances only to the PRIMARY
    # head below.
    for challenger in applied:
        gid = challenger.generation_id
        is_crowned = gid in promoted_ids
        gen_record = Generation(
            id=gid,
            epoch_id=epoch_id,
            parent_id=parent_id,
            snapshot_root=challenger.snapshot_root,
            created_at=challenger.generation.created_at,
            promoted=is_crowned,
            round_index=challenger.generation.round_index,
        )
        append_to_lineage(workspace_root, epoch_id, gen_record, parent_id=parent_id)
    if promoted_id is not None:
        _set_current_generation(workspace_root, epoch_id, promoted_id)
        # The marker MUST now name the crowned generation — a write that did
        # not stick (e.g. a read-only workspace) would leave a settled
        # ``promoted`` bracket whose champion never advanced. Re-read and
        # raise rather than diverge silently (issue #20 acceptance #3).
        _crowned_head = _resolve_current_generation(workspace_root, epoch_id)
        if _crowned_head != promoted_id:
            raise RuntimeError(
                "crowning invariant violated: bracket promoted "
                f"{promoted_id!r} but current_generation resolves to "
                f"{_crowned_head!r} after the crowning write; the champion "
                "pointer did not advance to the promoted generation"
            )

    # --- Journal: one entry per challenger (crowned + dead branches).
    for challenger in applied:
        append_journal_entry(workspace_root, epoch_id, finalised_by_id[challenger.generation_id])

    # --- Shared round epilogue (loop-health + analyzer + report) — the
    # same `_round_epilogue` the gauntlet path runs.
    health_summary, health_critical = await _round_epilogue(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        board=board,
        round_n=_round_n_from_generation_id(applied[0].generation_id) or round_index,
        analyzer_round=_round_n_from_generation_id(applied[0].generation_id),
        mutations=mutations,
        auxiliary_call_llm=auxiliary_call_llm,
        auxiliary_model=auxiliary_model,
        meta_loop_emitter=meta_loop_emitter,
        token_clip=_token_clip_state(config),
    )

    bookkeeping_decision = "promoted" if promoted_id is not None else "rejected"
    # Progress transition: the field tournament settled — record the
    # crowning (TOURNAMENT_SETTLE) then the terminal verdict so the
    # liveness seq lands on a PROMOTE/REJECT at the round's true end.
    with best_effort(
        "progress-log field tournament-settle",
        on_error=lambda exc: log.debug("progress-log field tournament-settle skipped: %s", exc),
    ):
        progress_log.append_progress(workspace_root, progress_log.TOURNAMENT_SETTLE)
    _beat(
        beater,
        workspace_root=workspace_root,
        progress=(
            progress_log.PROMOTE if bookkeeping_decision == "promoted" else progress_log.REJECT
        ),
        epoch_id=epoch_id,
        generation_id=promoted_id or applied[0].generation_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{tournament_id}:{bookkeeping_decision}",
    )
    round_log.emit("round_closed")

    # The round summary's champion/challenger scalars MUST come from the gate's
    # CROWNING matchup — the same champion-vs-leader duel the rejection_reason is
    # built from — not the per-pairing standings aggregate (`_first_aggregate_for`,
    # which averages across all of the champion's pairings) and not the
    # child-defaults-to-parent fallback (which reports delta 0.0 on a rejection
    # even though the gate measured a real regression — issue #10). Resolve the
    # crowning matchup's champion (the parent side) + challenger scalars; fall
    # back to the aggregate only when no crowning duel ran.
    summary_parent_scalar = parent_scalar
    summary_child_scalar = child_scalar_crown
    summary_child_id = promoted_id or applied[0].generation_id
    crowning = (
        next(
            (m for m in decision.matchups if m.matchup_id == decision.crowning_matchup_id),
            None,
        )
        if decision.crowning_matchup_id
        else None
    )
    if crowning is not None:
        champ_is_left = crowning.left_id == parent_id
        summary_parent_scalar = crowning.left_scalar() if champ_is_left else crowning.right_scalar()
        summary_child_scalar = crowning.right_scalar() if champ_is_left else crowning.left_scalar()
        # On a rejection the "proposed" gen reported is the LEADING challenger that
        # reached the gate (the one the reason is about), not an arbitrary applied[0].
        summary_child_id = promoted_id or (crowning.right_id if champ_is_left else crowning.left_id)

    # A holdout-demoted crown reports the ``holdout_not_confirmed`` cause from
    # the confirmation step, not the strategy's (promote-shaped) crowning
    # reason; every other rejection keeps the strategy's reason.
    summary_reason = (
        ""
        if promoted_id is not None
        else (crowning_reason_override if crowning_reason_override else decision.reason)
    )
    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=summary_child_id,
        tournament_decision=bookkeeping_decision,
        rejection_reason=summary_reason,
        parent_scalar=summary_parent_scalar,
        child_scalar=summary_child_scalar,
        delta_scalar=summary_child_scalar - summary_parent_scalar,
        health_summary=health_summary,
        health_critical=health_critical,
    )


# ---------------------------------------------------------------------------
# Round-decision + outcome helpers shared by the evolve pipelines
# ---------------------------------------------------------------------------


def _count_infra_aborted_runs(tournament_result: Any) -> int:
    """Count THIS duel's INFRA-aborted runs across both sides.

    Reads the settled result's ``per_entry_losses`` — one
    ``(parent, child)`` :class:`~zicato.core.LossProfile` pair per board
    entry — and counts every profile whose ``abort_cause`` names a
    non-cacheable infra abort
    (:func:`~zicato.core.loss.is_infra_abort_cause`: a worker crash, a
    parent/supervisor kill, an unreadable result — never the genuine
    ``budget_exhausted`` wall-clock exhaustion). Cache-reused units can
    never contribute (an infra abort is never persisted to the unit
    cache), so the count reflects THIS round's live failures only —
    the honest per-round outage signal for the circuit breaker.
    """
    from zicato.core.loss import is_infra_abort_cause  # noqa: PLC0415

    per_entry = getattr(tournament_result, "per_entry_losses", None) or {}
    count = 0
    for pair in per_entry.values():
        for loss in pair:
            if is_infra_abort_cause(getattr(loss, "abort_cause", None)):
                count += 1
    return count


def _token_clip_state(config: Any) -> tuple[int, int] | None:
    """The round's token-clip evidence for the health report, or ``None``.

    ``(tokens_spent, max_tokens_per_round)`` when this round's ledger
    latched its ``clipped`` flag (a scheduler stopped launching work on the
    spent budget); ``None`` otherwise — including every round with the
    knob off, where no ledger is even bound.
    """
    ledger = getattr(config, "token_ledger", None)
    if ledger is None or not getattr(ledger, "clipped", False):
        return None
    return int(ledger.spent), int(ledger.max_tokens)


def _defer_round_infra_outage(
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    board: list[Any],
    round_index: int,
    infra_aborted: int,
    infra_threshold: int,
    beater: HeartbeatBeater | None,
    round_log: _RoundLogEmitter,
) -> EvolveRoundOutcome:
    """Settle one round as DEFERRED on the endpoint-outage circuit (WS-H).

    Deliberately does NOT: cache either side's ``gen_score.json`` (a
    mostly-aborted aggregate would poison fast mode), route the gate
    verdict through the strategy, write an outcome / lineage / journal
    entry, or advance anything. The ``experiment.json`` persisted before
    the tournament stays UN-OUTCOMED — the exact on-disk shape the
    conservative crash-resume (:func:`zicato.runtime.resume.prepare_resume`)
    already reconciles: with at least one completed unit's cached
    ``loss.json`` the round resumes in place (the cache HITs the done
    units), with none it discards cleanly and re-proposes. The round log
    records the deferral, and the round's health report carries the
    ``infra_outage`` WARNING so the outage is visible on every surface
    that reads findings.
    """
    reason = (
        f"deferred_infra: {infra_aborted} infra-aborted run(s) reached the "
        f"endpoint-outage threshold of {infra_threshold}"
    )
    log.warning(
        "evolve: round %d (%s) DEFERRED — %d infra-aborted run(s) reached the "
        "endpoint-outage threshold of %d; keeping the experiment un-outcomed "
        "for resume (check the model endpoint / worker infrastructure)",
        round_index,
        next_id,
        infra_aborted,
        infra_threshold,
    )
    round_log.emit(
        "decision_recorded",
        {
            "decision": DEFERRED_INFRA_DECISION,
            "provenance": {
                "reason": reason,
                "infra_aborted_runs": infra_aborted,
                "infra_abort_round_threshold": infra_threshold,
                "parent_generation_id": parent_id,
                "promoted_generation_id": None,
            },
        },
    )
    health_summary, health_critical = _assess_and_persist_loop_health(
        workspace_root,
        epoch_id,
        _round_n_from_generation_id(next_id) or round_index,
        board,
        infra_outage=(infra_aborted, infra_threshold),
    )
    _beat(
        beater,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"deferred_infra:round_{round_index}:{next_id}",
    )
    round_log.emit("round_closed")
    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=next_id,
        tournament_decision=DEFERRED_INFRA_DECISION,
        rejection_reason=reason,
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
        health_summary=health_summary,
        health_critical=health_critical,
    )


# ---------------------------------------------------------------------------
# Small helpers — kept private so the public surface stays narrow.
# ---------------------------------------------------------------------------


def _generalization_fields(child_scalar: float, tournament_result: Any) -> dict[str, float | None]:
    """Build the per-generation ``train_loss`` / ``holdout_loss`` / gap fields.

    ``child_scalar`` is the challenger's TRAIN-slice scalar (the score that
    gated it — the train slice IS the full board when there is no holdout, so
    this is the byte-identical full-board scalar in the common degrade). The
    holdout scalar is read off the runner's
    :attr:`~zicato.tournament.runner.TournamentResult.holdout_child_scalar`,
    which is ``None`` whenever there was no holdout to measure.

    The generalization gap is ``holdout_loss - train_loss`` (OVERFITTING.md
    §6 / §12 #5): positive = the holdout scores *worse* than train, the
    board-memorization signature the gap detector watches for. ``None`` for
    both holdout fields when there is no holdout — the safe, no-finding
    degrade.
    """
    holdout_scalar = getattr(tournament_result, "holdout_child_scalar", None)
    return _generalization_fields_from_scalars(child_scalar, holdout_scalar)


def _generalization_fields_from_scalars(
    train_scalar: float, holdout_scalar: float | None
) -> dict[str, float | None]:
    """Build the per-generation ``train_loss`` / ``holdout_loss`` / gap fields.

    The scalar-level core of :func:`_generalization_fields`, shared with the
    non-gauntlet (multi-challenger) path which already holds the crowning
    challenger's TRAIN-slice and HOLDOUT-slice scalars directly (rather than a
    :class:`~zicato.tournament.runner.TournamentResult`). The generalization
    gap is ``holdout_loss - train_loss`` (OVERFITTING.md §6 / §12 #5); ``None``
    for both holdout fields when there is no holdout — the safe no-finding
    degrade.
    """
    train_loss = float(train_scalar)
    if holdout_scalar is None:
        return {"train_loss": train_loss, "holdout_loss": None, "generalization_gap": None}
    holdout_loss = float(holdout_scalar)
    return {
        "train_loss": train_loss,
        "holdout_loss": holdout_loss,
        "generalization_gap": holdout_loss - train_loss,
    }


def _round_n_from_generation_id(generation_id: str) -> int | None:
    """Map a ``vN`` generation id back to ``N`` for the analyzer's filename.

    Returns ``None`` (which makes the analyzer write
    ``insights/latest.md`` instead of a numbered round) for any
    generation id that doesn't follow the ``vN`` convention. Defensive
    against future schema changes; the orchestrator's own
    ``_next_generation_id`` always picks ``vN`` so this is a no-op on
    healthy inputs.
    """

    if generation_id.startswith("v") and generation_id[1:].isdigit():
        return int(generation_id[1:])
    return None


def _current_generation_marker(workspace_root: Path, epoch_id: str) -> Path:
    return WorkspaceLayout.from_root(workspace_root).current_generation_marker(epoch_id)


def _resolve_current_generation(workspace_root: Path, epoch_id: str) -> str:
    """Return the id of the promoted lineage head for this epoch.

    Reads ``epochs/{epoch}/current_generation`` if present; otherwise
    falls back to the highest-numbered ``vN`` subdirectory under
    ``generations/``. Raises :class:`FileNotFoundError` when neither
    path resolves — that's a sign the operator hasn't established a
    baseline generation yet.
    """
    marker = _current_generation_marker(workspace_root, epoch_id)
    if marker.exists():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return text
    gens_root = WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)
    if not gens_root.exists():
        raise FileNotFoundError(f"no generations under {gens_root}; the epoch has no baseline yet")
    candidates = [p.name for p in gens_root.iterdir() if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"no generations under {gens_root}; the epoch has no baseline yet")

    def _key(name: str) -> tuple[int, int, str]:
        if name.startswith("v") and name[1:].isdigit():
            return (0, int(name[1:]), name)
        return (1, 0, name)

    return sorted(candidates, key=_key)[-1]


def _safe_resolve_parent(workspace_root: Path, epoch_id: str | None) -> str:
    """Best-effort resolve the lineage head for a synthetic abort outcome.

    Used only on the within-round budget-abort path, where we need *a*
    ``parent_generation_id`` for the fabricated :class:`EvolveRoundOutcome`
    but the round was cancelled before it resolved its own parent. Any
    resolution failure (no baseline yet, missing epoch) degrades to the
    empty string rather than masking the real budget-abort message with
    an unrelated traceback.
    """
    if not epoch_id:
        return ""
    try:
        return _resolve_current_generation(workspace_root, epoch_id)
    except (FileNotFoundError, OSError):
        return ""


def _set_current_generation(workspace_root: Path, epoch_id: str, generation_id: str) -> None:
    marker = _current_generation_marker(workspace_root, epoch_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(generation_id + "\n", encoding="utf-8")


def _snapshot_root(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Return a generation's source-tree path via the :class:`GenerationStore` seam.

    Generation source trees are a pluggable store
    (``docs/design/STORAGE.md`` §4-§5); this resolves the coordinate
    through the workspace's :class:`~zicato.epoch.genstore.GenerationStore`
    rather than hard-coding the directory layout. The default store is
    the directory-snapshot backend, so the resolved path is unchanged
    (``generations/{id}/snapshot/``) — but a git backend would resolve
    it to a worktree at this one seam.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    return default_generation_store(workspace_root).snapshot_root(epoch_id, generation_id)


def _next_generation_id(workspace_root: Path, epoch_id: str) -> str:
    """Pick a fresh ``vN`` id one above the highest existing.

    Generation presence comes from the
    :class:`~zicato.epoch.genstore.GenerationStore` seam — the directory
    backend reports the same on-disk ``vN`` directories as before.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    store = default_generation_store(workspace_root)
    max_n = -1
    for gid in store.list_generations(epoch_id):
        if gid.startswith("v") and gid[1:].isdigit():
            n = int(gid[1:])
            if n > max_n:
                max_n = n
    return f"v{max_n + 1}"


def _resolve_mutable_trees(adapter: Any, snapshot_root: Path) -> list[Path]:
    """Resolve the mutable surface for a generation snapshot.

    The **mutable surface** is the set of sub-trees the proposer may
    rewrite — narrower than the whole snapshot, which also carries
    support code the worker executes but the proposer never edits. An
    adapter declares it via :meth:`HarnessAdapter.mutable_subpaths`,
    which re-bases the adapter's mutable-tree declaration onto this
    concrete ``snapshot_root``.

    Falls back to ``[snapshot_root]`` — the whole tree — only when the
    adapter has no ``mutable_subpaths`` method (a non-conforming or
    legacy adapter). Mutation enumeration walks exactly the returned
    paths.
    """
    resolver = getattr(adapter, "mutable_subpaths", None)
    if callable(resolver):
        subpaths = resolver(snapshot_root)
        if subpaths:
            return list(subpaths)
    return [snapshot_root]


def _load_parent_losses(
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    board: list[Any],
    read_loss_profile: Callable[[Path], Any],
) -> list[Any]:
    """Read every ``loss.json`` under the parent generation's runs/.

    Returns the list in board order so detectors that care about
    ordering see a stable view. Missing per-entry loss files are
    skipped silently — the parent might be ``v0`` with no telemetry
    yet on a freshly-initialised epoch.
    """
    losses: list[Any] = []
    for entry in board:
        from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

        lpath = loss_profile_path(workspace_root, epoch_id, parent_id, entry.id)
        if lpath.exists():
            try:
                losses.append(read_loss_profile(lpath))
            except (OSError, ValueError, KeyError):
                continue
    return losses


def _build_events_paths(
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    board: list[Any],
) -> dict[str, Path]:
    """Map entry id → events.jsonl path under the parent generation."""
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415

    return {
        entry.id: events_jsonl_path(workspace_root, epoch_id, parent_id, entry.id)
        for entry in board
    }


def _render_failure_profile(losses: list[Any], weights: Any) -> str:
    """Build the bucketed, board-anonymized outcome-marginal profile block.

    Capability 2 of issue #18. Aggregates the TRAIN-slice ``losses`` (the
    caller has already excluded the holdout) into board-wide outcome
    marginals and renders them through the proposer's banding step so every
    number is coarsened and no entry id / question / output token reaches the
    model. The optional operator summarizer hook (``weights
    .outcome_summarizer_spec``) contributes extra marginals, every one of
    them sanitized + numeric-only before it is banded.

    Returns the empty string — the proposer-side "omit this section" sentinel
    — when the slice is empty (a baseline round) or carries no outcome
    signal, so the proposer prompt is byte-identical to today.
    """
    from zicato.analyzer.outcome_marginals import (  # noqa: PLC0415
        aggregate_outcome_marginals,
        run_operator_summarizer,
    )
    from zicato.proposer.prompts import render_failure_mode_profile  # noqa: PLC0415

    spec = str(getattr(weights, "outcome_summarizer_spec", "") or "")
    operator_marginals = run_operator_summarizer(spec, losses) if spec else {}
    summary = aggregate_outcome_marginals(losses, operator_marginals=operator_marginals)
    return render_failure_mode_profile(summary)


def _render_process_exemplars_block(
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    patterns: list[Any],
    train_entry_ids: list[str],
    weights: Any,
) -> str:
    """Build the opt-in, redacted process-exemplar prompt block — best-effort.

    The opt-in half of the proposer failure-signal surface
    (``docs/design/PROCESS-EXEMPLARS.md``): when the contract sets
    ``proposer_quality.process_exemplars > 0``, extract up to that many
    drift-anchored event windows from the CHAMPION's TRAIN-slice
    ``events.jsonl`` files — the same ``parent_id`` + train partition the
    patterns / loss summary / outcome marginals already use — mechanically
    redacted by the extractor (no entry ids, no task text, no model
    outputs), and render them through the proposer's block renderer.

    Returns the empty string — the proposer-side "omit this section"
    sentinel — when the knob is off (the default; no extraction even
    runs, so the round is byte-identical to today), when no pattern has
    an event footprint, or when extraction fails for any reason:
    best-effort by contract, an exemplar failure must never abort a round.
    """
    quality = getattr(weights, "proposer_quality", None)
    cap = int(getattr(quality, "process_exemplars", 0) or 0)
    if cap <= 0:
        return ""
    with best_effort(
        "process-exemplar extraction",
        on_error=lambda exc: log.debug("process-exemplar extraction skipped: %s", exc),
    ):
        from zicato.analyzer.process_exemplars import extract_process_exemplars  # noqa: PLC0415
        from zicato.proposer.prompts import render_process_exemplars  # noqa: PLC0415

        exemplars = extract_process_exemplars(
            workspace_root,
            epoch_id,
            patterns,
            cap,
            parent_generation_id=parent_id,
            train_entry_ids=train_entry_ids,
        )
        return render_process_exemplars(exemplars)
    return ""


def _render_loss_summary(losses: list[Any]) -> str:
    """Render a short human-readable loss summary for the proposer prompt."""
    if not losses:
        return "(no prior loss data; this is a baseline round)"
    drift_total = sum(getattr(loss, "drift_loss", 0.0) for loss in losses)
    drift_mean = drift_total / len(losses)
    pass_eligible = [loss for loss in losses if getattr(loss, "pass_fail", None) is not None]
    if pass_eligible:
        pass_rate = sum(1 for loss in pass_eligible if loss.pass_fail) / len(pass_eligible)
        pass_part = f", pass_rate={pass_rate:.2f} over {len(pass_eligible)} entries"
    else:
        pass_part = ""
    return f"drift_loss_mean={drift_mean:.3f} over {len(losses)} runs" + pass_part


async def _regenerate_epoch_report(
    workspace_root: Path,
    epoch_id: str,
    auxiliary_call_llm: CallLLM,
    auxiliary_model: str,
) -> None:
    """Refresh the epoch publication's deterministic sections — best-effort.

    The event-driven freshness path (``docs/design/PUBLICATION.md``): after
    each settled round the publication's data-bearing sections (masthead,
    methodology, results, validity, proposer analytics, threats) are
    re-templated from the CURRENT workspace data WITHOUT an auxiliary-LLM
    call — cost discipline. The existing LLM-authored prose is preserved
    verbatim; the full LLM prose render happens at epoch close. Mid-epoch
    the masthead carries the ``LIVING DRAFT — through round N`` stamp.

    Naturally debounced: the round epilogue runs exactly once per settled
    round, so this fires at most once per round. Digest-gated inside — a
    settled round that moved no data rewrites nothing. Strictly
    best-effort: any failure is swallowed and logged at debug level so a
    wedge here can never abort the round or the loop. ``auxiliary_*`` are
    accepted for call-site parity with the LLM-authoring close render (and
    so the full-render path can be swapped back in per-epoch if wanted);
    the per-round refresh deliberately spends no tokens.
    """
    del auxiliary_call_llm, auxiliary_model  # no per-round LLM call by design
    with best_effort(
        "epoch analysis report regeneration",
        on_error=lambda exc: log.debug("epoch analysis report regeneration skipped: %s", exc),
    ):
        from zicato.analyzer import regenerate_epoch_report_deterministic  # noqa: PLC0415

        regenerate_epoch_report_deterministic(workspace_root, epoch_id)


# ---------------------------------------------------------------------------
# Durable per-round event log (WS8) — best-effort emission
# ---------------------------------------------------------------------------


class _RoundLogEmitter:
    """Best-effort appender onto one round's durable RoundLog (WS8).

    Emission failures must NEVER fail a round — the live index dual-write
    (:func:`_ingest_experiment_into_index`) is the precedent: the canonical
    stores (``experiment.json``, lineage, journal) stay authoritative and
    the event log is a derived, replayable trace. A bind failure degrades
    to a permanent no-op emitter; every append failure is logged at
    ``debug`` and swallowed.

    ``emit`` takes the wire ``type_token`` plus its payload fields and
    resolves the typed event through
    :data:`zicato.epoch.round_log.EVENT_TYPES` — the same string-token seam
    the proposer-side callback uses (:attr:`ProposerContext
    .round_event_emitter`), so one signature serves both sides. An unknown
    token is silently dropped (never a crash on a vocabulary skew).
    """

    __slots__ = ("_log",)

    def __init__(self, workspace_root: Path, epoch_id: str, round_index: int) -> None:
        self._log: Any = None
        try:
            from zicato.epoch.round_log import RoundLog  # noqa: PLC0415

            self._log = RoundLog(workspace_root, epoch_id, round_index)
        except Exception as exc:  # noqa: BLE001 — emission must never fail a round
            log.debug("round-log emitter unavailable: %s", exc)

    def emit(self, type_token: str, fields: dict[str, Any] | None = None) -> None:
        """Append one typed event; any failure is swallowed at debug level."""
        if self._log is None:
            return
        try:
            from zicato.epoch.round_log import EVENT_TYPES  # noqa: PLC0415

            cls = EVENT_TYPES.get(type_token)
            if cls is None:
                return
            self._log.append(cls(**(fields or {})))
        except Exception as exc:  # noqa: BLE001 — emission must never fail a round
            log.debug("round-log emit %s skipped: %s", type_token, exc)


def _emit_tournament_units(round_log: _RoundLogEmitter, tournament_result: Any) -> None:
    """Emit ``unit_completed`` events for a settled duel's board units.

    Emitted as an AGGREGATE after the duel settles (per-unit emission at
    the runner layer would thread a callback through the subprocess-worker
    boundary — too invasive for the runner's contract): one event per
    ``(entry, side)`` pair off the duel's ``per_entry_losses`` map, with
    ``replicate=0`` (the runner's per-entry map carries the canonical
    replicate; extra replicates fold into the aggregates upstream and are
    not re-derivable here). Best-effort like every emission.
    """
    per_entry = getattr(tournament_result, "per_entry_losses", None) or {}
    try:
        entry_ids = sorted(per_entry)
    except Exception:  # noqa: BLE001 — emission must never fail a round
        return
    for entry_id in entry_ids:
        for side in ("parent", "child"):
            round_log.emit(
                "unit_completed",
                {"entry_id": str(entry_id), "replicate": 0, "side": side},
            )


def _emit_harness_loaded(
    round_log: _RoundLogEmitter,
    workspace_root: Path,
    epoch_id: str,
    tournament_result: Any,
) -> None:
    """Emit ``harness_loaded`` once per generation the duel actually ran.

    The mutated-tree provenance for issue #110: the subprocess worker records
    what each generation actually loaded in its ``harness_load.json`` (it is the
    only process that imports the entrypoint or the mutable trees) — the
    resolved entrypoint file plus the accumulated per-tree verdicts; the
    orchestrator — the round log's single writer — folds that into ONE event per
    generation here, so the durable round record names both the file each side
    ran and any tree its units never imported.

    Best-effort and additive throughout: a generation with no record (a
    non-ADK adapter kind, a fully cache-served side, a failed write) simply
    contributes no event, and readers tolerate the absence.
    """
    generation_ids: list[str] = []
    for attr in ("parent_generation_id", "child_generation_id"):
        gen_id = str(getattr(tournament_result, attr, "") or "")
        if gen_id and gen_id not in generation_ids:
            generation_ids.append(gen_id)
    for gen_id in generation_ids:
        try:
            from zicato.core.workspace import harness_load_path  # noqa: PLC0415
            from zicato.health.inputs import str_tuple  # noqa: PLC0415
            from zicato.storage import read_json  # noqa: PLC0415

            record = read_json(harness_load_path(workspace_root, epoch_id, gen_id))
        except Exception as exc:  # noqa: BLE001 — emission must never fail a round
            log.debug("round-log harness_loaded read skipped for %s: %s", gen_id, exc)
            continue
        entrypoint_file = str((record or {}).get("entrypoint_file", "") or "")
        verified = str_tuple((record or {}).get("trees_verified"))
        never_imported = str_tuple((record or {}).get("trees_never_imported"))
        if not entrypoint_file and not verified and not never_imported:
            continue
        round_log.emit(
            "harness_loaded",
            {
                "generation_id": gen_id,
                "entrypoint_file": entrypoint_file,
                "trees_verified": verified,
                "trees_never_imported": never_imported,
            },
        )


# _str_tuple and the tree-import-gap reader now live in
# zicato.health.inputs (imported below) — pure workspace reads shared with
# the standalone `zicato health` CLI, which needs the same findings from a
# point-in-time invocation rather than a live round.


def _emit_gate_evaluated(
    round_log: _RoundLogEmitter,
    outcome: Any,
    *,
    parent_agg: Any = None,
    child_agg: Any = None,
    weights: Any = None,
) -> None:
    """Emit the ``gate_evaluated`` event for one settled duel's gate verdict.

    ``rule_fired`` carries the gate's own ``reason`` verbatim (the string
    that names which rule rejected; empty on a clean promote — the gate
    reports no rule for a pass). UNCHANGED.

    The scalars the gate decided on are recorded STRUCTURALLY alongside it, on
    BOTH decisions, so a promoted duel is no longer a numberless record and a
    downstream effect-size analysis is not missing exactly its promotions (see
    :class:`~zicato.epoch.round_log.GateEvaluated`). They come from the same
    aggregates and weights the gate itself was handed, so the event cannot
    disagree with the decision it describes.

    The three sources are keyword-optional: an older caller still emits a
    well-formed event with the scalars ABSENT (``None``) rather than
    fabricating zeros. Same tolerance within a call — a hand-built aggregate
    carrying no ``scalar``, or a non-numeric one, leaves that field absent
    rather than failing the round, matching the best-effort discipline of
    every other emission.
    """
    fields: dict[str, Any] = {
        "rule_fired": str(getattr(outcome, "reason", "") or ""),
        "decision": str(getattr(outcome, "decision", "") or ""),
    }
    for key, agg in (("champion_scalar", parent_agg), ("challenger_scalar", child_agg)):
        if isinstance(agg, dict):
            raw = agg.get("scalar")
            if _is_real_number(raw):
                fields[key] = float(raw)
    margin = getattr(weights, "promote_margin", None)
    if _is_real_number(margin):
        fields["margin_required"] = float(margin)
    round_log.emit("gate_evaluated", fields)


def _is_real_number(value: Any) -> TypeGuard[int | float]:
    """``True`` iff ``value`` is a genuine ``int``/``float``, never a ``bool``.

    ``bool`` is an ``int`` subclass, so a bare ``isinstance(x, int | float)``
    admits ``True``/``False`` — a malformed aggregate carrying
    ``{"scalar": True}`` would silently record a ``champion_scalar`` of
    ``1.0``. Used everywhere :func:`_emit_gate_evaluated` extracts a
    numeric field from an untrusted ``dict``/attribute so a boolean is
    treated the same as any other non-numeric value: the field is left
    absent rather than fabricated.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Per-round loop-health assessment
# ---------------------------------------------------------------------------


def _health_round_report_path(workspace_root: Path, epoch_id: str, round_n: int) -> Path:
    """Return the path of one round's loop-health report JSON.

    Layout: ``epochs/{epoch}/health/round_{N}.json``. ``N`` is the
    round number derived from the child generation id (``vN``); a
    non-``vN`` id (defensive) falls back to ``0``.
    """
    return WorkspaceLayout.from_root(workspace_root).health_dir(epoch_id) / f"round_{round_n}.json"


def _collect_epoch_health_inputs(
    workspace_root: Path,
    epoch_id: str,
    board: list[Any],
) -> tuple[dict[str, list[Any]], list[Any]]:
    """Gather the epoch's accumulated losses + experiments for a health check.

    Walks every ``vN`` generation directory under the epoch and reads,
    per generation, every per-entry ``loss.json`` and the generation's
    ``experiment.json`` (when present). Returns a tuple of:

    * ``losses_by_generation`` — ``{generation_id: [LossProfile, ...]}``
    * ``experiments`` — ``[Experiment, ...]`` in generation order.

    Best-effort throughout: a missing or unreadable file is skipped
    rather than raised, because the health assessment must never be the
    thing that aborts a round. ``v0`` typically has no experiment (it is
    the seed) and may have no losses on a fresh epoch — both are fine.
    """
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415
    from zicato.epoch import read_experiment  # noqa: PLC0415
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    losses_by_generation: dict[str, list[Any]] = {}
    experiments: list[Any] = []

    gens_root = WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)
    if not gens_root.exists():
        return losses_by_generation, experiments

    def _gen_key(name: str) -> tuple[int, int, str]:
        if name.startswith("v") and name[1:].isdigit():
            return (0, int(name[1:]), name)
        return (1, 0, name)

    gen_ids = sorted(
        (p.name for p in gens_root.iterdir() if p.is_dir()),
        key=_gen_key,
    )
    for gen_id in gen_ids:
        gen_losses: list[Any] = []
        for entry in board:
            lpath = loss_profile_path(workspace_root, epoch_id, gen_id, entry.id)
            if not lpath.exists():
                continue
            try:
                gen_losses.append(read_loss_profile(lpath))
            except (OSError, ValueError, KeyError):
                continue
        if gen_losses:
            losses_by_generation[gen_id] = gen_losses
        try:
            experiments.append(read_experiment(workspace_root, epoch_id, gen_id))
        except (FileNotFoundError, OSError, ValueError, KeyError):
            # v0 (the seed) has no experiment.json; skip silently.
            continue

    return losses_by_generation, experiments


def _epoch_max_generations_per_contract(workspace_root: Path, epoch_id: str) -> int | None:
    """Read the epoch's ``overfitting.max_generations_per_contract`` cadence.

    Best-effort: a missing / unreadable ``scoring.json`` yields ``None`` so
    the cadence detector stays silent (OVERFITTING.md §12 #6). Used only to
    feed :func:`zicato.health.diagnostics.detect_refresh_cadence`.
    """
    import json as _json  # noqa: PLC0415

    from zicato.core.workspace import scoring_path  # noqa: PLC0415
    from zicato.workspace_loader import overfitting_config_from_dict  # noqa: PLC0415

    try:
        raw = _json.loads(scoring_path(workspace_root, epoch_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return overfitting_config_from_dict(raw.get("overfitting")).max_generations_per_contract


# _epoch_noise_floor_inputs and _epoch_preflight_record now live in
# zicato.health.inputs (imported below) alongside the tree-import-gap
# reader, for the same reason: the CLI needs them too.


async def _maybe_calibrate_noise_floor(
    *,
    workspace_root: Path,
    epoch_id: str,
    epoch_cfg: Any,
    workspace_config: Any,
    adapter: Any,
    parent_gen: Generation,
    board: list[Any],
    weights: Any,
    config: Any,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
) -> None:
    """Run the opt-in A/A noise-floor calibration once per epoch.

    Fires only when the workspace config carries ``"calibrate_noise_floor":
    K`` (K >= 2 draws) AND the epoch record has no measured floor yet, so the
    measurement happens exactly once at epoch open (the first evolve round of
    a fresh epoch) and every later round short-circuits on the persisted
    record. Best-effort by contract — a calibration failure must never abort
    the round.
    """
    raw = workspace_config.get("calibrate_noise_floor")
    if not raw:
        return
    try:
        runs = int(raw)
    except (TypeError, ValueError):
        log.warning("calibrate_noise_floor=%r is not an integer; skipping calibration", raw)
        return
    if runs < 2:
        log.warning("calibrate_noise_floor=%d needs at least 2 draws; skipping calibration", runs)
        return
    if getattr(epoch_cfg, "noise_floor", None) is not None:
        return

    from zicato.epoch.lifecycle import set_epoch_noise_floor  # noqa: PLC0415
    from zicato.tournament.calibration import measure_noise_floor  # noqa: PLC0415

    with best_effort(
        "A/A noise-floor calibration",
        on_error=lambda exc: log.warning("noise-floor calibration skipped: %s", exc),
    ):
        floor = await measure_noise_floor(
            adapter=adapter,
            generation=parent_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            runs=runs,
            disable_drift=disable_drift,
            judge_only=judge_only,
        )
        set_epoch_noise_floor(workspace_root, epoch_id, floor.to_json())
        log.info(
            "A/A noise floor measured for epoch %s (%s, %d draws): "
            "max |delta| = %.6g, delta std = %.6g",
            epoch_id,
            parent_gen.id,
            runs,
            floor.max_abs_delta,
            floor.delta_std,
        )


async def _maybe_contract_preflight(
    *,
    workspace_root: Path,
    epoch_id: str,
    epoch_cfg: Any,
    workspace_config: Any,
    adapter: Any,
    parent_gen: Generation,
    board: list[Any],
    weights: Any,
    config: Any,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
) -> str | None:
    """Measure the contract pre-flight once per epoch; return the verdict.

    DEFAULT-ON (issue #84): unless the runtime opts out
    (:attr:`~zicato.core.runtime.RuntimeConfig.preflight_gate` ``== "off"``),
    the achievable-signal pre-flight is measured once at evolve start — the
    A/A noise floor AND the champion-vs-degraded-copy signal — and its
    verdict persisted onto the epoch record. Idempotent: a later round (or a
    resume) short-circuits on the persisted record and re-reports its verdict
    without re-measuring. Best-effort by contract — a measurement failure
    never aborts the round.

    The number of A/A draws K is taken from the legacy ``config.json``
    ``"contract_preflight": K`` key when present (K >= 2), else defaults to
    :data:`~zicato.tournament.calibration.DEFAULT_CALIBRATION_RUNS`.

    Returns the GATE verdict
    (:func:`~zicato.epoch.preflight.effective_gate_verdict` — ``"refuse"``
    when either the signal verdict or the ``promote_margin`` window refuses,
    else the signal verdict verbatim) when one is available (freshly measured
    or already persisted), else ``None`` (skipped / measurement failed). The
    caller enforces the gate: ``"warn"`` mode only warns (done here);
    ``"refuse"`` mode raises
    :class:`~zicato.epoch.preflight.PreflightRefusedError` on a ``refuse``
    verdict. ``"inert"`` is never a refusal — it says the probe, not the
    contract, came up short (issue #106). ``zicato board preflight`` is the
    manual surface.

    One failure escapes the best-effort contract: a
    :class:`~zicato.epoch.preflight.PreflightConfigError` (an unknown pinned
    mutation id, a probe ceiling wider than the reserved replicate block)
    under ``gate_mode == "refuse"`` raises
    :class:`~zicato.epoch.preflight.PreflightRefusedError` from here. "An
    outage never disqualifies a contract" is about NONDETERMINISTIC infra; a
    config typo is deterministic operator error, and a refuse-mode run that
    proceeds ungated because a knob was misspelled is the outcome that
    operator explicitly ruled out. Under ``"warn"`` it stays the loud warning
    it has always been.
    """
    from zicato.core.runtime import PREFLIGHT_GATE_DEFAULT  # noqa: PLC0415
    from zicato.tournament.calibration import DEFAULT_CALIBRATION_RUNS  # noqa: PLC0415

    gate_mode = str(
        getattr(config, "preflight_gate", PREFLIGHT_GATE_DEFAULT) or PREFLIGHT_GATE_DEFAULT
    )
    raw = workspace_config.get("contract_preflight")
    # Resolve K: an explicit (valid) ``contract_preflight`` key wins; else the
    # calibration default. A malformed / too-small key disables the measurement.
    runs: int | None
    if raw:
        try:
            runs = int(raw)
        except (TypeError, ValueError):
            log.warning("contract_preflight=%r is not an integer; skipping pre-flight", raw)
            runs = None
        else:
            if runs < 2:
                log.warning(
                    "contract_preflight=%d needs at least 2 A/A draws; skipping pre-flight", runs
                )
                runs = None
    else:
        runs = DEFAULT_CALIBRATION_RUNS

    # Opted fully out AND no explicit request ⇒ skip entirely (byte-identical
    # to the pre-#84 behaviour — the deterministic-oracle escape hatch).
    if gate_mode == "off" and not raw:
        return None

    from zicato.epoch.preflight import effective_gate_verdict  # noqa: PLC0415

    # Already measured ⇒ re-report the persisted verdict so the hard gate
    # still applies on a resumed / later round without re-measuring. Collapsed
    # through the SAME helper the fresh path uses, so a resume reaches the
    # identical gate decision as the round that measured.
    existing = getattr(epoch_cfg, "preflight", None)
    if isinstance(existing, dict):
        return effective_gate_verdict(existing)
    if runs is None:
        return None

    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        load_epoch,
        set_epoch_noise_floor,
        set_epoch_preflight,
    )
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_OK,
        PreflightConfigError,
        PreflightRefusedError,
        run_contract_preflight,
    )

    verdict_holder: list[str] = []
    # A probe-selection CONFIG error is the one best-effort failure a
    # refuse-mode operator must not have swallowed. ``best_effort`` exists here
    # because an endpoint outage must never disqualify a contract — but a
    # mistyped ``runtime.preflight_probe_mutation_ids`` id is not an outage:
    # it is deterministic, it will fail identically next round, and swallowing
    # it means a run configured to HARD-GATE proceeds with no gate at all
    # because of a typo. Captured here, escalated after the block (raising from
    # inside ``on_error`` would work but hides the control flow).
    config_error: list[PreflightConfigError] = []

    def _on_preflight_error(exc: BaseException) -> None:
        if isinstance(exc, PreflightConfigError):
            config_error.append(exc)
        log.warning("contract pre-flight skipped: %s", exc)

    with best_effort("contract pre-flight", on_error=_on_preflight_error):
        report, floor = await run_contract_preflight(
            adapter=adapter,
            generation=parent_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            runs=runs,
            disable_drift=disable_drift,
            judge_only=judge_only,
        )
        record = report.to_json()
        set_epoch_preflight(workspace_root, epoch_id, record)
        verdict_holder.append(effective_gate_verdict(record) or report.verdict)
        # The pre-flight's step (a) IS the A/A calibration; persist the
        # floor too when the epoch has none yet (reload — the calibration
        # hook may have written one after ``epoch_cfg`` was loaded), so the
        # margin check + noise-floor detector benefit from the same draws.
        if load_epoch(workspace_root, epoch_id).noise_floor is None:
            set_epoch_noise_floor(workspace_root, epoch_id, floor.to_json())
        if report.verdict == VERDICT_OK and report.window_failure is None:
            log.info(
                "contract pre-flight OK for epoch %s (%s): achievable signal "
                "%.6g clears the measured noise floor %.6g and leaves "
                "promote_margin %.6g inside the window (probed %d point(s))",
                epoch_id,
                parent_gen.id,
                report.signal,
                report.noise_floor_max_abs_delta,
                report.promote_margin,
                report.drawn_probe_count(),
            )
        else:
            # LOUD run-level warning. The diagnosis is per-verdict on purpose:
            # "noise swamps the signal", "the probe was inert", "the margin is
            # unreachable" and "the margin is inside the noise" have four
            # different fixes, and issue #106/#112 both trace operator time
            # wasted to these being reported in the same words. Whether this
            # also STOPS the run is the caller's decision, per ``gate_mode``.
            log.warning(
                "CONTRACT PRE-FLIGHT %s — epoch %s (%s): achievable signal "
                "%.6g vs measured A/A noise floor %.6g vs promote_margin %.6g "
                "(best of %d probed point(s): %s → scalar %.6g). %s %s See the "
                "per-round health report / `zicato board preflight`.",
                (report.window_failure or report.verdict).upper(),
                epoch_id,
                parent_gen.id,
                report.signal,
                report.noise_floor_max_abs_delta,
                report.promote_margin,
                report.drawn_probe_count(),
                report.degraded_mutation_id,
                report.degraded_scalar,
                _preflight_diagnosis(report),
                (
                    "Set runtime.preflight_gate='refuse' to stop such a run "
                    "before it spends rounds."
                    if gate_mode != "refuse"
                    else "Refusing the run (runtime.preflight_gate='refuse')."
                ),
            )
    if config_error and gate_mode == "refuse":
        raise PreflightRefusedError(
            f"contract pre-flight CONFIG ERROR for epoch {epoch_id}: "
            f"{config_error[0]}. The pre-flight could not run at all, so the "
            "hard gate has nothing to gate on — refusing the run rather than "
            "spending rounds unprotected (runtime.preflight_gate='refuse'). Fix "
            "the probe-selection config, or set runtime.preflight_gate='warn' to "
            "proceed with the pre-flight skipped."
        )
    return verdict_holder[0] if verdict_holder else None


def _preflight_diagnosis(report: Any) -> str:
    """The one-sentence "what is wrong and what fixes it" for a pre-flight.

    Kept beside the warning it feeds rather than inside
    :mod:`zicato.epoch.preflight` because it is operator prose for the evolve
    log; the machine-readable equivalents are the verdict constants and the
    health findings (:func:`zicato.health.diagnostics.detect_preflight_verdict`).
    """
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_INERT,
        VERDICT_WARN,
        WINDOW_EMPTY,
        WINDOW_MARGIN_ABOVE_ACHIEVABLE,
        WINDOW_MARGIN_BELOW_FLOOR,
    )

    if report.verdict == VERDICT_INERT:
        return (
            "Every probed mutation point left the scalar exactly at the champion "
            "mean while the A/A draws varied, so the achievable signal is "
            "UNMEASURED rather than zero — the probe was inert, which is NOT "
            "evidence against the contract. Pin a point the deliverable "
            "demonstrably depends on via runtime.preflight_probe_mutation_ids "
            "(or widen the sample with runtime.preflight_probe_points)."
        )
    if report.verdict == VERDICT_WARN:
        return (
            "Scalar spread was exactly zero across every probe — even a "
            "deliberately-degraded tree scored identically to the champion, so "
            "the board cannot discriminate candidates at all. Add expectations "
            "or strengthen judges."
        )
    if report.window_failure == WINDOW_MARGIN_ABOVE_ACHIEVABLE:
        return (
            "promote_margin sits at or above the largest improvement any single "
            "probed mutation point produces, so barring a compound patch no "
            "challenger can be promoted and the run is null by construction. "
            "Lower the margin inside the window (noise < margin < achievable). "
            "The probe degrades ONE point at a time, so this bound is a lower "
            "bound on the loop's reach — if the margin is deliberately above "
            "single-point reach (e.g. recombination is meant to union two "
            "sub-margin fixes) this is expected and informational."
        )
    if report.window_failure == WINDOW_MARGIN_BELOW_FLOOR:
        return (
            "promote_margin sits inside the measured noise, so promotions cannot "
            "be told from re-rolls of the same generation. Raise it above the "
            "noise (the record's recommended_margin scales delta_std, which does "
            "not drift with the draw count) and/or keep the evidence gate on."
        )
    if report.window_failure == WINDOW_EMPTY:
        return (
            "The achievable signal does not clear the noise floor, so the window "
            "noise < margin < achievable is EMPTY and no promote_margin is "
            "defensible — do not tune the margin. Reduce evaluation noise (more "
            "replicates, steadier judges) or strengthen the board."
        )
    return (
        "Under this contract every duel is decided by noise, so no challenger "
        "can be distinguished from the champion. Strengthen the board / reduce "
        "evaluation noise."
    )


def _warn_margin_below_noise_floor(workspace_root: Path, epoch_id: str) -> None:
    """Log loudly when the contract's margin sits inside measured A/A noise.

    Consulted once per evolve invocation (round 0). A WARNING only when the
    evidence gate is OFF — with the gate on, the defer→replicate loop still
    holds promotions to CI separation, so the margin being inside the noise
    is an informational note, not a decision hazard. Never hard-refuses.

    Best-effort like the rest of the health path: the :mod:`zicato.health`
    sibling lands in parallel and may be absent, so a missing
    ``zicato.health.inputs`` is silently inert rather than aborting the run.
    """
    try:
        from zicato.health.inputs import epoch_noise_floor_inputs  # noqa: PLC0415
    except ImportError:
        log.debug("zicato.health.inputs unavailable; skipping margin/noise-floor check")
        return
    from zicato.tournament.calibration import (  # noqa: PLC0415
        MARGIN_NOISE_MULTIPLE,
        margin_below_floor,
        recommended_promote_margin_from_floor,
    )

    floor, margin, gate_on = epoch_noise_floor_inputs(workspace_root, epoch_id)
    if margin is None or not margin_below_floor(margin, floor):
        return
    assert isinstance(floor, dict)  # narrowed by margin_below_floor
    max_abs = float(floor.get("max_abs_delta", 0.0))
    # Recommend from the draw-count-STABLE dispersion, never from the range
    # (issue #112): ``max_abs_delta`` grows with K on an unchanged board, so
    # "set the margin above the measured floor" drifts upward as calibration
    # improves — and a campaign followed it straight past the achievable
    # signal, making every duel a rejection by arithmetic.
    recommended = recommended_promote_margin_from_floor(floor) or max_abs
    if gate_on:
        log.info(
            "promote_margin %.6g is below the measured A/A noise floor %.6g "
            "for epoch %s; the evidence gate is ON, so promotions still "
            "replicate to CI separation",
            margin,
            max_abs,
            epoch_id,
        )
        return
    log.warning(
        "promote_margin %.6g is BELOW the measured A/A noise floor %.6g for "
        "epoch %s and the evidence gate is off: duels decided by the margin "
        "alone CANNOT distinguish a real improvement from a re-roll of the "
        "same generation (measured: a naive margin below the floor promotes "
        "pure noise). RECOMMENDED: raise promote_margin to about %.6g — "
        "%.6g sigma of the measured A/A delta_std, a statistic that does NOT "
        "drift upward as calibration draws accumulate the way the max |delta| "
        "range does — and/or enable the evidence gate — "
        '"promote_confidence_threshold": 0.8 with an honest '
        '"promote_confidence_replicates" budget (the scaffolded contracts '
        "use 32) — so promotions must replicate to CI separation. Check the "
        "result against the pre-flight's achievable signal: a margin ABOVE it "
        "promotes nothing at all. (Floor measured by `zicato board audit`; "
        "this run continues unchanged.)",
        margin,
        max_abs,
        epoch_id,
        recommended,
        MARGIN_NOISE_MULTIPLE,
    )


def _workspace_health_config(workspace_root: Path) -> Any:
    """Resolve the detector thresholds from the workspace ``config.json``.

    The ``health`` block of the workspace config is the operator surface
    for the loop-health thresholds (the former ``ZICATO_HEALTH_*`` env
    vars, deleted); it is parsed by
    :func:`zicato.config.health_config_from_workspace`. Best-effort like
    the rest of the health path: a missing / unreadable / malformed
    workspace config yields ``None`` so ``assess_loop_health`` falls
    back to the defaulted :class:`~zicato.config.HealthConfig` rather
    than blocking the round. (A malformed ``health`` block still fails
    loudly in ``zicato health``, the operator-facing command.)
    """
    try:
        from zicato.config import health_config_from_workspace  # noqa: PLC0415
        from zicato.workspace_loader import load_workspace_config  # noqa: PLC0415

        return health_config_from_workspace(load_workspace_config(workspace_root))
    except Exception as exc:  # noqa: BLE001 — health tuning is best-effort here
        log.debug("workspace health config unavailable (%s); using defaults", exc)
        return None


# _workspace_preflight_gate now lives in zicato.health.inputs alongside its
# fellow health-input readers (imported below).


def _assess_and_persist_loop_health(
    workspace_root: Path,
    epoch_id: str,
    round_n: int,
    board: list[Any],
    infra_outage: tuple[int, int] | None = None,
    token_clip: tuple[int, int] | None = None,
) -> tuple[str, bool]:
    """Run the per-round loop-health check and persist its report.

    ``infra_outage`` — the ``(infra_aborted_runs, threshold)`` pair for a
    round the endpoint-outage circuit deferred — is threaded through to
    :func:`~zicato.health.diagnostics.detect_infra_outage`; ``None``
    (every non-deferred round) is inert. ``token_clip`` — the
    ``(tokens_spent, max_tokens_per_round)`` pair for a round the token
    budget clipped — feeds
    :func:`~zicato.health.diagnostics.detect_token_budget_clip` the same
    way.

    Calls :func:`zicato.health.diagnostics.assess_loop_health` with the
    epoch's accumulated losses, experiments, and board, then writes the
    resulting :class:`LoopHealth` report atomically to
    ``epochs/{epoch}/health/round_{N}.json``.

    Returns a ``(summary, has_critical)`` tuple:

    * ``summary`` — a one-line human-readable health summary for the
      :class:`EvolveRoundOutcome` (empty when the assessment did not
      run).
    * ``has_critical`` — ``True`` when at least one finding is CRITICAL
      (the loop is producing no signal); the caller logs a prominent
      stderr WARNING in that case, and ``evolve_n_rounds`` feeds it to
      :class:`~zicato.evolve.loop.DegenerateHealthPolicy`, which stops the
      run after two consecutive critical rounds. That second consumer is
      why the pre-flight finding's severity is gate-aware
      (:func:`~zicato.health.inputs.workspace_preflight_gate`): a per-round
      re-emission from a persisted record is an unbroken streak, so grading
      it critical under the default ``"warn"`` gate would stop a run the
      operator asked to let run.

    Best-effort: the :mod:`zicato.health` sibling lands in parallel and
    may be absent. A missing module, or any failure assessing or writing
    the report, is logged at ``debug`` level and yields ``("", False)``
    — the round's outcome is never affected by a health-side error.
    """
    try:
        from zicato.health.diagnostics import assess_loop_health  # noqa: PLC0415
    except ImportError:
        log.debug("zicato.health.diagnostics unavailable; skipping loop-health check")
        return "", False

    try:
        from zicato.health.inputs import (  # noqa: PLC0415
            epoch_noise_floor_inputs,
            epoch_preflight_record,
            epoch_tree_import_gaps,
            workspace_preflight_gate,
        )

        losses_by_generation, experiments = _collect_epoch_health_inputs(
            workspace_root, epoch_id, board
        )
        noise_floor, promote_margin, evidence_gate_on = epoch_noise_floor_inputs(
            workspace_root, epoch_id
        )
        # The runtime-event inputs are passed only when a round actually
        # carries one, so an older / stubbed ``assess_loop_health``
        # signature (the sibling-may-lag tolerance this function already
        # documents) keeps working for every ordinary round.
        extra_kwargs: dict[str, Any] = {}
        if infra_outage is not None:
            extra_kwargs["infra_outage"] = infra_outage
        if token_clip is not None:
            extra_kwargs["token_clip"] = token_clip
        tree_import_gaps = epoch_tree_import_gaps(workspace_root, epoch_id)
        if tree_import_gaps:
            extra_kwargs["tree_import_gaps"] = tree_import_gaps
        health = assess_loop_health(
            losses_by_generation,
            experiments,
            board,
            epoch_id,
            config=_workspace_health_config(workspace_root),
            max_generations_per_contract=_epoch_max_generations_per_contract(
                workspace_root, epoch_id
            ),
            noise_floor=noise_floor,
            promote_margin=promote_margin,
            evidence_gate_on=evidence_gate_on,
            preflight=epoch_preflight_record(workspace_root, epoch_id),
            preflight_gate=workspace_preflight_gate(workspace_root),
            **extra_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — health assessment is best-effort
        log.debug("loop-health assessment skipped for %s round %d: %s", epoch_id, round_n, exc)
        return "", False

    summary, has_critical = _summarise_loop_health(health)

    # Promote a "declared judge never fired" finding from a soft, buried
    # health-report entry to a LOUD, operator-visible run-level warning:
    # a judge declared on the board that produced no metric across a whole
    # generation is indistinguishable, to the operator, from one that ran
    # and passed — so it must be surfaced on the terminal, not only in the
    # round's health JSON (issue #84).
    _warn_dead_judges(epoch_id, round_n, health)

    # Same discipline for the judge that could not answer at all: its zero
    # drift is an error artifact, and it makes the round's scalar better than
    # the truth.
    _warn_erroring_judges(epoch_id, round_n, health)

    # Same discipline for the mutated-tree alarm: a generation whose units never
    # imported a mutable tree scored code the loop never changed, which is
    # indistinguishable — from the terminal — from an honest null result.
    _warn_trees_never_imported(epoch_id, round_n, health)

    with best_effort(
        "loop-health report write",
        on_error=lambda exc: log.debug(
            "loop-health report write skipped for %s round %d: %s", epoch_id, round_n, exc
        ),
    ):
        report_path = _health_round_report_path(workspace_root, epoch_id, round_n)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(report_path, _loop_health_to_json(health, epoch_id, round_n))

    return summary, has_critical


def _summarise_loop_health(health: Any) -> tuple[str, bool]:
    """Derive a one-line summary + critical flag from a ``LoopHealth`` object.

    Tolerant of the sibling's exact :class:`LoopHealth` shape: it is
    documented to expose ``.findings`` and ``.healthy``, and each finding
    is expected to carry a ``severity`` (string) and a ``message`` /
    ``summary`` / ``detail`` text field. Anything missing is filled in
    defensively so a schema drift in the sibling never raises here.
    """
    findings = list(getattr(health, "findings", ()) or ())
    healthy = bool(getattr(health, "healthy", not findings))

    def _severity(f: Any) -> str:
        return str(getattr(f, "severity", "") or "").upper()

    critical = [f for f in findings if _severity(f) == "CRITICAL"]
    has_critical = bool(critical)

    if not findings:
        return ("loop healthy" if healthy else "loop health: no findings"), False

    def _text(f: Any) -> str:
        for attr in ("message", "summary", "detail", "description"):
            val = getattr(f, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return str(f)

    if has_critical:
        head = _text(critical[0])
        extra = f" (+{len(critical) - 1} more critical)" if len(critical) > 1 else ""
        return f"CRITICAL: {head}{extra}", True

    head = _text(findings[0])
    extra = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"{len(findings)} finding(s): {head}{extra}", False


def _loop_health_to_json(health: Any, epoch_id: str, round_n: int) -> str:
    """Serialize a ``LoopHealth`` object to a pretty-printed JSON string.

    Uses :func:`dataclasses.asdict` when the sibling's :class:`LoopHealth`
    is a dataclass; otherwise falls back to reading ``.healthy`` /
    ``.findings`` and coercing each finding via :func:`dataclasses.asdict`
    or ``vars()``. ``epoch_id`` / ``round`` / ``assessed_at`` are stamped
    on so the report is self-describing for the dashboard.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415

    def _coerce(obj: Any) -> Any:
        if _dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return _dataclasses.asdict(obj)
        if hasattr(obj, "__dict__"):
            return dict(vars(obj))
        return obj

    body: dict[str, Any]
    if _dataclasses.is_dataclass(health) and not isinstance(health, type):
        body = _dataclasses.asdict(health)
    else:
        body = {
            "healthy": bool(getattr(health, "healthy", False)),
            "findings": [_coerce(f) for f in getattr(health, "findings", ()) or ()],
        }
    summary, has_critical = _summarise_loop_health(health)
    body.update(
        {
            "epoch_id": epoch_id,
            "round": round_n,
            "assessed_at": _now_iso(),
            "summary": summary,
            "has_critical": has_critical,
        }
    )
    return json.dumps(body, default=str, indent=2, sort_keys=True) + "\n"


def _warn_loop_no_signal(epoch_id: str, round_n: int, summary: str) -> None:
    """Emit a prominent stderr WARNING that the evolve loop has no signal.

    Called when a round's loop-health assessment surfaces a CRITICAL
    finding (e.g. degenerate scoring — every generation scoring the same,
    so the tournament can never tell a real improvement from noise). The
    operator must see this: a loop that produces no signal will burn LLM
    calls forever without ever promoting anything meaningful.

    The message goes to both the logger (``warning`` level) and, via the
    logger's default stderr handler, the operator's terminal.
    """
    log.warning(
        "LOOP HEALTH CRITICAL — epoch %s round %d: %s. "
        "The evolve loop is producing no usable signal; inspect the "
        "scoring weights / proposer brief before spending more LLM calls.",
        epoch_id,
        round_n,
        summary or "degenerate scoring",
    )


def _warn_dead_judges(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for any board-declared judge that never fired.

    The ``dead_judge`` loop-health finding (a board-declared process judge
    that produced no ``custom:<name>`` metric across the whole epoch) is a
    recommend-only ``warning`` in the health report — easy to miss in the
    per-round JSON. This lifts it to a run-level operator-facing WARNING on
    the terminal: a declared judge that contributes no metric is either
    mis-wired (the events it keys on are never emitted / the harness never
    invokes it) or its criterion is unreachable, and an operator cannot
    tell it apart from a judge that ran and passed. Fired every round the
    finding is present (idempotent, best-effort).

    Tolerant of the health sibling's exact shape: it scans ``.findings``
    for the stable ``code == "dead_judge"`` and reads the finding's
    ``detail["dead_judges"]`` / ``summary`` defensively, so a schema drift
    never raises here.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "dead_judge":
            continue
        detail = getattr(finding, "detail", None)
        dead = detail.get("dead_judges") if isinstance(detail, dict) else None
        named = ", ".join(repr(str(n)) for n in dead) if isinstance(dead, list | tuple) else ""
        summary = str(getattr(finding, "summary", "") or "")
        log.warning(
            "DECLARED JUDGE NEVER FIRED — epoch %s round %d: %s%s "
            "A judge declared on the board produced no metric across the whole "
            "generation: it is either mis-wired (the events it keys on are never "
            "emitted, or the harness never invokes it) or its criterion is "
            "unreachable — an operator cannot tell it apart from a judge that ran "
            "and passed. Confirm each judge is wired to events that fire and its "
            "criterion is reachable (see zicato-design-judges).",
            epoch_id,
            round_n,
            summary or "a declared judge never fired",
            f" (dead: {named})" if named else "",
        )
        return


def _warn_erroring_judges(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for a board-declared judge that RAISED.

    The sibling of :func:`_warn_dead_judges`, for the failure it used to be
    confused with (issue #121). A judge whose callable raised produced no
    verdict at all, but every layer below swallows the exception — zicato's
    judge boundary and goldfive's steerer both catch by hard contract — and
    goldfive emits no event for the empty verdict that results. So the round
    scored with that judge's signal silently missing, and its zero drift
    made the generation look BETTER than the evidence supports. That is a
    result the operator must see on the terminal in the round it happens,
    not in a per-round JSON read afterwards.

    Tolerant of the health sibling's exact shape: it scans ``.findings`` for
    the stable ``code == "judge_erroring"`` and reads the finding's
    ``summary`` / ``detail`` defensively, so a schema drift never raises here.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "judge_erroring":
            continue
        detail = getattr(finding, "detail", None)
        recommendation = detail.get("recommendation") if isinstance(detail, dict) else None
        summary = str(getattr(finding, "summary", "") or "")
        log.warning(
            "DECLARED JUDGE RAISED — epoch %s round %d: %s %s",
            epoch_id,
            round_n,
            summary or "a declared judge failed on every invocation",
            str(recommendation)
            or (
                "a judge that raised did not decide anything: its silence lowered "
                "this round's drift loss without evidence. Check the judge / "
                "auxiliary endpoint and model config."
            ),
        )
        return


def _warn_trees_never_imported(epoch_id: str, round_n: int, health: Any) -> None:
    """Emit a prominent stderr WARNING for a tree no unit of a generation imported.

    The ``tree_never_imported`` finding says the loop mutated code that was
    never executed: the board scored, the gate fired and the round promoted or
    rejected on a comparison between two identical unmutated trees. Buried in
    the per-round health JSON it reads like any other warning, and an operator
    cannot tell "the mutations did not help" from "the mutations were never
    under test" — the same argument that lifted ``dead_judge`` to the terminal
    (:func:`_warn_dead_judges`). Fired every round the finding is present
    (idempotent, best-effort), and tolerant of the health sibling's exact shape.
    """
    for finding in getattr(health, "findings", ()) or ():
        if str(getattr(finding, "code", "") or "") != "tree_never_imported":
            continue
        log.warning(
            "MUTATED TREE NEVER IMPORTED — epoch %s round %d: %s. The round's "
            "verdict compared two IDENTICAL unmutated trees, so it carries no "
            "optimization signal. Check that the harness entrypoint imports the "
            "mutable tree rather than an installed copy under another name, and "
            "that the board exercises the code path the mutations target; the "
            "per-generation record is generations/<gen>/harness_load.json "
            "(issue #110).",
            epoch_id,
            round_n,
            str(getattr(finding, "summary", "") or "a mutable tree was never imported"),
        )


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (``.tmp`` + :func:`os.replace`).

    Delegates to the single atomic-write definition in
    :mod:`zicato.storage._atomic` so there is one ``.tmp`` + ``fsync`` +
    rename implementation in the codebase.
    """
    from zicato.storage._atomic import atomic_write_text as _atomic_write_text_impl  # noqa: PLC0415

    _atomic_write_text_impl(path, text)


def _dump_mutations_snapshot(
    workspace_root: Path,
    epoch_id: str,
    mutations: list[Any],
) -> None:
    """Serialize the round's enumerated mutation points to ``mutations.json``.

    Writes a JSON array of objects ``{id, kind, file, line_start,
    line_end, content, content_hash}`` — i.e. :func:`dataclasses.asdict`
    of each :class:`zicato.core.types.MutationPoint` with the ``Path``
    fields stringified — to ``epochs/{epoch_id}/mutations.json``. The
    write is atomic (``.tmp`` + :func:`os.replace`).

    Best-effort: any failure (a serialisation error, an I/O error) is
    swallowed at ``debug`` level so a broken snapshot can never abort the
    evolve round. The proposer has already been fed the in-memory
    ``mutations`` list by the time this runs; the on-disk file is purely
    for the dashboard.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415
    import os as _os  # noqa: PLC0415

    from zicato.core.workspace import mutations_json_path  # noqa: PLC0415

    with best_effort(
        "mutations.json snapshot",
        on_error=lambda exc: log.debug("mutations.json snapshot skipped: %s", exc),
    ):
        payload: list[dict[str, Any]] = []
        for point in mutations:
            raw = _dataclasses.asdict(point)
            payload.append(
                {
                    "id": raw["id"],
                    "kind": raw["kind"],
                    "file": str(raw["file"]),
                    "line_start": raw["line_start"],
                    "line_end": raw["line_end"],
                    "content": raw["content"],
                    "content_hash": raw["content_hash"],
                }
            )
        target = mutations_json_path(workspace_root, epoch_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        _os.replace(tmp, target)


def _ensure_baseline_snapshot(
    workspace_root: Path,
    epoch_id: str,
    workspace_config: Any,
) -> None:
    """Seed a ``v0`` snapshot for the epoch if no generations exist yet.

    Two seed sources, in priority order:

    1. **Cross-epoch lineage seed.** When the epoch was created by a
       contract-roll, :func:`ensure_epoch_for_contract` leaves a
       ``v0_seed_from`` marker pointing at the previous epoch's
       promoted-head snapshot. The new epoch's ``v0`` is seeded from
       that snapshot so the lineage continues from the best result of
       the old epoch rather than restarting from the registered
       source.
    2. **Registered mutable trees.** The default for a fresh, non-rolled
       epoch (or a rolled epoch whose predecessor had no promoted
       generation beyond v0). Each registered ``mutable_trees`` root is
       copied under ``epochs/{epoch}/generations/v0/snapshot/{name}/``.

    Subsequent invocations are a no-op when ``v0`` already exists.

    The seed snapshot is also recorded in lineage (as the unparented
    promoted head) and marked as the current generation; the same
    bookkeeping the post-promotion path performs after every successful
    round. This keeps lineage truthful when the epoch is later
    summarised by the analysis pass.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    store = default_generation_store(workspace_root)
    if store.list_generations(epoch_id):
        return  # already have at least one generation; nothing to do

    # Priority 1 — cross-epoch lineage seed left by a contract-roll.
    # The seed marker points at the *snapshot directory* of the
    # predecessor epoch's promoted head; its CHILDREN become the new
    # v0's top-level trees (the roll continues the lineage rather than
    # nesting it one level deeper). seed_generation copies each source
    # under its basename, so handing it the children reproduces the
    # pre-seam flatten-into-v0 behaviour.
    seed_marker = _roll_seed_marker(workspace_root, epoch_id)
    seeded_from_roll = False
    roll_source: tuple[str, str] | None = None  # (source_epoch, source_generation)
    if seed_marker.exists():
        seed_text = seed_marker.read_text(encoding="utf-8").strip()
        seed_source = Path(seed_text) if seed_text else None
        if seed_source is not None and seed_source.exists():
            store.seed_generation(epoch_id, "v0", sorted(seed_source.iterdir()))
            seeded_from_roll = True
            roll_source = _source_epoch_generation(seed_source)
            log.info(
                "epoch %s: seeded v0 from rolled predecessor snapshot %s",
                epoch_id,
                seed_source,
            )

    # Priority 2 — registered mutable trees.
    if not seeded_from_roll:
        raw_trees = (
            workspace_config.get("mutable_trees") or workspace_config.get("source_roots") or []
        )
        if not raw_trees:
            raise RuntimeError(
                "evolve_once: workspace_config has no 'mutable_trees' / "
                "'source_roots' — cannot seed a v0 baseline snapshot. "
                "Run `zicato register --mutable-tree ...` first."
            )
        # seed_generation copies each registered tree under its basename
        # and raises FileNotFoundError for a missing source — the same
        # contract the inline loop enforced.
        store.seed_generation(epoch_id, "v0", [Path(raw) for raw in raw_trees])

    snapshot_root = store.snapshot_root(epoch_id, "v0")

    # Lineage + current-generation marker so the orchestrator's
    # downstream readers see a clean baseline state.
    from zicato.epoch import append_to_lineage  # noqa: PLC0415

    baseline_gen = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snapshot_root,
        created_at=_now_iso(),
        promoted=True,
    )
    append_to_lineage(workspace_root, epoch_id, baseline_gen, parent_id=None)
    _set_current_generation(workspace_root, epoch_id, "v0")

    # Synthetic ``experiment.json`` for v0 so every downstream consumer
    # (the analyzer report data loader, the index dual-write, the
    # dashboard lineage walker) sees a uniform on-disk shape. The seed is
    # not a proposer experiment; the marker carries a "baseline seed"
    # hypothesis and a null outcome (no tournament round produced it).
    # Idempotent — safe to call again on a workspace whose v0 already
    # has the marker.
    from zicato.epoch.journal import write_seed_experiment  # noqa: PLC0415

    write_seed_experiment(
        workspace_root,
        epoch_id,
        "v0",
        proposed_at=baseline_gen.created_at,
    )

    # Champion self-containment: when this epoch carried the champion
    # forward from a rolled predecessor, MATERIALISE the carried-over
    # per-board losses + aggregate into the new epoch's ``v0`` gen dir,
    # each tagged ``cached: true`` with ``source_epoch`` / ``source_run``
    # provenance. Without this the champion would be a hollow shell — only
    # ``experiment.json`` + ``snapshot/`` — while the challengers carry
    # their ``loss.json`` files, so the epoch would not be self-contained
    # and a fast first round would degrade to a full champion re-run. With
    # the losses materialised, the champion is consistent with the
    # challengers (both materialised per-board, distinguished only by the
    # ``cached`` provenance) and the cache-first runner reuses it from the
    # very first round.
    if roll_source is not None:
        _materialize_carried_champion(
            workspace_root,
            epoch_id=epoch_id,
            generation_id="v0",
            source_epoch=roll_source[0],
            source_generation=roll_source[1],
        )


def _source_epoch_generation(seed_source: Path) -> tuple[str, str] | None:
    """Derive ``(source_epoch, source_generation)`` from a roll-seed snapshot path.

    The cross-epoch roll-seed marker points at the predecessor's
    promoted-head snapshot directory, of the form
    ``…/epochs/<epoch>/generations/<gen>/snapshot``. This recovers the
    ``(epoch, generation)`` pair so the champion's prior losses can be
    materialised into the new epoch with honest provenance. Returns
    ``None`` when the path does not match the expected layout (a
    hand-built marker, a future relayout) — materialisation is then
    skipped, which is a clean degrade rather than a crash.
    """
    parts = seed_source.parts
    try:
        # …/epochs/<epoch>/generations/<gen>/snapshot
        snap_i = len(parts) - 1 - parts[::-1].index("snapshot")
    except ValueError:
        return None
    # Expect ["generations", <gen>, "snapshot"] ending and an "epochs"
    # marker two levels above the generation id.
    if snap_i < 4 or parts[snap_i - 2] != "generations" or parts[snap_i - 4] != "epochs":
        return None
    source_generation = parts[snap_i - 1]
    source_epoch = parts[snap_i - 3]
    return source_epoch, source_generation


def _materialize_carried_champion(
    workspace_root: Path,
    *,
    epoch_id: str,
    generation_id: str,
    source_epoch: str,
    source_generation: str,
) -> None:
    """Copy a carried-over champion's per-board losses + aggregate into this epoch.

    Best-effort. Reads every per-board ``loss.json`` (and per-replicate
    ``loss.r<r>.json``) the champion produced in ``source_epoch`` /
    ``source_generation`` and rewrites each into THIS epoch's
    ``generations/<generation_id>/runs/<entry>/`` with ``cached=True`` and
    ``source_epoch`` / ``source_run`` provenance (``source_run`` is the
    original run id, so the trail back to the live evaluation survives).
    The champion's ``gen_score.json`` aggregate is likewise copied with the
    same provenance fields so a fast first round reuses it. Each
    materialised run is folded into the analytical index so the champion
    reads as scored-but-cached within the epoch (the index's ``cached``
    column keeps it from being double-counted as a fresh evaluation).

    A missing source (the predecessor never scored its head), an
    unreadable file, or an absent reducer degrades to "materialise what we
    can" — never an abort. The champion's run id in the new epoch keeps
    the canonical ``{generation_id}--{entry_id}`` form so the cache-first
    runner finds it as a hit.
    """
    from zicato.core.workspace import run_dir  # noqa: PLC0415

    try:
        from zicato.telemetry.reducer import (  # noqa: PLC0415
            read_loss_profile,
            write_loss_profile,
        )
    except ImportError as exc:
        # The reducer (de)serialisers are unavailable in this environment
        # (e.g. a test that stubs out ``zicato.telemetry``). Materialising
        # carried losses is best-effort — degrade to "carry nothing"
        # rather than aborting the epoch's baseline seed.
        log.debug("materialise champion: reducer unavailable (%s); skipping", exc)
        return

    src_gen_dir = generation_dir(workspace_root, source_epoch, source_generation)
    src_runs_root = src_gen_dir / "runs"
    materialised_entries: list[str] = []
    if src_runs_root.exists():
        for entry_dir in sorted(p for p in src_runs_root.iterdir() if p.is_dir()):
            entry_id = entry_dir.name
            dst_run_dir = run_dir(workspace_root, epoch_id, generation_id, entry_id)
            any_for_entry = False
            # Canonical loss.json (replicate 0) + any loss.r<r>.json siblings.
            for src_loss in sorted(entry_dir.glob("loss*.json")):
                try:
                    profile = read_loss_profile(src_loss)
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    log.debug("materialise champion: unreadable %s: %s", src_loss, exc)
                    continue
                carried = replace(
                    profile,
                    generation_id=generation_id,
                    epoch_id=epoch_id,
                    run_id=f"{generation_id}--{entry_id}"
                    + ("" if src_loss.name == "loss.json" else f"--{src_loss.stem}"),
                    cached=True,
                    source_epoch=source_epoch,
                    source_run=profile.run_id,
                )
                try:
                    write_loss_profile(carried, dst_run_dir / src_loss.name)
                    any_for_entry = True
                except OSError as exc:
                    log.debug("materialise champion: write %s skipped: %s", src_loss.name, exc)
            if any_for_entry:
                materialised_entries.append(entry_id)

    # Carry the aggregate (gen_score.json) with the same provenance so a
    # fast first round reuses the champion rather than re-running it.
    src_score = src_gen_dir / "gen_score.json"
    if src_score.exists():
        try:
            raw = json.loads(src_score.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("materialise champion: gen_score read skipped: %s", exc)
            raw = None
        if isinstance(raw, dict):
            raw["generation_id"] = generation_id
            raw["cached"] = True
            raw["source_epoch"] = source_epoch
            raw["source_run"] = source_generation
            _cache_gen_score(workspace_root, epoch_id, generation_id, raw)

    # Fold the materialised runs into the analytical index so the champion
    # reads as scored-but-cached within the epoch.
    for entry_id in materialised_entries:
        try:
            from zicato.index.ingest import ingest_run  # noqa: PLC0415

            ingest_run(
                workspace_root,
                _index_db_path(workspace_root),
                epoch_id,
                generation_id,
                entry_id,
            )
        except ImportError:
            break
        except Exception as exc:  # noqa: BLE001 — index dual-write is best-effort
            log.debug("materialise champion: index ingest %s skipped: %s", entry_id, exc)
    if materialised_entries:
        log.info(
            "epoch %s: materialised carried champion %s from %s/%s (%d board entries, cached)",
            epoch_id,
            generation_id,
            source_epoch,
            source_generation,
            len(materialised_entries),
        )


def _load_historical_aggregate(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Read the parent's cached ``gen_score.json``.

    Raises :class:`FileNotFoundError` when the cache is missing — fast
    mode is meaningless without a parent aggregate.
    """
    gdir = generation_dir(workspace_root, epoch_id, generation_id)
    path = gdir / "gen_score.json"
    if not path.exists():
        raise FileNotFoundError(
            f"fast-mode evolve needs a cached parent aggregate at {path}; "
            "run a full round for the parent generation first"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object at top level")
    raw.setdefault("generation_id", generation_id)
    return raw


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

# The N-round loop lives in ``zicato.evolve.loop``; imported HERE (at the
# bottom, after ``EvolveRoundOutcome`` and every collaborator the loop
# resolves through this module are defined) so the loop module's lazy
# back-reference to the orchestrator never sees a half-built module.
# Re-exported so ``from zicato.orchestrator import evolve_n_rounds`` — and
# the test suite's ``orch.evolve_n_rounds`` monkeypatch — keep resolving.
from zicato.evolve.loop import evolve_n_rounds  # noqa: E402

__all__ = [
    "EvolveRoundOutcome",
    # Re-export shims for the round-pipeline ROUND-CONTEXT stage (the
    # pre-propose context builders) moved to ``zicato.evolve.round_context``.
    # All are also used inside this module; the two listed here are referenced
    # from outside the orchestrator (the candidate-screen + best-of-N test
    # modules), so they are listed for mypy's no-implicit-reexport. The three
    # purely internal builders (``_build_recombination_pair``,
    # ``_build_genealogy_items``, ``_build_calibration_summary``) are not
    # listed.
    "_build_candidate_screen_runner",
    "_recombine_pair_for_slot",
    # Re-export shims for the round-pipeline PROPOSE/APPLY stage (mint one
    # challenger, admit it to the field) moved to
    # ``zicato.evolve.propose_apply``. The names this module's own body still
    # calls are re-imported above; the four listed here are referenced from
    # OUTSIDE the orchestrator — ``_propose_and_apply_challenger`` (the
    # multi-challenger test), ``_mint_challenger_field`` (the decomposition
    # test), ``_diversity_signature`` (the best-of-N tree-integrity test), and
    # ``_max_overlap_with_accepted`` (the prior-experiments test) — so they are
    # listed for mypy's no-implicit-reexport. ``_max_overlap_with_accepted`` is
    # not otherwise used in this module; the other three are.
    "_diversity_signature",
    "_max_overlap_with_accepted",
    "_mint_challenger_field",
    "_propose_and_apply_challenger",
    # Re-export shims for the round-pipeline GATE stage (the promotion
    # decision procedure) moved to ``zicato.evolve.gate``. All are also used
    # inside this module; the seven listed here are referenced from outside
    # the orchestrator (the decision-procedure + decomposition test modules),
    # so they are listed so ``from zicato.orchestrator import
    # _gauntlet_decision_from_result`` and ``orch._apply_field_overrides``
    # keep resolving under mypy's no-implicit-reexport. The three purely
    # internal selectors (``_field_override_provenance``,
    # ``_first_aggregate_for``, ``_registered_mutable_trees``) are not listed.
    "_CrowningHoldout",
    "_apply_field_overrides",
    "_confirm_crowning_on_holdout",
    "_confirm_gauntlet_promotion",
    "_gauntlet_decision_from_result",
    "_integrity_block_reason",
    "_resolve_round_champion_mode",
    # Re-export shims for the round-pipeline INGEST stage (the live SQLite
    # index IO) moved to ``zicato.evolve.ingest``. Both are also used inside
    # this module; listed so external callers' ``from zicato.orchestrator
    # import _load_prior_experiments`` (the propose CLI) and ``_index_db_path``
    # (the dashboard projection) keep resolving under mypy's no-implicit-reexport.
    "_index_db_path",
    "_load_prior_experiments",
    # Re-export shims for lifecycle services moved to
    # ``zicato.evolve.lifecycle_services`` — listed here so the import is
    # recognised as a re-export (and so callers' ``from zicato.orchestrator
    # import _NoopShutdownHandle`` keeps resolving). These four are not used
    # inside this module; the rest of the moved names are.
    "_EnvVarRestorer",
    "_LaunchedHandle",
    "_NoopShutdownHandle",
    "_now_iso",
    "_resolve_harmonograf_url",
    # Re-export shims for the contract-hash auto-epoching helpers moved to
    # ``zicato.evolve.epoching``. These five are not used inside this
    # module (only ``ensure_epoch_for_contract`` and ``_roll_seed_marker``
    # are); listed so the import is recognised as a re-export and callers'
    # ``from zicato.orchestrator import _create_epoch_from_contract`` keep
    # resolving.
    "_component_diff_label",
    "_create_epoch_from_contract",
    "_promoted_head_snapshot",
    "_stored_component_hashes",
    "_write_component_hashes",
    "ensure_epoch_for_contract",
    # Collaborators the N-round loop (``zicato.evolve.loop``) resolves
    # through THIS module object at call time so the test suite's
    # monkeypatches keep biting. Some are no longer referenced in the
    # orchestrator body (the loop moved out); others (``claim_skip_round``,
    # ``_safe_resolve_parent``) are still used here too. All must stay
    # attributes of this module and reachable as exports for the loop's
    # ``_orch.<name>`` access.
    "_build_meta_loop_emitter_safe",
    "_mark_run_terminal",
    "_resolve_or_launch_harmonograf",
    "_safe_resolve_parent",
    "block_while_paused",
    "claim_rubric_replacement",
    "claim_skip_round",
    "evolve_once",
    "evolve_n_rounds",
]
# ``experiment_json_path`` is referenced in the module docstring's
# "step 11" — kept imported here as a re-export hook for tests that
# want to assert on the persistence target without re-deriving it.
_ = experiment_json_path
