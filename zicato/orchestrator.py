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

import datetime as _dt
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core.types import (
    Generation,
    OutcomeRecord,
)
from zicato.core.workspace import (
    experiment_json_path,
    generation_dir,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import acquire_workspace_lock, release_workspace_lock

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


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
    rejection_reason:
        Symbolic / human-readable string when the round did not
        promote. Empty string on a successful promotion.
    parent_scalar:
        Parent generation's scalar score (drift + pass terms weighted).
    child_scalar:
        Child generation's scalar score.
    delta_scalar:
        ``child_scalar - parent_scalar``. Negative = improvement.
    """

    parent_generation_id: str
    proposed_generation_id: str
    tournament_decision: str
    rejection_reason: str
    parent_scalar: float
    child_scalar: float
    delta_scalar: float


# ---------------------------------------------------------------------------
# Contract-hash auto-epoching
# ---------------------------------------------------------------------------


#: Internal sentinel: workspace-level state file recording, for each
#: epoch, where its v0 baseline should be seeded from when the epoch is
#: a contract-roll of a predecessor. Keyed by epoch id; value is the
#: absolute path to the previous epoch's promoted-head snapshot. The
#: file is written by :func:`ensure_epoch_for_contract` and consumed by
#: :func:`_ensure_baseline_snapshot`.
def _roll_seed_marker(workspace_root: Path, epoch_id: str) -> Path:
    return workspace_root / "epochs" / epoch_id / "v0_seed_from"


def _component_diff_label(prev_components: dict[str, str], cur_components: dict[str, str]) -> str:
    """Return a human-readable label naming which contract components moved.

    Compares the per-component sub-hashes; returns a comma-joined list
    of the component names that differ (``board``, ``rubric``,
    ``scoring``, ``entrypoint``, ``mutable_trees``). Falls back to a
    generic ``"contract"`` when no per-component breakdown is available
    (e.g. a legacy epoch with no stored components).
    """
    if not prev_components:
        return "contract"
    changed = [
        name for name, cur_hash in cur_components.items() if prev_components.get(name) != cur_hash
    ]
    return ", ".join(changed) if changed else "contract"


def _stored_component_hashes(workspace_root: Path, epoch_id: str) -> dict[str, str]:
    """Return the per-component sub-hashes recorded for an epoch.

    The breakdown is written next to ``config.json`` as
    ``contract_components.json`` at epoch creation / roll time. Absent
    for legacy epochs (returns an empty dict — the caller falls back to
    a generic message).
    """
    path = workspace_root / "epochs" / epoch_id / "contract_components.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _write_component_hashes(
    workspace_root: Path, epoch_id: str, components: dict[str, str]
) -> None:
    """Persist an epoch's per-component contract sub-hashes."""
    path = workspace_root / "epochs" / epoch_id / "contract_components.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(components, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def ensure_epoch_for_contract(
    workspace_root: Path,
    *,
    auto_epoch: bool,
    aux_call_llm: CallLLM,
    epoch_name: str | None = None,
) -> str:
    """Resolve the epoch ``evolve`` should run against, auto-rolling on drift.

    The "evaluation contract" is the board + rubric + scoring + the
    registered inner-harness identity (entrypoint + mutable trees). A
    change to any of those means generations on either side are no
    longer comparable, so the epoch must roll. This function is the
    roll-at-evolve-time hook: it is called before the orchestrator
    resolves an epoch.

    Logic:

    1. Compute the current contract hash via
       :func:`zicato.epoch.contract.compute_contract_hash`.
    2. ``cur = current_epoch_id(workspace_root)``.
    3. If ``cur`` is ``None``:

       * ``auto_epoch`` True  — create epoch ``e0`` from the contract,
         return it.
       * ``auto_epoch`` False — raise (tell the operator to
         ``zicato epoch new``).
    4. Load ``cur``'s :class:`EpochConfig`.

       * If ``cur.contract_hash == ""`` (legacy epoch) OR ``== `` the
         current hash: return ``cur`` (continue, no roll).
       * Else (the contract changed):

         * ``auto_epoch`` True  — close ``cur`` (generating
           ``analysis.md``), create a NEW epoch carrying the new
           contract, baselined from ``cur``'s promoted head, auto-named
           ``e{N+1}``; echo a clear message; return the new id.
         * ``auto_epoch`` False — raise a clear error: the contract
           drifted from the current epoch; revert the files or run
           ``zicato epoch new``.

    ``epoch_name`` overrides the default ``e{N}`` auto-name for any
    epoch this function creates (the first epoch on a fresh workspace,
    or the new epoch after a roll). When ``None``, the ``e{N}`` scheme
    is used.

    Returns the epoch id ``evolve`` should use.
    """
    from zicato.epoch.contract import (  # noqa: PLC0415
        compute_component_hashes,
        compute_contract_hash,
        resolve_contract_inputs,
    )
    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        current_epoch_id,
        list_epochs,
        load_epoch,
    )

    inputs = resolve_contract_inputs(workspace_root)
    current_hash = compute_contract_hash(inputs)
    current_components = compute_component_hashes(inputs)

    cur = current_epoch_id(workspace_root)
    if cur is None:
        if not auto_epoch:
            raise FileNotFoundError(
                f"no current_epoch marker under {workspace_root}; "
                "run `zicato epoch new <name> ...` or drop --no-auto-epoch "
                "so `zicato evolve` can create the first epoch"
            )
        new_id = _create_epoch_from_contract(
            workspace_root,
            inputs=inputs,
            name=epoch_name or "e0",
            aux_call_llm=aux_call_llm,
        )
        _write_component_hashes(workspace_root, new_id, current_components)
        return new_id

    cfg = load_epoch(workspace_root, cur)
    if cfg.contract_hash == "" or cfg.contract_hash == current_hash:
        # Legacy epoch (empty hash → treated as always-matching) or the
        # contract is unchanged. Either way: no roll.
        return cur

    # The contract drifted from the current epoch.
    if not auto_epoch:
        drifted = _component_diff_label(
            _stored_component_hashes(workspace_root, cur), current_components
        )
        raise RuntimeError(
            f"evaluation contract has drifted from the current epoch "
            f"{cur!r} (changed: {drifted}); either revert the contract "
            "files or run `zicato epoch new` to start a new epoch. "
            "(Remove --no-auto-epoch to let `zicato evolve` roll the "
            "epoch automatically.)"
        )

    # Auto-roll: close the drifted epoch, open a fresh one carrying the
    # new contract, baselined from the closed epoch's promoted head.
    # close_epoch_async is awaited (we are already inside an event loop;
    # the sync close_epoch would nest asyncio.run and raise).
    from zicato.epoch.lifecycle import close_epoch_async  # noqa: PLC0415

    await close_epoch_async(workspace_root, cur, aux_call_llm=aux_call_llm)

    next_n = len(list_epochs(workspace_root))
    new_id = _create_epoch_from_contract(
        workspace_root,
        inputs=inputs,
        name=epoch_name or f"e{next_n}",
        aux_call_llm=aux_call_llm,
    )
    _write_component_hashes(workspace_root, new_id, current_components)

    # Record where the new epoch's v0 should be seeded from: the
    # promoted head of the epoch we just closed. `_ensure_baseline_snapshot`
    # reads this marker on the first evolve round of the new epoch.
    prev_head_snapshot = _promoted_head_snapshot(workspace_root, cur)
    if prev_head_snapshot is not None:
        _roll_seed_marker(workspace_root, new_id).write_text(
            str(prev_head_snapshot) + "\n", encoding="utf-8"
        )

    changed = _component_diff_label(
        _stored_component_hashes(workspace_root, cur), current_components
    )
    log.info("contract changed (%s) — rolled %s -> %s", changed, cur, new_id)
    print(f"contract changed ({changed}) — rolled {cur} -> {new_id}")
    return new_id


