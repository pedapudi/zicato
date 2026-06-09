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
import datetime as _dt
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import (
    Experiment,
    Generation,
    OutcomeRecord,
    PriorExperiment,
)
from zicato.core.workspace import (
    experiment_json_path,
    generation_dir,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import acquire_workspace_lock, release_workspace_lock

if TYPE_CHECKING:
    # Annotation-only — the proposer module is imported lazily inside
    # ``evolve_once`` (see the module docstring on lazy imports), so its
    # exception type is referenced here purely for type annotations.
    from zicato.proposer.agent import ProposerAgent
    from zicato.proposer.proposer import ProposerError

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
    of the component names that differ (``board``, ``brief``,
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

    The "evaluation contract" is the board + proposer brief + scoring +
    the registered inner-harness identity (entrypoint + mutable trees).
    A change to any of those means generations on either side are no
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

    # The comprehensive report is regenerated every round, so the copy on disk
    # is frozen at the last mid-run pass — masthead still reading "in progress"
    # with pre-close counts. Now that the epoch is marked closed, re-stamp that
    # (deterministic) masthead so the persisted analysis.md/.html reflect the
    # final closed state. Cheap, no LLM; the narrative is left untouched.
    try:
        from zicato.analyzer import restamp_persisted_report  # noqa: PLC0415

        restamp_persisted_report(workspace_root, cur)
    except Exception as exc:  # noqa: BLE001 — best-effort, never block the roll
        log.debug("post-close report re-stamp skipped: %s", exc)

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
    # The auto-roll path has no operator interaction surface, so the
    # epoch's ``goal`` field lands empty. Nudge the operator to fill it
    # in later via the dedicated subcommand.
    log.warning(
        "epoch %s opened by auto-roll with no goal recorded; "
        'run `zicato epoch set-goal --epoch %s --goal "..."` to fill it in.',
        new_id,
        new_id,
    )
    print(
        f"NOTE: epoch {new_id} opened by auto-roll with no goal recorded; "
        f'run `zicato epoch set-goal --epoch {new_id} --goal "..."` to fill it in.'
    )
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
        brief_source=inputs.brief_path,
        weights=weights,
        auto_close_previous=False,  # ensure_epoch_for_contract closes explicitly
        aux_call_llm=aux_call_llm,
        entrypoint=inputs.entrypoint,
        mutable_trees=tuple(inputs.mutable_trees),
        proposer_path=inputs.proposer_path,
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
    beater: HeartbeatBeater | None = None,
    round_index: int = 0,
    total_rounds: int = 0,
    meta_loop_emitter: Any = None,
) -> EvolveRoundOutcome:
    """Run ONE evolve round against the current epoch.

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
    from zicato.epoch import (  # noqa: PLC0415
        append_journal_entry,
        append_to_lineage,
        load_epoch,  # noqa: PLC0415
        update_experiment_outcome,
        write_experiment,
    )
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
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
    from zicato.proposer.agent import (  # noqa: PLC0415
        ProposerContext,
        build_proposer_agent,
    )
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
        )

    # --- 6. Propose ---
    next_id = _next_generation_id(workspace_root, resolved_epoch_id)
    _beat(
        beater,
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
    # mounts; no second apply is needed.
    last_child_snapshot: dict[str, Path] = {}

    async def _validate_experiment_post_apply(candidate: Experiment) -> list[str]:
        _beat(
            beater,
            epoch_id=resolved_epoch_id,
            generation_id=next_id,
            round_index=round_index,
            phase=f"applying:round_{round_index}:{next_id}",
        )
        # derive_generation is the generation-level transaction boundary:
        # it copies the parent tree, applies the patch set all-or-nothing,
        # and clears any stale child tree from a prior attempt — so a
        # retry re-derives cleanly. See docs/design/STORAGE.md §4-§5.
        # apply_patches now runs its own post-apply syntax gate and raises
        # ValueError when a patch left a touched ``.py`` file unparseable;
        # surface that as a retryable post-apply finding rather than letting
        # it crash the evolve loop (issue #11).
        try:
            child = genstore.derive_generation(
                epoch_id=resolved_epoch_id,
                parent_generation_id=parent_id,
                child_generation_id=next_id,
                patches=list(candidate.patches),
            )
        except ValueError as exc:
            return [f"derive_generation rejected the patch set: {exc}"]
        last_child_snapshot["path"] = child
        return validate_post_apply(child, list(candidate.patches), mutations)

    # Experiment memory: the settled cross-round digest for this epoch.
    # Best-effort — a missing / stale index yields an empty list and the
    # proposer simply runs without the ``## What's already been tried``
    # section. The gauntlet field is a single challenger, so there are no
    # in-flight siblings to concatenate here.
    prior = _load_prior_experiments(workspace_root, resolved_epoch_id)

    proposer_validation_failed: ProposerError | None = None
    try:
        experiment = await proposer_agent.propose(
            ProposerContext(
                epoch_id=resolved_epoch_id,
                parent_generation_id=parent_id,
                new_generation_id=next_id,
                patterns=tuple(patterns),
                mutations=tuple(mutations),
                brief_text=brief.text,
                current_loss_summary=loss_summary,
                aux_call_llm=auxiliary_call_llm,
                model=str(workspace_config.get("auxiliary_model", "")),
                max_retries=max_proposer_retries,
                forbidden_ids=brief.forbidden_ids,
                workspace_root=workspace_root,
                validate_experiment=_validate_experiment_post_apply,
                meta_loop_emitter=meta_loop_emitter,
                custom_judge_names=custom_judge_names,
                prior_experiments=tuple(prior),
                restrict_visibility=weights.overfitting.restrict_proposer_visibility,
                failure_profile=failure_profile,
            )
        )
    except ProposerError as exc:
        # The proposer exhausted its bounded retries without producing a
        # patch set that survives post-apply validation (or parsing).
        # Fall through to the rejected-outcome path rather than crashing
        # the round — the round still produces a clean ``rejected``
        # journal entry, and the loop continues.
        proposer_validation_failed = exc
        experiment = None
    else:
        # Stamp the EVOLVE round that minted this generation onto the experiment
        # so it persists into experiment.json — the dashboard's round-timeline /
        # champion-spine attributes each generation to its birth round from this
        # stamp (the canonical value the loop already threads as round_index).
        # The guard is redundant at runtime (the `else` means propose succeeded,
        # so experiment is non-None) but narrows the union type for the checker.
        if experiment is not None:
            experiment = replace(experiment, round_index=round_index)

    # --- 7. Validate patch set against the manifest ---
    if experiment is not None:
        mutations_by_id = {m.id: m for m in mutations}
        for patch in experiment.patches:
            if patch.mutation_id not in mutations_by_id:
                raise RuntimeError(
                    f"proposer-emitted patch {patch.id!r} targets unknown "
                    f"mutation_id {patch.mutation_id!r}"
                )
        forbidden_violations = check_forbidden_ids(
            list(experiment.patches), list(brief.forbidden_ids)
        )
        if forbidden_violations:
            raise RuntimeError(
                "proposer-emitted patches violate forbidden_ids: " + "; ".join(forbidden_violations)
            )

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
        # Persist the experiment with a rejected outcome describing
        # the validator findings, then abort. Two distinct symbolic
        # reasons: ``validation_failed`` when a single applied patch set
        # failed post-apply validation; ``proposer_retries_exhausted``
        # when the proposer could not produce a patch set that survives
        # validation within its bounded retry budget.
        write_experiment(workspace_root, resolved_epoch_id, next_id, experiment)
        if proposer_validation_failed is not None:
            rejection_reason = "proposer_retries_exhausted: " + "; ".join(validation_errors)
        else:
            rejection_reason = "validation_failed: " + "; ".join(validation_errors)
        rejected_outcome = OutcomeRecord(
            ran_at=_now_iso(),
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=0.0,
            tournament_decision="rejected",
            rejection_reason=rejection_reason,
        )
        finalised = update_experiment_outcome(
            workspace_root, resolved_epoch_id, next_id, rejected_outcome
        )
        # Live index dual-write: experiment.json now carries the rejected
        # outcome, so fold it into the SQLite analytical index.
        _ingest_experiment_into_index(workspace_root, resolved_epoch_id, next_id)
        append_journal_entry(workspace_root, resolved_epoch_id, finalised)
        # Loop-health check for this round even on an early validator
        # rejection — a stuck loop should still surface on the dashboard.
        round_n = _round_n_from_generation_id(next_id) or round_index
        health_summary, health_critical = _assess_and_persist_loop_health(
            workspace_root, resolved_epoch_id, round_n, board
        )
        if health_critical:
            _warn_loop_no_signal(resolved_epoch_id, round_n, health_summary)
        # Regenerate the comprehensive epoch analysis report even on an
        # early validator rejection — the round still wrote an
        # experiment + journal entry, so the report should reflect it.
        await _regenerate_epoch_report(
            workspace_root,
            resolved_epoch_id,
            auxiliary_call_llm,
            str(workspace_config.get("auxiliary_model", "")),
        )
        _beat(
            beater,
            epoch_id=resolved_epoch_id,
            generation_id=next_id,
            round_index=round_index,
            phase=f"done:round_{round_index}:{next_id}:rejected",
        )
        return EvolveRoundOutcome(
            parent_generation_id=parent_id,
            proposed_generation_id=next_id,
            tournament_decision="rejected",
            rejection_reason=rejected_outcome.rejection_reason,
            parent_scalar=0.0,
            child_scalar=0.0,
            delta_scalar=0.0,
            health_summary=health_summary,
            health_critical=health_critical,
        )

    # --- 10. Run the tournament ---
    write_experiment(workspace_root, resolved_epoch_id, next_id, experiment)
    # Live index dual-write: the proposer-side experiment.json (outcome
    # still None) is on disk — fold it in so the index reflects the
    # in-progress generation before the tournament finishes.
    _ingest_experiment_into_index(workspace_root, resolved_epoch_id, next_id)
    _beat(
        beater,
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
    if fast_mode and parent_historical is not None:
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
            round_index=round_index,
            total_rounds=total_rounds,
        )

    # Cache gen_score.json for future fast-mode runs.
    _cache_gen_score(workspace_root, resolved_epoch_id, parent_id, tournament_result.parent_agg)
    _cache_gen_score(workspace_root, resolved_epoch_id, next_id, tournament_result.child_agg)

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

    # --- 11. Persist outcome ---
    decision = selection_decision.decision
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
        rejection_reason=selection_decision.reason,
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
    )
    finalised = update_experiment_outcome(
        workspace_root, resolved_epoch_id, next_id, outcome_record
    )
    # Live index dual-write: experiment.json now carries the tournament
    # outcome — refresh the SQLite analytical index entry for it.
    _ingest_experiment_into_index(workspace_root, resolved_epoch_id, next_id)

    # --- 12. Lineage / current-generation marker on promotion ---
    if bookkeeping_decision == "promoted":
        promoted_gen = Generation(
            id=next_id,
            epoch_id=resolved_epoch_id,
            parent_id=parent_id,
            snapshot_root=child_snapshot,
            created_at=child_gen.created_at,
            promoted=True,
            round_index=child_gen.round_index,
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
            round_index=child_gen.round_index,
        )
        append_to_lineage(workspace_root, resolved_epoch_id, rejected_gen, parent_id=parent_id)

    # --- 13. Journal ---
    append_journal_entry(workspace_root, resolved_epoch_id, finalised)

    # --- 14. Per-round loop-health check ---
    # Assess whether the loop is producing usable signal this round —
    # the epoch's accumulated losses + experiments + board, fed to
    # zicato.health. The LoopHealth report lands at
    # epochs/{epoch}/health/round_{N}.json; a CRITICAL finding (e.g.
    # degenerate scoring) escalates to a prominent stderr WARNING so the
    # operator sees "the loop is producing no signal." Best-effort: a
    # missing health sibling, or any assessment error, never aborts the
    # round (see _assess_and_persist_loop_health).
    round_n = _round_n_from_generation_id(next_id) or round_index
    health_summary, health_critical = _assess_and_persist_loop_health(
        workspace_root, resolved_epoch_id, round_n, board
    )
    if health_critical:
        _warn_loop_no_signal(resolved_epoch_id, round_n, health_summary)

    # --- 15. Best-effort decision-telemetry analyzer ---
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
            # Ground the insight prompt in the agent's REAL mutation
            # surface (enumerated above for this round) so the LLM's
            # "Suggested next mutations" section cannot hallucinate
            # mutation target ids that do not exist.
            mutation_ids=[m.id for m in mutations],
            meta_loop_emitter=meta_loop_emitter,
        )
    except Exception as exc:  # noqa: BLE001 — analyser is best-effort
        log.debug("decision telemetry analyzer skipped: %s", exc)

    # --- 16. Best-effort epoch analysis report regeneration ---
    await _regenerate_epoch_report(
        workspace_root,
        resolved_epoch_id,
        auxiliary_call_llm,
        str(workspace_config.get("auxiliary_model", "")),
    )

    _beat(
        beater,
        epoch_id=resolved_epoch_id,
        generation_id=next_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{next_id}:{bookkeeping_decision}",
    )

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
    on_status: Callable[[dict[str, Any]], None] | None = None,
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
    from zicato.mutation.validator import (  # noqa: PLC0415
        check_forbidden_ids,
        validate_post_apply,
    )
    from zicato.proposer.agent import ProposerContext  # noqa: PLC0415
    from zicato.proposer.proposer import ProposerError  # noqa: PLC0415

    genstore = default_generation_store(workspace_root)
    last_child_snapshot: dict[str, Path] = {}

    def _emit_status(record: dict[str, Any]) -> None:
        """Best-effort live publish of one challenger's proposal record."""
        if on_status is None:
            return
        try:
            on_status(record)
        except Exception as exc:  # noqa: BLE001 — live status is best-effort
            log.debug("proposal-status publish skipped: %s", exc)

    _beat(
        beater,
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

    async def _validate(candidate: Experiment) -> list[str]:
        _beat(
            beater,
            epoch_id=epoch_id,
            generation_id=next_id,
            round_index=round_index,
            phase=f"applying:round_{round_index}:{next_id}",
        )
        # apply_patches runs a post-apply syntax gate and raises ValueError
        # when a patch left a touched ``.py`` file unparseable; surface that
        # as a retryable post-apply finding rather than crashing the round
        # (issue #11).
        try:
            child = genstore.derive_generation(
                epoch_id=epoch_id,
                parent_generation_id=parent_id,
                child_generation_id=next_id,
                patches=list(candidate.patches),
            )
        except ValueError as exc:
            return [f"derive_generation rejected the patch set: {exc}"]
        last_child_snapshot["path"] = child
        return validate_post_apply(child, list(candidate.patches), mutations)

    try:
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
                validate_experiment=_validate,
                meta_loop_emitter=meta_loop_emitter,
                custom_judge_names=custom_judge_names,
                prior_experiments=prior_experiments,
                restrict_visibility=restrict_visibility,
                failure_profile=failure_profile,
            )
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

    mutations_by_id = {m.id: m for m in mutations}
    for patch in experiment.patches:
        if patch.mutation_id not in mutations_by_id:
            raise RuntimeError(
                f"proposer-emitted patch {patch.id!r} targets unknown "
                f"mutation_id {patch.mutation_id!r}"
            )
    forbidden_violations = check_forbidden_ids(list(experiment.patches), list(brief.forbidden_ids))
    if forbidden_violations:
        raise RuntimeError(
            "proposer-emitted patches violate forbidden_ids: " + "; ".join(forbidden_violations)
        )

    child_snapshot = last_child_snapshot["path"]
    # Stamp the EVOLVE round that minted this challenger onto the experiment
    # so it persists into experiment.json — mirroring the gauntlet path's
    # round_index stamp. Without it a multi-challenger field's experiments
    # carry the proposer's default round_index=0, so EVERY in-flight round's
    # challengers fold onto round 0's bracket (the round mis-attribution in
    # issue #16). The dashboard's round-grouping treats round_index as the
    # authoritative birth round, so this stamp is what makes a live round
    # group correctly by construction.
    experiment = replace(experiment, round_index=round_index)
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
    from zicato.selection import resolve_tournament  # noqa: PLC0415
    from zicato.selection.strategy import (  # noqa: PLC0415
        Contestant,
        Matchup,
        MatchupResult,
    )
    from zicato.tournament.runner import confirm_crowning_holdout, run_matchup  # noqa: PLC0415

    auxiliary_model = str(workspace_config.get("auxiliary_model", ""))
    field_n = strategy.field_size()

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
    prior = tuple(_load_prior_experiments(workspace_root, epoch_id))
    siblings: list[PriorExperiment] = []
    # The LIVE field-status map, keyed by generation_id, rewritten as each
    # challenger slot enters ("proposing"), retries, and settles
    # ("applied"/"rejected"). The orchestrator publishes a "proposing"-phase
    # ActiveTournament envelope on every status transition so the dashboard's
    # proposing tracker updates as each challenger is attempted — not only
    # after the whole field is minted. (OBSERVABILITY: the operator's "most
    # of what happens during proposal phase is opaque" pain.)
    live_status: dict[str, dict[str, Any]] = {}
    proposing_tournament_id = f"tourn_{epoch_id}_{base_id}"

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
            phase="proposing",
            entries=_field_entries(champion_only),
        )

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
            on_status=_publish_proposing,
        )
        field_status.append(status)
        if challenger is not None:
            applied.append(challenger)
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
            phase="proposing",
            entries=_field_entries(champion_only),
        )
        return EvolveRoundOutcome(
            parent_generation_id=parent_id,
            proposed_generation_id="",
            tournament_decision="rejected",
            rejection_reason="multi-challenger field: no challenger applied cleanly",
            parent_scalar=0.0,
            child_scalar=0.0,
            delta_scalar=0.0,
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

    # --- run_matchup: one duel via the board-unit runner + unchanged gate.
    async def _run_matchup(m: Matchup) -> MatchupResult:
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
        # gauntlet path's _cache_gen_score calls.
        _cache_gen_score(workspace_root, epoch_id, m.left.generation_id, result.parent_agg)
        _cache_gen_score(workspace_root, epoch_id, m.right.generation_id, result.child_agg)
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

    # --- Drive the strategy to a crowned decision.
    try:
        decision = await resolve_tournament(
            strategy,
            request_field=_request_field,
            run_matchup=_run_matchup,
            on_progress=_publish_live_structure,
        )
    except Exception:
        # A failure mid-resolution leaves no settled bracket — clear the
        # live "running" envelope so the dashboard does not show a stuck
        # tournament, then re-raise.
        _clear_active_tournament(workspace_root)
        raise
    # Settle the live envelope with the resolved rounds + standings so the
    # dashboard's structure reader sees the final bracket. Unlike the
    # gauntlet path (which clears its transient running record on exit),
    # the multi-challenger envelope is RETAINED with phase="completed":
    # competitors/rounds/standings are the dashboard's only live source for
    # a non-gauntlet field until the next round's tournament starts.
    _settle_active_tournament(
        workspace_root,
        tournament_id=tournament_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        strategy=strategy,
        decision=decision,
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
    _persist_field_tournament(
        workspace_root,
        field_tournament_id=f"{epoch_id}:field:{first_challenger_id}",
        first_challenger_id=first_challenger_id,
        epoch_id=epoch_id,
        structure=tournament_spec.structure,
        structure_params=dict(tournament_spec.params),
        competitors=competitors_meta,
        rounds=_serialise_rounds(strategy.rounds()),
        standings=_serialise_standings(decision.standings),
        field_status=field_status or [],
        decision=decision,
        state="settled",
    )

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
    promoted_id = decision.promoted_generation_id
    crowning_reason_override: str | None = None
    crowning_holdout_block: dict[str, Any] | None = None
    crowning_holdout_child_scalar: float | None = None
    crowning_challenger_id: str | None = None
    crowning_challenger_train_scalar: float | None = None
    crowning_result = (
        next(
            (m for m in decision.matchups if m.matchup_id == decision.crowning_matchup_id),
            None,
        )
        if decision.crowning_matchup_id
        else None
    )
    if crowning_result is not None and decision.decision == "promoted" and promoted_id is not None:
        # Identify the champion (parent) and challenger (survivor) sides of
        # the crowning duel; ``left`` is the champion by the strategy's
        # convention, but resolve defensively so a future strategy that
        # seeds the champion on the right still confirms the right pair.
        champ_is_left = crowning_result.left_id == parent_id
        challenger_crown_id = crowning_result.right_id if champ_is_left else crowning_result.left_id
        crowning_challenger_id = challenger_crown_id
        champ_train_agg = crowning_result.left_agg if champ_is_left else crowning_result.right_agg
        challenger_train_agg = (
            crowning_result.right_agg if champ_is_left else crowning_result.left_agg
        )
        # The crowning challenger's TRAIN-slice scalar — paired with its
        # holdout-slice scalar (below) for a consistent generalization gap
        # measured on the SAME crowning duel (mirrors the gauntlet, where the
        # gap is the child's train vs holdout scalar of the one duel).
        crowning_challenger_train_scalar = float(challenger_train_agg.get("scalar", 0.0))
        (
            crowning_outcome,
            crowning_holdout_block,
            crowning_holdout_child_scalar,
        ) = await confirm_crowning_holdout(
            adapter=adapter,
            champion_gen=champion_gen,
            challenger_gen=_generation_for(challenger_crown_id),
            board=board,
            train_outcome=crowning_result.outcome,
            train_parent_agg=champ_train_agg,
            train_child_agg=challenger_train_agg,
            weights=weights,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            disable_drift=disable_drift,
            judge_only=judge_only,
            fast=fast_mode,
        )
        if crowning_outcome.decision != "promoted":
            # The holdout flipped a bracket-leader's train win to a reject:
            # the champion stands, the crowned generation is demoted to a
            # dead branch, and the crowning reason carries the holdout cause.
            promoted_id = None
            crowning_reason_override = crowning_outcome.reason

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
        is_crowned = gid == promoted_id
        gen_decision = "promoted" if is_crowned else "rejected"
        agg = _first_aggregate_for(gid, decision)
        gen_scalar = float(agg.get("scalar", 0.0)) if agg else 0.0
        if is_crowned:
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
        outcome_record = OutcomeRecord(
            ran_at=_now_iso(),
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=gen_scalar - parent_scalar,
            tournament_decision=gen_decision,  # type: ignore[arg-type]
            rejection_reason=rejection_reason,
            structure=tournament_spec.structure,
            final_rank=rank_by_id.get(gid),
            match_record=tuple(matches_by_gen.get(gid, ())),
            champion_eval_mode=resolved_champion_eval_mode,
            holdout=holdout_block,
            train_loss=gen_fields["train_loss"],
            holdout_loss=gen_fields["holdout_loss"],
            generalization_gap=gen_fields["generalization_gap"],
        )
        finalised = update_experiment_outcome(workspace_root, epoch_id, gid, outcome_record)
        finalised_by_id[gid] = finalised
        _ingest_experiment_into_index(workspace_root, epoch_id, gid)

    # --- Lineage: the crowned generation on the spine (promoted), every
    # other challenger recorded as a dead branch (rejected child of the
    # champion). current_generation advances only on a promotion.
    for challenger in applied:
        gid = challenger.generation_id
        is_crowned = gid == promoted_id
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

    # --- Journal: one entry per challenger (crowned + dead branches).
    for challenger in applied:
        append_journal_entry(workspace_root, epoch_id, finalised_by_id[challenger.generation_id])

    # --- Loop-health + analyzer + report (mirrors the gauntlet path).
    round_n = _round_n_from_generation_id(applied[0].generation_id) or round_index
    health_summary, health_critical = _assess_and_persist_loop_health(
        workspace_root, epoch_id, round_n, board
    )
    if health_critical:
        _warn_loop_no_signal(epoch_id, round_n, health_summary)

    try:
        from zicato.analyzer import analyze_epoch_telemetry  # noqa: PLC0415

        await analyze_epoch_telemetry(
            workspace_root,
            epoch_id,
            auxiliary_call_llm,
            model=auxiliary_model,
            round_n=_round_n_from_generation_id(applied[0].generation_id),
            mutation_ids=[m.id for m in mutations],
            meta_loop_emitter=meta_loop_emitter,
        )
    except Exception as exc:  # noqa: BLE001 — analyser is best-effort
        log.debug("decision telemetry analyzer skipped: %s", exc)

    await _regenerate_epoch_report(workspace_root, epoch_id, auxiliary_call_llm, auxiliary_model)

    bookkeeping_decision = "promoted" if promoted_id is not None else "rejected"
    _beat(
        beater,
        epoch_id=epoch_id,
        generation_id=promoted_id or applied[0].generation_id,
        round_index=round_index,
        phase=f"done:round_{round_index}:{tournament_id}:{bookkeeping_decision}",
    )

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


def _resolve_round_champion_mode(
    champion_cached_units: int,
    champion_fresh_units: int,
    *,
    fast_requested: bool,
) -> str:
    """Collapse the CHAMPION's cached-vs-fresh tally into a round-level mode.

    With the cache-first board-unit runner, the champion's reuse is
    measured directly: ``champion_cached_units`` board units were reused
    from the cache and ``champion_fresh_units`` were executed live across
    every matchup the champion appeared in this round. The round-level
    provenance is:

    * ``"full"`` when fast was not requested (or the champion never
      played a board unit this round);
    * ``"fast-degraded"`` when fast was requested but the champion had to
      run at least one board unit live for lack of a cache (the seed/first
      champion, or a not-yet-covered racing subset);
    * ``"fast"`` when fast was requested and EVERY champion board unit was
      reused from the cache (no fresh champion run this round).

    A RUNTIME provenance value only — it is recorded in the journal and
    never folds into the contract hash.
    """
    if not fast_requested:
        return "full"
    if champion_fresh_units > 0:
        return "fast-degraded"
    if champion_cached_units > 0:
        return "fast"
    # Fast requested but the champion played no board unit this round.
    return "full"


def _first_aggregate_for(gid: str, decision: Any) -> dict[str, Any] | None:
    """Find a generation's aggregate dict from the decision's matchups.

    A generation may appear as ``left`` or ``right`` across several
    matchups (a Swiss / double-elim run); any one carries its aggregate,
    so the first occurrence suffices for the scalar the journal records.
    """
    for mr in decision.matchups:
        if mr.left_id == gid:
            return dict(mr.left_agg)
        if mr.right_id == gid:
            return dict(mr.right_agg)
    return None


def _publish_active_tournament(
    workspace_root: Path,
    *,
    tournament_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    round_index: int,
    total_rounds: int,
    field_status: list[dict[str, Any]] | None = None,
    phase: str = "running",
    rounds: list[dict[str, Any]] | None = None,
    standings: list[dict[str, Any]] | None = None,
    entries: list[Any] | None = None,
) -> None:
    """Best-effort: publish the live ActiveTournament envelope.

    Populates the structure envelope (``structure`` / ``structure_params``
    / ``competitors``) per the data-model doc so the dashboard can render
    a non-gauntlet field while it runs. ``parent_generation_id`` /
    ``child_generation_id`` are left empty for non-gauntlet structures
    (the data model's documented convention); ``competitors`` is the
    authoritative field.

    ``rounds`` / ``standings`` carry the live (in-flight) structure when
    supplied — the settled rounds PLUS the current scheduled round (its
    matches with ``winner: null`` + ``pending: true``) and the
    standings-so-far, both serialised through the SAME
    :func:`_serialise_rounds` / :func:`_serialise_standings` path the
    settle + durable-record producers use, so the dashboard's
    bracket/ladder/funnel renderers work identically live and post-run.
    Omitting them (e.g. the all-rejected publish, which has no field to
    seed) leaves the envelope's ``rounds`` / ``standings`` empty.

    ``entries`` is the per-entry funnel field
    (:class:`~zicato.runtime.state.ActiveTournamentEntry`). The gauntlet
    A/B path seeds it directly; the multi-challenger / racing path builds
    it via :func:`_field_entries` so the dashboard's per-entry funnel rung
    is non-empty live, not just after the run settles. When omitted (e.g.
    the all-rejected publish) it falls back to an empty list, matching the
    pre-existing behaviour.

    Never raises — a live-state write failure must not abort the round.
    """
    try:
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            read_active_tournament,
            write_active_tournament,
        )

        # PRESERVE the runner-written live fields across a republish. The
        # runner rewrites ``projected`` / ``partial_*_agg`` per settled board
        # via its OWN read-modify-write; this full envelope republish (one per
        # scheduled batch) would otherwise clobber them back to empty, killing
        # the live projected standing. Carry them forward from the on-disk
        # record so the two writers compose instead of racing to zero.
        prior = read_active_tournament(workspace_root)
        projected = dict(prior.projected) if prior is not None else {}
        partial_champion = dict(prior.partial_champion_agg) if prior is not None else {}
        partial_challenger = dict(prior.partial_challenger_agg) if prior is not None else {}

        write_active_tournament(
            workspace_root,
            ActiveTournament(
                tournament_id=tournament_id,
                parent_generation_id="",
                child_generation_id="",
                epoch_id=epoch_id,
                started_at=_now_iso(),
                phase=phase,
                round_index=round_index,
                total_rounds=total_rounds,
                structure=structure,
                structure_params=dict(structure_params),
                competitors=[dict(c) for c in competitors],
                field_status=[dict(f) for f in (field_status or [])],
                rounds=[dict(r) for r in (rounds or [])],
                standings=[dict(s) for s in (standings or [])],
                entries=list(entries or []),
                projected=projected,
                partial_champion_agg=partial_champion,
                partial_challenger_agg=partial_challenger,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament publish skipped: %s", exc)


def _field_entries(
    competitors: list[dict[str, Any]],
    standings: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Build the per-competitor funnel ``entries`` for a non-gauntlet field.

    The gauntlet A/B path seeds :attr:`ActiveTournament.entries` with one
    :class:`~zicato.runtime.state.ActiveTournamentEntry` per board entry
    per side; the dashboard funnel groups by ``side`` to draw rungs. The
    racing / multi-challenger live-publish path historically left
    ``entries`` empty, so the live funnel could not render the per-entry
    rung field for a field tournament until the run settled. This helper
    closes that gap by emitting one row per competitor, mirroring the A/B
    shape: ``side`` carries the competitor's role (``champion`` /
    ``challenger``) — the data-model §2.3 convention for non-gauntlet
    structures — and ``entry_id`` carries its generation id.

    When live ``standings`` are supplied each row's ``status`` and
    ``loss_summary`` are derived from the matching standing (status maps
    alive/competing → ``running``, eliminated/champion → ``completed``; the
    Copeland ``scalar`` rides in ``loss_summary`` so the funnel can colour
    a rung by relative loss). Absent standings (the pre-schedule publish)
    every row is ``queued`` with an empty loss summary.
    """
    from zicato.runtime.state import ActiveTournamentEntry  # noqa: PLC0415

    by_gen: dict[str, dict[str, Any]] = {}
    for s in standings or []:
        gid = str(s.get("generation_id", ""))
        if gid:
            by_gen[gid] = s

    entries: list[Any] = []
    for c in competitors:
        gid = str(c.get("generation_id", ""))
        role = str(c.get("role", "") or "")
        standing = by_gen.get(gid)
        if standing is None:
            status = "queued"
            loss_summary: dict[str, float] = {}
        else:
            raw_status = str(standing.get("status", "") or "")
            status = "completed" if raw_status in ("eliminated", "champion") else "running"
            scalar = standing.get("scalar")
            loss_summary = {"scalar": float(scalar)} if isinstance(scalar, int | float) else {}
            # An IN-FLIGHT competitor carries a live PROJECTED scalar (the
            # runner's running aggregate over boards-so-far). Surface it +
            # the boards progress on the funnel entry so the rung can render
            # the "projected" treatment (dashed, ~prefix, scored sub-bar)
            # before a settled scalar lands.
            if standing.get("in_flight"):
                proj = standing.get("projected_scalar")
                if isinstance(proj, int | float):
                    loss_summary["projected_scalar"] = float(proj)
                loss_summary["in_flight"] = 1.0
                bd = standing.get("boards_done")
                bt = standing.get("boards_total")
                if isinstance(bd, int | float):
                    loss_summary["boards_done"] = float(bd)
                if isinstance(bt, int | float):
                    loss_summary["boards_total"] = float(bt)
        entries.append(
            ActiveTournamentEntry(
                entry_id=gid,
                # Non-gauntlet structures carry the competitor role as the
                # ``side`` key (data-model §2.3); the funnel groups on it.
                side=role or gid,
                status=status,
                loss_summary=loss_summary,
            )
        )
    return entries


def _serialise_rounds(rounds: Any) -> list[dict[str, Any]]:
    """Project a sequence of :class:`RoundRecord` to the dashboard shape.

    The single canonical serialisation of the round-by-round pairings
    (data-model §2.4), shared by ALL THREE producers so they carry
    byte-identical shapes and the dashboard renderers work identically:

    * the LIVE ``active_tournament`` envelope (``strategy.live_rounds()``
      — settled rounds plus the in-flight round whose matches carry
      ``winner: null`` + ``pending: true``);
    * the SETTLED envelope at run end (``strategy.rounds()``);
    * the DURABLE field-tournament snapshot (``strategy.rounds()``).

    ``winner`` is emitted as ``None`` (the contract's ``winner: null``)
    for a match that has not crowned a side — every ``pending`` match, and
    any settled match the strategy left with an empty ``winner`` (a racing
    rung, which cuts rather than crowns).
    """
    return [
        {
            "stage_index": r.stage_index,
            "label": r.label,
            "matches": [
                {
                    "match_id": m.match_id,
                    "competitors": list(m.competitors),
                    "winner": (m.winner or None),
                    "decision": m.decision,
                    "delta_scalar": m.delta_scalar,
                    "bracket_slot": m.bracket_slot,
                    "bye": m.bye,
                    "survivors": list(m.survivors),
                    "cut": list(m.cut),
                    "board_fraction": m.board_fraction,
                    "pending": m.pending,
                    "live_progress": {
                        str(gid): dict(lane) for gid, lane in (m.live_progress or {}).items()
                    },
                }
                for m in r.matches
            ],
        }
        for r in rounds
    ]


def _overlay_projected_live_progress(
    rounds: list[dict[str, Any]],
    workspace_root: Path,
) -> None:
    """Fold the runner's per-board ``projected`` into the rung ``live_progress``.

    The racing strategy publishes the in-flight rung's per-lane
    ``live_progress`` TOPOLOGY (which lanes are racing, each lane's
    ``boards_total`` = the rung's board-slice size, the ``inflight`` flag,
    and the lane's last-known running scalar vs the champion). The runner's
    :class:`_IncrementalScorer` writes the live per-board ``projected`` map
    (``{generation_id: {scalar, boards_done, boards_total, pass_rate}}``)
    onto :attr:`ActiveTournament.projected` as each board unit settles. This
    overlays the latter onto the former IN PLACE so each rung lane carries
    one authoritative progress row the dashboard consumes directly:

    * ``boards_done`` — from the runner's projected row (the strategy can't
      know mid-duel board progress).
    * ``projected_scalar`` / ``projected`` — refreshed from the runner's
      LIVE running aggregate when present (more current than the strategy's
      last-rung scalar). The strategy's seeded scalar (a prior rung's
      result) stays as the fallback when no live projected row exists yet.
    * ``boards_total`` — kept from the strategy's authoritative rung-slice
      size; only filled from the projected row when the strategy left it
      unknown (whole-board fallback construction).

    The separation of concerns is preserved: the STRATEGY owns the
    ``live_progress`` topology (it is written into the serialised rounds),
    the SCORER owns the ``projected`` scalars (read here from the on-disk
    state). Best-effort — a missing / unreadable projected map leaves the
    strategy-published ``live_progress`` untouched. Mutates ``rounds`` in
    place; a no-op for any round whose matches carry no ``live_progress``
    (every non-racing structure, and a racing rung before it is scheduled).
    """
    has_progress = any(m.get("live_progress") for r in rounds for m in (r.get("matches") or []))
    if not has_progress:
        return
    try:
        from zicato.runtime.state import read_active_tournament  # noqa: PLC0415

        active = read_active_tournament(workspace_root)
    except Exception:  # noqa: BLE001 — overlay is best-effort
        active = None
    projected = dict(active.projected) if active is not None else {}
    if not projected:
        return
    for r in rounds:
        for m in r.get("matches") or []:
            lanes = m.get("live_progress")
            if not lanes:
                continue
            for gid, lane in lanes.items():
                proj = projected.get(str(gid))
                if not isinstance(proj, dict):
                    continue
                if "boards_done" in proj:
                    lane["boards_done"] = int(proj["boards_done"])
                if "boards_total" in proj and "boards_total" not in lane:
                    lane["boards_total"] = int(proj["boards_total"])
                if "scalar" in proj:
                    lane["projected_scalar"] = float(proj["scalar"])
                    lane["projected"] = True


def _serialise_standings(standings: Any) -> list[dict[str, Any]]:
    """Project a sequence of :class:`Standing` to the dashboard shape.

    The single canonical serialisation of the Copeland standings
    (data-model §2.5), shared by the live envelope
    (``strategy.live_standings()``), the settled envelope, and the durable
    field-tournament snapshot (both ``decision.standings``). The live
    ``status`` is emitted raw (alive / eliminated / champion / competing);
    the dashboard maps display labels.
    """
    return [
        {
            "generation_id": s.generation_id,
            "rank": s.rank,
            "scalar": s.scalar,
            "wins": s.wins,
            "losses": s.losses,
            "status": s.status,
            "role": s.role,
        }
        for s in standings
    ]


def _overlay_projected_standings(
    standings: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    workspace_root: Path,
    structure: str,
) -> list[dict[str, Any]]:
    """Fold the runner's live PROJECTED standing onto the standings rows.

    Reads :attr:`ActiveTournament.projected` (the runner's per-board
    ``{generation_id: {scalar, boards_done, boards_total, pass_rate}}`` map)
    and overlays it onto the matching standing rows for the competitors that
    are IN FLIGHT — i.e. those appearing in a still-pending match of the
    current live round. Each touched row gains ``projected_scalar``,
    ``in_flight=True``, ``boards_done`` and ``boards_total``; settled rows
    are left untouched (no ``in_flight`` key, original scalar).

    Per-structure ranking rule (substitute the projected scalar into the
    EXISTING sort key for the in-flight competitor ONLY):

    * ``single_elim`` / ``double_elim`` / ``racing`` — scalar rank. The
      projected scalar replaces the row's (still-zero) scalar in the sort
      key, so an in-flight leader bubbles up live; settled rows keep their
      real scalar.
    * ``swiss`` — Copeland points are NEVER projected (a half-finished duel
      has no win). The points-based rank is preserved exactly; the projected
      scalar only nudges the MEAN-SCALAR TIEBREAK among rows on equal points,
      and the pairing is marked in-flight visually. Never re-ranks on points.
    * ``gauntlet`` — not routed here (no multi-competitor standings).

    Best-effort: a missing / unreadable projected map yields the input
    standings unchanged.
    """
    if not standings:
        return standings
    try:
        from zicato.runtime.state import read_active_tournament  # noqa: PLC0415

        active = read_active_tournament(workspace_root)
    except Exception:  # noqa: BLE001 — overlay is best-effort
        active = None
    projected = dict(active.projected) if active is not None else {}
    if not projected:
        return standings

    # The IN-FLIGHT competitor set: every generation in a pending (unresolved)
    # match of the live rounds. A row is only projected when its competitor is
    # actually running right now — a stale projected row for a competitor whose
    # match already settled must not override its real scalar.
    in_flight: set[str] = set()
    for r in rounds:
        for m in r.get("matches", []) or []:
            if not m.get("pending"):
                continue
            for g in m.get("competitors", []) or []:
                if g:
                    in_flight.add(str(g))

    out: list[dict[str, Any]] = []
    for s in standings:
        row = dict(s)
        gid = str(row.get("generation_id", ""))
        proj = projected.get(gid)
        if gid and gid in in_flight and isinstance(proj, dict) and "scalar" in proj:
            row["in_flight"] = True
            row["projected_scalar"] = float(proj["scalar"])
            if "boards_done" in proj:
                row["boards_done"] = int(proj["boards_done"])
            if "boards_total" in proj:
                row["boards_total"] = int(proj["boards_total"])
        out.append(row)

    def _scalar_key(row: dict[str, Any]) -> float:
        # In-flight rows sort on the projected scalar (lower is better);
        # settled rows keep their real scalar.
        if row.get("in_flight") and isinstance(row.get("projected_scalar"), int | float):
            return float(row["projected_scalar"])
        sc = row.get("scalar")
        return float(sc) if isinstance(sc, int | float) else float("inf")

    if structure in ("single_elim", "double_elim", "racing"):
        out.sort(key=_scalar_key)
        for i, row in enumerate(out, start=1):
            row["rank"] = i
    elif structure == "swiss":
        # Points-rank is authoritative; the projected scalar only breaks ties
        # among rows on EQUAL wins (the mean-scalar tiebreak). Never re-rank on
        # points — a half-finished duel has crowned no winner.
        def _swiss_key(row: dict[str, Any]) -> tuple[int, float]:
            wins = row.get("wins")
            w = int(wins) if isinstance(wins, int) else 0
            return (-w, _scalar_key(row))

        out.sort(key=_swiss_key)
        for i, row in enumerate(out, start=1):
            row["rank"] = i
    # Any other structure: overlay the markers but leave the published order.
    return out


def _persist_field_tournament(
    workspace_root: Path,
    *,
    field_tournament_id: str,
    first_challenger_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    standings: list[dict[str, Any]],
    field_status: list[dict[str, Any]],
    decision: Any,
    state: str = "settled",
) -> None:
    """Best-effort: durably persist a FIELD-level structure record.

    Writes the round's field record — round pairings, Copeland
    standings, competitors, proposing field-status, the crowning verdict —
    to its durable ``tournaments/field-*.json`` snapshot AND dual-writes it
    into the analytical index as ONE field-level ``tournaments`` row. The
    snapshot is the canonical source (so ``zicato reindex`` re-derives the
    row); the dual-write puts the swiss / elim ladder in the index.

    ``state`` carries the explicit ``in_progress`` → ``settled`` lifecycle
    (issue #16): the orchestrator OPENS the envelope at round start
    (``state="in_progress"``, ``decision=None``, empty rounds/standings —
    just the competitor field + proposing status so the round is visible to
    every queryable store mid-flight) and FINALISES it at settle
    (``state="settled"`` with the resolved bracket + crowning verdict). The
    record is keyed on the field-level ``tournament_id`` so the settle write
    upserts the same row the open write created — idempotent, crash-safe,
    and safe to re-open on resume (the in_progress record is simply
    overwritten by the next open or the settle).

    A no-op for a degenerate two-competitor (gauntlet) field — the
    per-challenger row already covers it. Never raises: a durable-state
    write failure must not abort the round.
    """
    if len(competitors) < 3:
        return
    crowning_delta: float | None = None
    for r in reversed(rounds):
        matches = r.get("matches") or []
        if matches:
            crowning_delta = matches[-1].get("delta_scalar")
            break
    champion_id = next(
        (c.get("generation_id") for c in competitors if str(c.get("role", "")) == "champion"),
        "",
    )
    # ``decision`` is None while the round is still in flight (the envelope
    # is opened before the bracket resolves); the crowning fields stay empty
    # until settle. getattr tolerates the None case alongside the settled
    # TournamentDecision so the open + settle writes share one code path.
    record: dict[str, Any] = {
        "tournament_id": field_tournament_id,
        "epoch_id": epoch_id,
        "structure": structure,
        "structure_params": dict(structure_params),
        "competitors": [dict(c) for c in competitors],
        "rounds": rounds,
        "standings": standings,
        "field_status": [dict(f) for f in field_status],
        "promoted_generation_id": getattr(decision, "promoted_generation_id", "") or "",
        "champion_generation_id": champion_id or "",
        "decision": getattr(decision, "decision", "") or "",
        "reason": getattr(decision, "reason", "") or "",
        "delta_scalar": crowning_delta,
        "state": state,
        "ran_at": _now_iso(),
    }
    try:
        from zicato.core.workspace import field_tournament_path  # noqa: PLC0415

        path = field_tournament_path(workspace_root, epoch_id, first_challenger_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — durable snapshot is best-effort
        log.debug("field-tournament snapshot skipped: %s", exc)
        return
    try:
        from zicato.index.ingest import ingest_field_tournament  # noqa: PLC0415

        ingest_field_tournament(workspace_root, _index_db_path(workspace_root), record)
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping field-tournament dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug("field-tournament index dual-write skipped: %s", exc)


def _settle_active_tournament(
    workspace_root: Path,
    *,
    tournament_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    strategy: Any,
    decision: Any,
    round_index: int,
    total_rounds: int,
    field_status: list[dict[str, Any]] | None = None,
) -> None:
    """Best-effort: rewrite the live envelope with the settled bracket.

    Serializes the strategy's ``rounds()`` (data-model §2.4) and the
    decision's ``standings`` (§2.5) so the dashboard's structure reader
    sees the final bracket / leaderboard. Never raises.
    """
    try:
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            write_active_tournament,
        )

        rounds = _serialise_rounds(strategy.rounds())
        standings = _serialise_standings(decision.standings)
        write_active_tournament(
            workspace_root,
            ActiveTournament(
                tournament_id=tournament_id,
                parent_generation_id="",
                child_generation_id=decision.promoted_generation_id or "",
                epoch_id=epoch_id,
                started_at=_now_iso(),
                phase="completed",
                round_index=round_index,
                total_rounds=total_rounds,
                structure=structure,
                structure_params=dict(structure_params),
                competitors=[dict(c) for c in competitors],
                rounds=rounds,
                standings=standings,
                field_status=[dict(f) for f in (field_status or [])],
                # Seed the per-competitor funnel field from the SETTLED
                # standings so the retained completed envelope (the
                # dashboard's only live source for a non-gauntlet field
                # until the next round) carries the per-entry rung, not an
                # empty list. Mirrors the live-publish path.
                entries=_field_entries(competitors, standings),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament settle skipped: %s", exc)


def _clear_active_tournament(workspace_root: Path) -> None:
    """Best-effort: clear the live ActiveTournament record. Never raises."""
    try:
        from zicato.runtime.state import clear_active_tournament  # noqa: PLC0415

        clear_active_tournament(workspace_root)
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament clear skipped: %s", exc)


def _mark_run_terminal(workspace_root: Path) -> None:
    """Best-effort: mark a cleanly-ended run terminal so it never reads LIVE.

    A normally-ended evolve loop already stamps a terminal heartbeat phase
    (``evolve_n_rounds:done``), which the dashboard treats as idle. But the
    runtime ``active_tournament.json`` envelope can linger with
    ``phase="running"`` (e.g. a mid-resolution structure whose settle write
    never ran) — and a frontend reading the heartbeat as fresh would then
    show a "LIVE" tournament on a closed epoch. As a defensive measure on
    clean shutdown we flip any lingering ``phase="running"`` envelope to a
    terminal ``"stopped"`` phase, so a normally-ended run does not read as
    live even before the heartbeat freshness window elapses. A SIGKILLed run
    cannot self-clean — that case is covered by the frontend freshness gate.
    Never raises: a teardown-time live-state write failure must not mask the
    real shutdown reason.
    """
    try:
        from zicato.runtime.state import (  # noqa: PLC0415
            read_active_tournament,
            write_active_tournament,
        )

        current = read_active_tournament(workspace_root)
        if current is None:
            return
        if str(current.phase).strip().lower() == "running":
            write_active_tournament(workspace_root, replace(current, phase="stopped"))
    except Exception as exc:  # noqa: BLE001 — terminal-state write is best-effort
        log.debug("terminal active-tournament mark skipped: %s", exc)


# ---------------------------------------------------------------------------
# evolve_n_rounds
# ---------------------------------------------------------------------------


#: Default threshold for the loop-health circuit breaker: this many
#: consecutive rounds with a CRITICAL loop-health finding stops the
#: evolve loop early. Two is deliberately tight — one CRITICAL round
#: could be a transient (e.g. a single degenerate tournament), but two
#: in a row means the loop is genuinely producing no signal.
_DEGENERATE_HEALTH_STOP_THRESHOLD = 2


def _gauntlet_decision_from_result(
    tournament_spec: Any,
    parent_id: str,
    child_id: str,
    child_snapshot: Path,
    tournament_result: Any,
) -> Any:
    """Drive a gauntlet strategy from an already-run single duel.

    The gauntlet structure is the back-compat baseline: one champion, one
    challenger, one full-board duel, promote-on-gate. ``evolve_once``
    already ran that single duel (full or fast mode) and holds its
    :class:`~zicato.tournament.runner.TournamentResult`, whose ``outcome``
    is the unchanged :func:`~zicato.tournament.gate.evaluate_gate` verdict.
    We feed that verdict into a fresh :class:`GauntletStrategy` so the
    *decision* (and its audit / standings) flows through the
    :class:`SelectionStrategy` abstraction without re-running the gate or
    altering behaviour — the strategy reads ``outcome.decision`` exactly
    as the historical inline branch did.

    Returns a :class:`~zicato.selection.strategy.SelectionDecision`.

    Non-gauntlet structures are dispatched by the registry; for the v1
    wave the orchestrator runs the gauntlet's single-duel path (the field
    size for any structure with ``field_size == 1`` degrades to the
    gauntlet, per the registry's documented degeneracy). The full
    multi-challenger field is driven by
    :func:`zicato.selection.resolve_tournament` + ``run_matchup`` — wired
    here as the strategy is fed the single duel; widening ``evolve_once``
    to request and apply an N-challenger field is the follow-on the
    multi-candidate field (§9 lever 0) enables.
    """
    from zicato.selection.registry import make_strategy  # noqa: PLC0415
    from zicato.selection.strategy import Contestant, MatchupResult  # noqa: PLC0415

    strategy = make_strategy(tournament_spec)
    champion = Contestant(generation_id=parent_id, role="champion")
    challenger = Contestant(generation_id=child_id, role="challenger", snapshot_root=child_snapshot)
    strategy.seed(champion, [challenger])
    matchups = strategy.next_matchups()
    if not matchups:
        return strategy.champion()
    matchup = matchups[0]
    result = MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=parent_id,
        right_id=child_id,
        left_agg=tournament_result.parent_agg,
        right_agg=tournament_result.child_agg,
        outcome=tournament_result.outcome,
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )
    strategy.record_result(result)
    return strategy.champion()


def _rejected_proposer_experiment(
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    error: ProposerError,
) -> Experiment:
    """Build a placeholder experiment for a proposer that exhausted retries.

    When the proposer cannot produce a patch set that survives post-apply
    validation within its bounded retry budget, there is no real
    :class:`Experiment` to journal — but the round must still leave an
    append-only record. This synthesises a minimal experiment whose
    hypothesis carries the per-attempt failure trail and whose patch
    tuple is empty (nothing was successfully applied). The orchestrator
    stamps the rejected :class:`OutcomeRecord` onto it exactly as it
    does for a validator rejection.
    """

    from zicato.core.types import HypothesisSpec  # noqa: PLC0415

    return Experiment(
        id=f"exp_{epoch_id}_{generation_id}",
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        proposed_at=_now_iso(),
        hypothesis=HypothesisSpec(
            core_idea="proposer exhausted retries without a valid patch set",
            modulating=(),
            why=(
                "Every proposer attempt this round failed parsing or "
                "post-apply validation; see the rejected outcome for the "
                "per-attempt error trail."
            ),
            expected_drift_movements=(),
            expected_pass_rate_delta="0.0",
            risks="; ".join(error.attempts),
        ),
        patches=(),
        outcome=None,
    )


def _budget_aborted_outcome(parent_generation_id: str, budget_s: int) -> EvolveRoundOutcome:
    """Build the synthetic outcome for a round cut short by the total budget.

    Used when a single round's work is cancelled by
    :func:`asyncio.wait_for` because finishing it would push the whole
    ``evolve_n_rounds`` invocation past ``max_wall_clock_seconds``. The
    round never produced a real tournament decision, so we fabricate a
    rejection-style outcome whose ``rejection_reason`` is the symbolic
    ``"wall_clock_budget"`` string — the same token the per-entry
    budget uses for its aborts — so journal readers and the CLI can
    recognise it.
    """
    return EvolveRoundOutcome(
        parent_generation_id=parent_generation_id,
        proposed_generation_id="",
        tournament_decision="rejected",
        rejection_reason=f"wall_clock_budget: evolve total budget of {budget_s}s exceeded",
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
    )


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
    stop_on_degenerate_health: bool = True,
    max_wall_clock_seconds: int | None = None,
    stop_reason_out: list[str] | None = None,
) -> list[EvolveRoundOutcome]:
    """Loop :func:`evolve_once` up to ``rounds`` times.

    Stops early on ``max_consecutive_rejections`` rejected rounds in a
    row — that's a strong signal the proposer is stuck and the
    operator probably wants to inspect the proposer brief / patterns
    before spending more LLM calls. A successful promotion resets the
    consecutive-rejection counter.

    A second circuit breaker watches loop *health*: when
    ``stop_on_degenerate_health`` is true (the default), the loop stops
    early once :data:`_DEGENERATE_HEALTH_STOP_THRESHOLD` consecutive
    rounds report a CRITICAL loop-health finding (e.g. degenerate
    scoring — the tournament can no longer tell a real improvement from
    noise). Same spirit as the consecutive-rejection breaker: there is
    no point spending more LLM calls on a loop that is producing no
    usable signal. A round whose health is not CRITICAL resets the
    counter. Pass ``stop_on_degenerate_health=False`` to opt out and run
    every requested round regardless of health.

    A third early-exit is the **total wall-clock budget**: when
    ``max_wall_clock_seconds`` is set (``None``, the default, leaves the
    loop unbounded — the historical behaviour), the orchestrator records
    a monotonic start time and enforces the ceiling two ways:

    * **Between rounds** — before starting round N+1, if the elapsed
      time has already reached the budget, the loop stops cleanly with
      a logged message and returns the outcomes gathered so far. This
      mirrors the consecutive-reject breaker's shape exactly.
    * **Within a round** — each round's work is wrapped in
      :func:`asyncio.wait_for` with a timeout equal to the *remaining*
      budget, so a single long round cannot blow the total. A round
      that would exceed the ceiling is cancelled; it is recorded as an
      aborted round (a synthetic :class:`EvolveRoundOutcome` carrying a
      ``"wall_clock_budget"`` rejection reason) and the loop stops.

    The total budget is enforced *in addition to* — not instead of —
    each board entry's own ``wall_clock_budget_seconds``; both apply.
    Note the within-round cancellation is a Layer-1 ``asyncio.wait_for``
    guard (see ``docs/design/ROBUSTNESS.md``): it only pre-empts
    *cooperative* async work. A round wedged in a blocking call or a
    CPU-bound loop is not hard-killed here — that requires the
    subprocess-worker layer (L3). This is the same contract the
    per-entry budget relies on.

    Contract-hash auto-epoching runs ONCE, before the round loop: when
    ``epoch_id`` is ``None`` and ``auto_epoch`` is true, the orchestrator
    resolves (and, if the contract drifted, auto-rolls) the epoch via
    :func:`ensure_epoch_for_contract`. The resolved id is then pinned
    for every round of this invocation so the loop never re-rolls
    mid-flight. When ``epoch_id`` is passed explicitly, auto-rolling is
    skipped entirely — an explicit target always wins.

    The list of :class:`EvolveRoundOutcome` returned has one entry per
    round attempted (which may be fewer than ``rounds`` if any
    early-stop fired).

    ``stop_reason_out`` is an optional caller-supplied list the function
    appends a single symbolic terminal-reason string to before
    returning, so a caller (the CLI) can render a summary that
    distinguishes the terminal states without re-deriving them from the
    outcomes. One of: ``"completed"`` (all rounds ran),
    ``"consecutive_rejections"``, ``"degenerate_health"``,
    ``"wall_clock_budget_between_rounds"`` (the total budget was already
    spent before the next round started), or
    ``"wall_clock_budget_mid_round"`` (a round was cancelled because
    finishing it would overrun the total budget). Callers that do not
    pass the list see no behavioural change.
    """

    def _set_stop_reason(reason: str) -> None:
        if stop_reason_out is not None:
            stop_reason_out.append(reason)

    if rounds <= 0:
        _set_stop_reason("completed")
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
    # Resolve the harmonograf console URL once up front so the supervisor
    # / dashboard can surface a "watch live" link from the heartbeat for
    # the whole invocation. When no URL is configured (the default after
    # #202) the supervisor auto-launches an in-process harmonograf bound
    # to a free localhost port; the handle is shut down in the finally
    # block. The auto-launched URL is also pushed into ZICATO_HARMONOGRAF_URL
    # so per-board-run workers attach their per-run sinks to the same
    # server without any further plumbing.
    harmonograf_url, harmonograf_handle = _resolve_or_launch_harmonograf(workspace_root)
    # Meta-loop goldfive emitter. One per evolve invocation, stable
    # session id derived from the start ISO — the proposer + analyzer
    # call sites take it through ``evolve_once`` so their LLM calls
    # land as paired envelopes on the same harmonograf timeline workers
    # already feed. Constructed best-effort; a degraded install (no
    # goldfive proto stubs) returns an emitter with an empty sink list
    # and every emit is a no-op. The emitter is closed in the same
    # ``finally`` block that tears the harmonograf supervisor down.
    evolve_started_at_iso = _now_iso()
    meta_loop_emitter = _build_meta_loop_emitter_safe(
        workspace_root, harmonograf_url, evolve_started_at_iso
    )
    outcomes: list[EvolveRoundOutcome] = []
    try:
        await beater.start()
        # The meta-loop session id is surfaced on the heartbeat so the
        # dashboard can deep-link the top-bar "execution" entry into the
        # zicato-level harmonograf session (the proposer + judge timeline).
        # Read it off the emitter — empty when no meta-loop session is in
        # scope. See docs/design/HARMONOGRAF.md §2b/§4.
        meta_session = getattr(meta_loop_emitter, "session_id", "") or ""
        beater.update(
            epoch_id=epoch_id or "",
            phase="evolve_n_rounds:start",
            harmonograf_url=harmonograf_url,
            harmonograf_meta_session=meta_session,
        )
        beater.bump_now()
        consecutive_rejections = 0
        consecutive_critical_health = 0
        # Total wall-clock budget — a monotonic clock so a wall-clock
        # adjustment mid-run can't move the deadline. ``None`` leaves
        # the loop unbounded (the historical behaviour).
        budget_start = time.monotonic()
        budget_stopped = False
        stop_reason = "completed"
        for round_idx in range(rounds):
            # Between-rounds budget check — before spending the next
            # round's LLM calls, bail if the total budget is spent.
            # Same shape as the consecutive-reject breaker above.
            if max_wall_clock_seconds is not None:
                elapsed = time.monotonic() - budget_start
                if elapsed >= max_wall_clock_seconds:
                    log.warning(
                        "evolve_n_rounds: evolve total wall-clock budget of %ds "
                        "reached after %d rounds (round %d/%d)",
                        max_wall_clock_seconds,
                        round_idx,
                        round_idx,
                        rounds,
                    )
                    budget_stopped = True
                    stop_reason = "wall_clock_budget_between_rounds"
                    break
            beater.update(
                epoch_id=epoch_id or "",
                round_index=round_idx,
                round_started_at=_now_iso(),
                phase=f"evolve_once:round_{round_idx}",
            )
            beater.bump_now()

            async def _run_round(_round_idx: int = round_idx) -> EvolveRoundOutcome:
                return await evolve_once(
                    workspace_root=workspace_root,
                    epoch_id=epoch_id,
                    harness_call_llm=harness_call_llm,
                    auxiliary_call_llm=auxiliary_call_llm,
                    instance_id=instance_id,
                    fast_mode=fast_mode,
                    max_proposer_retries=max_proposer_retries,
                    beater=beater,
                    round_index=_round_idx,
                    total_rounds=rounds,
                    meta_loop_emitter=meta_loop_emitter,
                )

            if max_wall_clock_seconds is None:
                # Unbounded — run the round with no within-round ceiling.
                outcome = await _run_round()
            else:
                remaining = max_wall_clock_seconds - (time.monotonic() - budget_start)
                # ``remaining`` is > 0 here (the between-rounds check
                # above already returned for an exhausted budget), but
                # clamp defensively against a tiny / negative slice.
                remaining = max(remaining, 0.001)
                try:
                    # Layer-1 asyncio.wait_for guard: a round that would
                    # push past the total budget is cancelled. This only
                    # pre-empts cooperative async work — a round wedged
                    # in a blocking call or CPU-bound loop is not
                    # hard-killed here; that needs the L3 subprocess
                    # worker. Same caveat as the per-entry budget. See
                    # docs/design/ROBUSTNESS.md.
                    outcome = await asyncio.wait_for(_run_round(), timeout=remaining)
                except TimeoutError:
                    # asyncio.wait_for raises the builtin TimeoutError
                    # (asyncio.TimeoutError is an alias of it on 3.11+).
                    parent_id = _safe_resolve_parent(workspace_root, epoch_id)
                    outcome = _budget_aborted_outcome(parent_id, max_wall_clock_seconds)
                    outcomes.append(outcome)
                    log.warning(
                        "evolve_n_rounds: round %d aborted — evolve total wall-clock "
                        "budget of %ds exceeded mid-round; stopping (round %d/%d)",
                        round_idx,
                        max_wall_clock_seconds,
                        round_idx + 1,
                        rounds,
                    )
                    budget_stopped = True
                    stop_reason = "wall_clock_budget_mid_round"
                    break
            outcomes.append(outcome)
            beater.update(
                epoch_id=epoch_id or "",
                generation_id=outcome.proposed_generation_id,
                round_index=round_idx,
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
                    stop_reason = "consecutive_rejections"
                    break
            # Loop-health circuit breaker — stop early when the loop has
            # produced no usable signal for too many rounds running.
            if stop_on_degenerate_health and outcome.health_critical:
                consecutive_critical_health += 1
                if consecutive_critical_health >= _DEGENERATE_HEALTH_STOP_THRESHOLD:
                    log.warning(
                        "evolve_n_rounds: stopping after %d consecutive rounds with a "
                        "CRITICAL loop-health finding (round %d/%d) — the loop is "
                        "producing no usable signal; inspect the scoring weights / "
                        "proposer brief before resuming. (Pass "
                        "stop_on_degenerate_health=False to opt out.)",
                        consecutive_critical_health,
                        round_idx + 1,
                        rounds,
                    )
                    stop_reason = "degenerate_health"
                    break
            else:
                consecutive_critical_health = 0
        beater.update(
            phase="evolve_n_rounds:budget_exhausted" if budget_stopped else "evolve_n_rounds:done"
        )
        beater.bump_now()
    finally:
        # Defensive terminal-state write (issue: a dead/closed run reading
        # LIVE). A cleanly-ended loop stamps a terminal heartbeat phase
        # above; here we ALSO flip any lingering active-tournament envelope
        # out of ``phase="running"`` so a normally-ended run never reads as a
        # live tournament — even inside the heartbeat freshness window. Runs
        # on BOTH the clean and the error/interrupt path (a SIGKILL still
        # can't self-clean, which the frontend freshness gate covers).
        _mark_run_terminal(workspace_root)
        await beater.stop()
        release_workspace_lock(lock)
        # Flush + close the meta-loop emitter BEFORE the harmonograf
        # supervisor is stopped — a sink that needs to push a final
        # buffer to the gRPC console wants the server still up.
        if meta_loop_emitter is not None:
            try:
                await meta_loop_emitter.close()
            except Exception as exc:  # noqa: BLE001 — never raise from teardown
                log.debug("meta-loop emitter close raised: %s", exc)
        # Shut down the auto-launched harmonograf server (no-op on the
        # opt-out / failure-isolation paths). MUST run unconditionally
        # so a crashed evolve still tears the embedded server down.
        try:
            harmonograf_handle.shutdown()
        except Exception as exc:  # noqa: BLE001 — never raise from teardown
            log.debug("harmonograf shutdown raised: %s", exc)
    _set_stop_reason(stop_reason)
    return outcomes


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


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _resolve_harmonograf_url(workspace_root: Path) -> str:
    """Resolve the harmonograf console URL for this run, or ``""``.

    Delegates to :func:`zicato.telemetry.sink.resolve_harmonograf_url`,
    feeding it the workspace ``config.json`` so both the
    ``ZICATO_HARMONOGRAF_URL`` environment variable and the
    ``harmonograf_url`` config key are honoured. Best-effort: any
    failure resolving the config falls back to the empty string so a
    broken config never blocks an evolve run.

    Note this resolves only the *configured* URL — it does NOT trigger
    auto-launch. :func:`_resolve_or_launch_harmonograf` is the variant
    the evolve loop uses, which falls back to spawning an in-process
    server when the configured URL is empty.
    """
    try:
        from zicato import workspace_loader  # noqa: PLC0415
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415

        try:
            cfg = workspace_loader.load_workspace_config(workspace_root)
        except Exception:  # noqa: BLE001 — config is optional here
            cfg = None
        return resolve_harmonograf_url(cfg)
    except Exception as exc:  # noqa: BLE001 — never block a run on this
        log.debug("harmonograf url resolution skipped: %s", exc)
        return ""


def _resolve_or_launch_harmonograf(
    workspace_root: Path,
) -> tuple[str, Any]:
    """Return ``(url, handle)`` for the harmonograf console this evolve uses.

    Auto-launch semantics (the post-#202 default):

    * If the operator pinned ``ZICATO_HARMONOGRAF_URL`` (or the
      workspace-config ``harmonograf_url`` key), use that URL verbatim
      and return a no-op handle — opt-out lets a long-lived shared
      harmonograf collect traffic from multiple zicato invocations.
    * Otherwise launch an in-process harmonograf server bound to a free
      localhost port (see :mod:`zicato.telemetry.harmonograf_supervisor`)
      and return its URL + a real handle whose ``shutdown()`` the
      caller MUST invoke at evolve teardown.

    On any auto-launch failure (missing dep, port-bind error, startup
    timeout), the supervisor logs a warning and returns a no-op handle
    with ``url=""``. The orchestrator treats that as "JSONL-only
    telemetry" exactly as it did before #202 — the live console is
    additive, never load-bearing.

    Side effect: when auto-launch succeeds, the resolved URL is also
    written into ``os.environ["ZICATO_HARMONOGRAF_URL"]`` so the
    tournament runner and worker subprocesses (which re-resolve via
    :func:`zicato.telemetry.sink.resolve_harmonograf_url`) attach their
    own per-run sinks to the same server without any further plumbing.
    The orchestrator restores the pre-launch env var value on shutdown
    via :class:`_EnvVarRestorer` — a nested evolve invocation that
    inherits a parent's auto-launched URL won't clobber it.
    """
    configured = _resolve_harmonograf_url(workspace_root)
    if configured:
        # Opt-out: external harmonograf in use. No auto-launch, no
        # env-var manipulation, no shutdown needed.
        log.debug("harmonograf auto-launch skipped: external URL configured (%s)", configured)
        return configured, _NoopShutdownHandle()

    # Route through the per-workspace ensure-helper so an evolve and a
    # concurrently-open standalone dashboard share ONE harmonograf server
    # bound to the workspace's sqlite db (the ``server.json`` record is the
    # single-server-per-workspace contract — see
    # ``harmonograf_supervisor.ensure_workspace_harmonograf``). When the
    # helper REUSES an existing server (a standalone dashboard already
    # launched one), evolve does NOT own its lifecycle — it leaves it
    # running; when evolve LAUNCHED it, the handle's shutdown stops it.
    try:
        from zicato.telemetry.harmonograf_supervisor import (  # noqa: PLC0415
            ensure_workspace_harmonograf,
        )
    except Exception as exc:  # noqa: BLE001 — supervisor import is best-effort
        log.warning("harmonograf auto-launch skipped: supervisor module unavailable (%s)", exc)
        return "", _NoopShutdownHandle()

    handle = ensure_workspace_harmonograf(workspace_root)
    if not handle.web_url:
        # Helper's own failure-isolation path already logged a warning.
        return "", _NoopShutdownHandle()

    # Make the resolved URL discoverable to the tournament runner and the
    # worker subprocesses, both of which re-resolve the env var via
    # load_config()/resolve_harmonograf_url(). The restorer is captured on
    # the handle so shutdown unsets / restores the environment cleanly.
    restorers: list[_EnvVarRestorer] = []
    url_restorer = _EnvVarRestorer("ZICATO_HARMONOGRAF_URL")
    url_restorer.set(handle.web_url)
    restorers.append(url_restorer)

    # The server binds TWO ports: the web URL above (for browser deep-
    # links) and a native gRPC port the per-run sinks must dial. Export the
    # gRPC target distinctly so the sink builders dial the gRPC port
    # instead of stripping the web URL and dialing the web port (which
    # would silently drop telemetry).
    grpc_target = getattr(handle, "grpc_target", "") or ""
    if grpc_target:
        grpc_restorer = _EnvVarRestorer("ZICATO_HARMONOGRAF_GRPC")
        grpc_restorer.set(grpc_target)
        restorers.append(grpc_restorer)

    return handle.web_url, _LaunchedHandle(handle, restorers)


def _build_meta_loop_emitter_safe(
    workspace_root: Path,
    harmonograf_url: str,
    evolve_started_at_iso: str,
) -> Any:
    """Build the meta-loop emitter; never raise.

    The factory itself is best-effort — a missing goldfive proto stub
    or a permission error on the JSONL parent directory must not block
    an evolve invocation. Return ``None`` on any unexpected error so
    the orchestrator simply skips meta-loop emits (every call site is
    ``None``-tolerant).
    """
    try:
        from zicato.telemetry.meta_loop import (  # noqa: PLC0415
            build_meta_loop_emitter,
        )

        return build_meta_loop_emitter(
            workspace_root,
            harmonograf_url=harmonograf_url,
            evolve_started_at_iso=evolve_started_at_iso,
        )
    except Exception as exc:  # noqa: BLE001 — meta-loop telemetry is additive
        log.warning(
            "meta-loop emitter build failed (%s); evolve continues without "
            "proposer / analyzer telemetry envelopes",
            exc,
        )
        return None


class _NoopShutdownHandle:
    """Tiny stand-in for the auto-launch handle when no launch happened.

    Used in two places: when the operator pinned an external URL (opt-
    out) and when the supervisor refused to launch (degraded install).
    Mirrors the ``shutdown()`` contract of the real handle so the
    orchestrator's ``finally`` block can call it unconditionally.
    """

    url: str = ""

    def shutdown(self) -> None:
        return None


class _LaunchedHandle:
    """Composite handle: server lifecycle plus env-var restoration.

    Holds a list of :class:`_EnvVarRestorer` — one for the web URL
    (``ZICATO_HARMONOGRAF_URL``) and, on the auto-launch path, one for the
    native gRPC target (``ZICATO_HARMONOGRAF_GRPC``) — so shutdown returns
    both env vars to their pre-launch state.
    """

    def __init__(self, inner: Any, restorers: list[_EnvVarRestorer]) -> None:
        self._inner = inner
        self._restorers = list(restorers)
        # ``inner`` may be a ``HarmonografHandle`` (``.url``) or a
        # ``WorkspaceHarmonografHandle`` (``.web_url``); accept either.
        self.url = getattr(inner, "url", None) or getattr(inner, "web_url", "")

    def shutdown(self) -> None:
        # Restore env BEFORE stopping the server so a concurrent
        # tournament-runner re-resolve does not pick up the auto-launched
        # URL after the server is gone.
        for restorer in self._restorers:
            try:
                restorer.restore()
            except Exception as exc:  # noqa: BLE001 — restoration is best-effort
                log.debug("env restoration during harmonograf shutdown failed: %s", exc)
        try:
            self._inner.shutdown()
        except Exception as exc:  # noqa: BLE001 — never raise from teardown
            log.debug("harmonograf shutdown raised: %s", exc)


class _EnvVarRestorer:
    """RAII-style snapshot+restore for a single environment variable.

    Captures the variable's prior value on construction (or its absence)
    so :meth:`restore` returns the environment to the exact state the
    process started in. Idempotent — re-calling :meth:`restore` is a
    no-op.
    """

    def __init__(self, name: str) -> None:
        import os  # noqa: PLC0415

        self._os = os
        self._name = name
        self._had = name in os.environ
        self._prior = os.environ.get(name)
        self._restored = False

    def set(self, value: str) -> None:
        self._os.environ[self._name] = value

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        if self._had and self._prior is not None:
            self._os.environ[self._name] = self._prior
        else:
            self._os.environ.pop(self._name, None)


def _beat(beater: HeartbeatBeater | None, **fields: Any) -> None:
    """Push a heartbeat phase/coordinate update and flush it immediately.

    A no-op when ``beater`` is ``None`` (a standalone ``evolve_once``
    call with no heartbeat lifecycle). Every update is followed by a
    :meth:`HeartbeatBeater.bump_now` so the dashboard sees the new phase
    without waiting for the next periodic bump. Best-effort: a failure
    to write the heartbeat must never abort the evolve round.
    """
    if beater is None:
        return
    try:
        beater.update(**fields)
        beater.bump_now()
    except Exception as exc:  # noqa: BLE001 — heartbeat is non-critical
        log.debug("heartbeat update skipped: %s", exc)


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
    """Regenerate the comprehensive epoch analysis report — best-effort.

    The academic-paper-style epoch report is rebuilt in full after every
    round so it is always current; by epoch close it reads as a complete
    write-up. Its data-bearing sections are templated exactly from the
    structured workspace artifacts; one bounded auxiliary-LLM call writes
    the prose sections. The report is persisted as
    ``epochs/{epoch}/analysis.md`` plus a rendered ``analysis.html``
    (served by the existing dashboard endpoint).

    Strictly best-effort: any failure is swallowed and logged at debug
    level so a wedge here cannot abort the round or the loop. This is a
    separate artifact from the per-round ``insights/round_{N}.md``
    proposer-feedback files.
    """
    try:
        from zicato.analyzer import generate_epoch_report  # noqa: PLC0415

        await generate_epoch_report(
            workspace_root,
            epoch_id,
            auxiliary_call_llm,
            model=auxiliary_model,
        )
    except Exception as exc:  # noqa: BLE001 — report is best-effort
        log.debug("epoch analysis report regeneration skipped: %s", exc)


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


# ---------------------------------------------------------------------------
# Live SQLite analytical index — best-effort dual-write
# ---------------------------------------------------------------------------


#: Location of the SQLite analytical index, relative to the workspace
#: root (the ``.zicato/`` directory). The :mod:`zicato.index` sibling
#: owns the schema; the orchestrator only knows the path so it can keep
#: the index live as the loop runs.
_INDEX_DB_RELPATH = "index.db"


def _index_db_path(workspace_root: Path) -> Path:
    """Return the SQLite analytical index path for a workspace."""
    return workspace_root / _INDEX_DB_RELPATH


def _load_prior_experiments(
    workspace_root: Path,
    epoch_id: str,
) -> list[PriorExperiment]:
    """Best-effort read of the epoch's settled experiment-memory digest.

    The orchestrator threads the result into
    :func:`zicato.proposer.proposer.propose_experiment` so the proposer
    sees the ``## What's already been tried`` section. Mirrors
    :func:`_ingest_experiment_into_index`: the :mod:`zicato.index` sibling
    may be absent and a missing / stale index must never abort a round, so
    any failure — a missing module, an unreadable database — is logged at
    ``debug`` level and yields ``[]``. ``experiment.json`` on disk stays
    canonical; an empty digest simply omits the prompt section.
    """
    try:
        from zicato.index.query import prior_experiments_for_epoch  # noqa: PLC0415

        return prior_experiments_for_epoch(_index_db_path(workspace_root), epoch_id)
    except ImportError:
        log.debug("zicato.index.query unavailable; proposer runs without experiment memory")
        return []
    except Exception as exc:  # noqa: BLE001 — experiment-memory read is best-effort
        log.debug(
            "prior_experiments_for_epoch skipped for %s: %s",
            epoch_id,
            exc,
        )
        return []


def _ingest_experiment_into_index(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> None:
    """Best-effort dual-write of one generation's experiment into the index.

    Called after ``experiment.json`` is written or its outcome updated,
    so the live SQLite analytical index reflects the experiment as the
    loop runs. The :mod:`zicato.index` sibling may not be installed (it
    lands in parallel); the import is lazy and any failure — a missing
    module, a schema mismatch, an I/O error — is logged at ``debug``
    level and swallowed. ``experiment.json`` on disk stays canonical and
    ``zicato reindex`` can always rebuild the index from scratch.
    """
    try:
        from zicato.index.ingest import ingest_experiment  # noqa: PLC0415

        ingest_experiment(
            workspace_root,
            _index_db_path(workspace_root),
            epoch_id,
            generation_id,
        )
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug(
            "live index ingest_experiment skipped for %s/%s: %s",
            epoch_id,
            generation_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Per-round loop-health assessment
# ---------------------------------------------------------------------------


def _health_round_report_path(workspace_root: Path, epoch_id: str, round_n: int) -> Path:
    """Return the path of one round's loop-health report JSON.

    Layout: ``epochs/{epoch}/health/round_{N}.json``. ``N`` is the
    round number derived from the child generation id (``vN``); a
    non-``vN`` id (defensive) falls back to ``0``.
    """
    return workspace_root / "epochs" / epoch_id / "health" / f"round_{round_n}.json"


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

    gens_root = workspace_root / "epochs" / epoch_id / "generations"
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


def _assess_and_persist_loop_health(
    workspace_root: Path,
    epoch_id: str,
    round_n: int,
    board: list[Any],
) -> tuple[str, bool]:
    """Run the per-round loop-health check and persist its report.

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
      stderr WARNING in that case.

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
        losses_by_generation, experiments = _collect_epoch_health_inputs(
            workspace_root, epoch_id, board
        )
        health = assess_loop_health(
            losses_by_generation,
            experiments,
            board,
            epoch_id,
            max_generations_per_contract=_epoch_max_generations_per_contract(
                workspace_root, epoch_id
            ),
        )
    except Exception as exc:  # noqa: BLE001 — health assessment is best-effort
        log.debug("loop-health assessment skipped for %s round %d: %s", epoch_id, round_n, exc)
        return "", False

    summary, has_critical = _summarise_loop_health(health)

    try:
        report_path = _health_round_report_path(workspace_root, epoch_id, round_n)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(report_path, _loop_health_to_json(health, epoch_id, round_n))
    except Exception as exc:  # noqa: BLE001 — report write is best-effort
        log.debug("loop-health report write skipped for %s round %d: %s", epoch_id, round_n, exc)

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


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (``.tmp`` + :func:`os.replace`)."""
    import os as _os  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _os.replace(tmp, path)


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

    try:
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
    except Exception as exc:  # noqa: BLE001 — snapshot write is best-effort
        log.debug("mutations.json snapshot skipped: %s", exc)


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


__all__ = [
    "EvolveRoundOutcome",
    "evolve_once",
    "evolve_n_rounds",
]
# ``experiment_json_path`` is referenced in the module docstring's
# "step 11" — kept imported here as a re-export hook for tests that
# want to assert on the persistence target without re-deriving it.
_ = experiment_json_path