def _create_epoch_from_contract(
    workspace_root: Path,
    *,
    inputs: Any,
    name: str,
    aux_call_llm: CallLLM,
) -> str:
    """Create an epoch from resolved contract inputs; return its id.

    A thin wrapper over :func:`zicato.epoch.lifecycle.new_epoch` that
    loads the scoring weights from the live ``scoring.json`` and carries
    the registered inner-harness identity into the contract hash.
    """
    from zicato.epoch.lifecycle import new_epoch  # noqa: PLC0415
    from zicato.workspace_loader import _scoring_weights_from_dict  # noqa: PLC0415

    if inputs.scoring_path.exists():
        weights = _scoring_weights_from_dict(
            json.loads(inputs.scoring_path.read_text(encoding="utf-8"))
        )
    else:
        from zicato.core.types import ScoringWeights  # noqa: PLC0415

        weights = ScoringWeights()

    cfg = new_epoch(
        workspace_root=workspace_root,
        name=name,
        board_source=inputs.board_path,
        rubric_source=inputs.rubric_path,
        weights=weights,
        auto_close_previous=False,  # ensure_epoch_for_contract closes explicitly
        aux_call_llm=aux_call_llm,
        entrypoint=inputs.entrypoint,
        mutable_trees=tuple(inputs.mutable_trees),
    )
    return cfg.id


def _promoted_head_snapshot(workspace_root: Path, epoch_id: str) -> Path | None:
    """Return the snapshot dir of an epoch's last promoted generation.

    Reads the epoch's ``current_generation`` marker (the promoted head)
    and returns that generation's ``snapshot/`` directory. Returns
    ``None`` when the epoch has no promoted generation beyond a seed
    that was never run, or when the snapshot directory is absent — the
    caller then falls back to seeding from the registered mutable trees.
    """
    try:
        head = _resolve_current_generation(workspace_root, epoch_id)
    except FileNotFoundError:
        return None
    snap = _snapshot_root(workspace_root, epoch_id, head)
    if not snap.exists() or not any(snap.iterdir()):
        return None
    return snap


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
) -> EvolveRoundOutcome:
    """Run ONE evolve round against the current epoch.

    Steps:

    1. Load the workspace config and the current epoch (board, rubric,
       scoring, adapter via the workspace's adapter factory).
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
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
        update_experiment_outcome,
        write_experiment,
    )
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.mutation.validator import (  # noqa: PLC0415
        check_forbidden_ids,
        validate_post_apply,
    )
    from zicato.patterns.detectors import (  # noqa: PLC0415
        ALL_DETECTORS,
        DetectorInput,
        detect_patterns,
    )
    from zicato.proposer.proposer import propose_experiment  # noqa: PLC0415
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
    board = workspace_loader.load_current_board(workspace_root)
    weights = workspace_loader.load_current_scoring(workspace_root)
    rubric = workspace_loader.load_current_rubric(workspace_root)

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
        from dataclasses import replace as _replace  # noqa: PLC0415

        config = _replace(config, instance_id=instance_id)

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

    # --- 3. Mutations ---
    mutations = enumerate_mutations(_resolve_mutable_trees(parent_gen.snapshot_root))
    if not mutations:
        raise RuntimeError(
            f"no mutation points enumerated under {parent_gen.snapshot_root}; "
            "did the adapter declare its mutable_trees?"
        )
    # --- 4. Patterns ---
    losses = _load_parent_losses(
        workspace_root, resolved_epoch_id, parent_id, board, read_loss_profile
    )
    events_paths = _build_events_paths(workspace_root, resolved_epoch_id, parent_id, board)
    detector_input = DetectorInput(
        losses=losses,
        entries={e.id: e for e in board},
        events_paths=events_paths,
    )
    patterns = detect_patterns(detector_input, detectors=ALL_DETECTORS)

    # --- 5. Loss summary ---
    loss_summary = _render_loss_summary(losses)

    # --- 6. Propose ---
    next_id = _next_generation_id(workspace_root, resolved_epoch_id)
    experiment = await propose_experiment(
        epoch_id=resolved_epoch_id,
        parent_generation_id=parent_id,
        new_generation_id=next_id,
        patterns=patterns,
        mutations=mutations,
        rubric_text=rubric.text,
        current_loss_summary=loss_summary,
        aux_call_llm=auxiliary_call_llm,
        model=str(workspace_config.get("auxiliary_model", "")),
        max_retries=max_proposer_retries,
        forbidden_ids=rubric.forbidden_ids,
        workspace_root=workspace_root,
    )

    # --- 7. Validate patch set against the manifest ---
    mutations_by_id = {m.id: m for m in mutations}
    for patch in experiment.patches:
        if patch.mutation_id not in mutations_by_id:
            raise RuntimeError(
                f"proposer-emitted patch {patch.id!r} targets unknown "
                f"mutation_id {patch.mutation_id!r}"
            )
    forbidden_violations = check_forbidden_ids(list(experiment.patches), list(rubric.forbidden_ids))
    if forbidden_violations:
        raise RuntimeError(
            "proposer-emitted patches violate forbidden_ids: " + "; ".join(forbidden_violations)
        )

    # --- 8. Apply patches into the child snapshot ---
    child_snapshot = _snapshot_root(workspace_root, resolved_epoch_id, next_id)
    if child_snapshot.exists():
        # Defensive: a previous failed round may have left a partial
        # snapshot. The applier refuses to overwrite, so we clear the
        # tree before re-running. Removing only the snapshot
        # subdirectory keeps any sibling debug data the operator may
        # have dropped under the generation dir.
        shutil.rmtree(child_snapshot)
    child_snapshot.parent.mkdir(parents=True, exist_ok=True)
    apply_patches(
        source_root=parent_gen.snapshot_root,
        patches=list(experiment.patches),
        target_root=child_snapshot,
    )

    # --- 9. Validate post-apply ---
    validation_errors = validate_post_apply(child_snapshot, list(experiment.patches), mutations)
    if validation_errors:
        # Persist the experiment with a rejected outcome describing
        # the validator findings, then abort.
        write_experiment(workspace_root, resolved_epoch_id, next_id, experiment)
        rejected_outcome = OutcomeRecord(
            ran_at=_now_iso(),
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=0.0,
            tournament_decision="rejected",
            rejection_reason="validation_failed: " + "; ".join(validation_errors),
        )
        finalised = update_experiment_outcome(
            workspace_root, resolved_epoch_id, next_id, rejected_outcome
        )
        append_journal_entry(workspace_root, resolved_epoch_id, finalised)
        return EvolveRoundOutcome(
            parent_generation_id=parent_id,
            proposed_generation_id=next_id,
            tournament_decision="rejected",
            rejection_reason=rejected_outcome.rejection_reason,
            parent_scalar=0.0,
            child_scalar=0.0,
            delta_scalar=0.0,
        )

    # --- 10. Run the tournament ---
    write_experiment(workspace_root, resolved_epoch_id, next_id, experiment)

    child_gen = Generation(
        id=next_id,
        epoch_id=resolved_epoch_id,
        parent_id=parent_id,
        snapshot_root=child_snapshot,
        created_at=_now_iso(),
    )
    if fast_mode:
        parent_historical = _load_historical_aggregate(workspace_root, resolved_epoch_id, parent_id)
        tournament_result = await run_fast_mode(
            adapter=adapter,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            parent_historical_agg=parent_historical,
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
        )

    # Cache gen_score.json for future fast-mode runs.
    _cache_gen_score(workspace_root, resolved_epoch_id, parent_id, tournament_result.parent_agg)
    _cache_gen_score(workspace_root, resolved_epoch_id, next_id, tournament_result.child_agg)

    # --- 11. Persist outcome ---
    decision = tournament_result.outcome.decision
    # "deferred" → treat as a non-promotion for evolve loop bookkeeping.
    bookkeeping_decision = "promoted" if decision == "promoted" else "rejected"
    parent_scalar = float(tournament_result.parent_agg.get("scalar", 0.0))
    child_scalar = float(tournament_result.child_agg.get("scalar", 0.0))
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
        rejection_reason=tournament_result.outcome.reason,
    )
    finalised = update_experiment_outcome(
        workspace_root, resolved_epoch_id, next_id, outcome_record
    )

    # --- 12. Lineage / current-generation marker on promotion ---
    if bookkeeping_decision == "promoted":
        promoted_gen = Generation(
            id=next_id,
            epoch_id=resolved_epoch_id,
            parent_id=parent_id,
            snapshot_root=child_snapshot,
            created_at=child_gen.created_at,
            promoted=True,
        )
        append_to_lineage(workspace_root, resolved_epoch_id, promoted_gen, parent_id=parent_id)
        _set_current_generation(workspace_root, resolved_epoch_id, next_id)
    else:
        # Still record the rejected generation in lineage so the
        # operator can see it in `zicato epoch list`.
        rejected_gen = Generation(
            id=next_id,
            epoch_id=resolved_epoch_id,
            parent_id=parent_id,
            snapshot_root=child_snapshot,
            created_at=child_gen.created_at,
            promoted=False,
        )
        append_to_lineage(workspace_root, resolved_epoch_id, rejected_gen, parent_id=parent_id)

    # --- 13. Journal ---
    append_journal_entry(workspace_root, resolved_epoch_id, finalised)

    # --- 14. Best-effort decision-telemetry analyzer ---
    # Analyser failure must never abort the round; the orchestrator
    # only logs at debug level and keeps going. The analyser writes
    # ``epochs/{epoch}/insights/round_{N}.md`` which the next round's
    # proposer (via :func:`zicato.analyzer.load_latest_insights`) reads.
    # The round number is derived from the newly-proposed generation
    # id (``v{N}``) so the insight file lines up with the lineage.
    try:
        from zicato.analyzer import analyze_epoch_telemetry  # noqa: PLC0415

        analyzer_round = _round_n_from_generation_id(next_id)
        await analyze_epoch_telemetry(
            workspace_root,
            resolved_epoch_id,
            auxiliary_call_llm,
            model=str(workspace_config.get("auxiliary_model", "")),
            round_n=analyzer_round,
        )
    except Exception as exc:  # noqa: BLE001 — analyser is best-effort
        log.debug("decision telemetry analyzer skipped: %s", exc)

    return EvolveRoundOutcome(
        parent_generation_id=parent_id,
        proposed_generation_id=next_id,
        tournament_decision=bookkeeping_decision,
        rejection_reason=outcome_record.rejection_reason,
        parent_scalar=parent_scalar,
        child_scalar=child_scalar,
        delta_scalar=child_scalar - parent_scalar,
    )


# ---------------------------------------------------------------------------
# evolve_n_rounds
# ---------------------------------------------------------------------------


async def evolve_n_rounds(
    *,
    rounds: int,
    workspace_root: Path,
    epoch_id: str | None = None,
    harness_call_llm: CallLLM,
    auxiliary_call_llm: CallLLM,
    instance_id: str = "default",
    fast_mode: bool = False,
    max_consecutive_rejections: int = 3,
    max_proposer_retries: int = 2,
    auto_epoch: bool = True,
    epoch_name: str | None = None,
) -> list[EvolveRoundOutcome]:
    """Loop :func:`evolve_once` up to ``rounds`` times.

    Stops early on ``max_consecutive_rejections`` rejected rounds in a
    row — that's a strong signal the proposer is stuck and the
    operator probably wants to inspect the rubric / patterns before
    spending more LLM calls. A successful promotion resets the
    consecutive-rejection counter.

    Contract-hash auto-epoching runs ONCE, before the round loop: when
    ``epoch_id`` is ``None`` and ``auto_epoch`` is true, the orchestrator
    resolves (and, if the contract drifted, auto-rolls) the epoch via
    :func:`ensure_epoch_for_contract`. The resolved id is then pinned
    for every round of this invocation so the loop never re-rolls
    mid-flight. When ``epoch_id`` is passed explicitly, auto-rolling is
    skipped entirely — an explicit target always wins.

    The list of :class:`EvolveRoundOutcome` returned has one entry per
    round attempted (which may be fewer than ``rounds`` if the
    early-stop fired).
    """
    if rounds <= 0:
        return []

    # Contract-hash auto-epoching — resolve the epoch ONCE up front.
    # An explicit --epoch wins and skips auto-rolling entirely.
    if epoch_id is None:
        epoch_id = await ensure_epoch_for_contract(
            workspace_root,
            auto_epoch=auto_epoch,
            aux_call_llm=auxiliary_call_llm,
            epoch_name=epoch_name,
        )
    if max_consecutive_rejections <= 0:
        # 0 / negative effectively disables early-stop — protect against
        # nonsense values by treating them as "never stop early".
        max_consecutive_rejections = rounds + 1

    # Workspace lock + heartbeat lifecycle. The lock keeps two concurrent
    # orchestrators from corrupting the same workspace; the beater writes
    # ``heartbeat.json`` so the supervisor binary can detect a wedge.
    lock = acquire_workspace_lock(workspace_root, instance_id)
    beater = HeartbeatBeater(workspace_root, instance_id, interval_s=2.0)
    outcomes: list[EvolveRoundOutcome] = []
    try:
        await beater.start()
        beater.update(epoch_id=epoch_id or "", phase="evolve_n_rounds:start")
        beater.bump_now()
        consecutive_rejections = 0
        for round_idx in range(rounds):
            beater.update(
                round_index=round_idx,
                round_started_at=_now_iso(),
                phase=f"evolve_once:round_{round_idx}",
            )
            beater.bump_now()
            outcome = await evolve_once(
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                harness_call_llm=harness_call_llm,
                auxiliary_call_llm=auxiliary_call_llm,
                instance_id=instance_id,
                fast_mode=fast_mode,
                max_proposer_retries=max_proposer_retries,
            )
            outcomes.append(outcome)
            beater.update(
                generation_id=outcome.proposed_generation_id,
                phase=f"after_round_{round_idx}:{outcome.tournament_decision}",
            )
            beater.bump_now()
            # Best-effort progressive analysis.html refresh so file://
            # readers (and the dashboard's static fallback) see the
            # latest lineage immediately after each round.
            try:
                from zicato.epoch.analysis import (  # noqa: PLC0415
                    regenerate_in_progress_html,
                )
                from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

                eid = epoch_id or current_epoch_id(workspace_root)
                if eid:
                    regenerate_in_progress_html(workspace_root, eid)
            except Exception as exc:  # noqa: BLE001 — HTML refresh is non-critical
                log.debug("progressive analysis.html refresh skipped: %s", exc)
            if outcome.tournament_decision == "promoted":
                consecutive_rejections = 0
            else:
                consecutive_rejections += 1
                if consecutive_rejections >= max_consecutive_rejections:
                    log.warning(
                        "evolve_n_rounds: stopping after %d consecutive rejections (round %d/%d)",
                        consecutive_rejections,
                        round_idx + 1,
                        rounds,
                    )
                    break
        beater.update(phase="evolve_n_rounds:done")
        beater.bump_now()
    finally:
        await beater.stop()
        release_workspace_lock(lock)
    return outcomes


# ---------------------------------------------------------------------------
# Small helpers — kept private so the public surface stays narrow.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


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
    return workspace_root / "epochs" / epoch_id / "current_generation"


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
    gens_root = workspace_root / "epochs" / epoch_id / "generations"
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


def _set_current_generation(workspace_root: Path, epoch_id: str, generation_id: str) -> None:
    marker = _current_generation_marker(workspace_root, epoch_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(generation_id + "\n", encoding="utf-8")


def _snapshot_root(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    return generation_dir(workspace_root, epoch_id, generation_id) / "snapshot"


def _next_generation_id(workspace_root: Path, epoch_id: str) -> str:
    """Pick a fresh ``vN`` id one above the highest existing."""
    gens_root = workspace_root / "epochs" / epoch_id / "generations"
    if not gens_root.exists():
        return "v0"
    max_n = -1
    for child in gens_root.iterdir():
        if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit():
            n = int(child.name[1:])
            if n > max_n:
                max_n = n
    return f"v{max_n + 1}"


def _resolve_mutable_trees(snapshot_root: Path) -> list[Path]:
    """Default to the whole snapshot when an adapter doesn't narrow it."""
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


def _cache_gen_score(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    aggregate: dict[str, Any],
) -> None:
    """Persist the generation aggregate so fast-mode can read it later."""
    gdir = generation_dir(workspace_root, epoch_id, generation_id)
    gdir.mkdir(parents=True, exist_ok=True)
    payload = dict(aggregate)
    payload.setdefault("generation_id", generation_id)
    (gdir / "gen_score.json").write_text(
        json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
    gens_root = workspace_root / "epochs" / epoch_id / "generations"
    if gens_root.exists() and any(p.is_dir() for p in gens_root.iterdir()):
        return  # already have at least one generation; nothing to do

    snapshot_root = _snapshot_root(workspace_root, epoch_id, "v0")

    # Priority 1 — cross-epoch lineage seed left by a contract-roll.
    seed_marker = _roll_seed_marker(workspace_root, epoch_id)
    seeded_from_roll = False
    if seed_marker.exists():
        seed_text = seed_marker.read_text(encoding="utf-8").strip()
        seed_source = Path(seed_text) if seed_text else None
        if seed_source is not None and seed_source.exists():
            snapshot_root.mkdir(parents=True, exist_ok=True)
            for child in sorted(seed_source.iterdir()):
                target = snapshot_root / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
            seeded_from_roll = True
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

        snapshot_root.mkdir(parents=True, exist_ok=True)
        for raw in raw_trees:
            source = Path(raw).resolve()
            if not source.exists():
                raise FileNotFoundError(
                    f"evolve_once: registered mutable tree {source} does not "
                    "exist on disk; baseline snapshot cannot be seeded."
                )
            if source.is_file():
                # Files are copied directly — rare in practice but cheap
                # to support so the helper does not impose tree-only
                # semantics.
                shutil.copy2(source, snapshot_root / source.name)
                continue
            target = snapshot_root / source.name
            shutil.copytree(source, target)

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


__all__ = [
    "EvolveRoundOutcome",
    "evolve_once",
    "evolve_n_rounds",
]
# ``experiment_json_path`` is referenced in the module docstring's
# "step 11" — kept imported here as a re-export hook for tests that
# want to assert on the persistence target without re-deriving it.
_ = experiment_json_path
