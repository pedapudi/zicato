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
    _ensure_baseline_snapshot(
        workspace_root, resolved_epoch_id, workspace_config
    )
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
    events_paths = _build_events_paths(
        workspace_root, resolved_epoch_id, parent_id, board
    )
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
    )

    # --- 7. Validate patch set against the manifest ---
    mutations_by_id = {m.id: m for m in mutations}
    for patch in experiment.patches:
        if patch.mutation_id not in mutations_by_id:
            raise RuntimeError(
                f"proposer-emitted patch {patch.id!r} targets unknown "
                f"mutation_id {patch.mutation_id!r}"
            )
    forbidden_violations = check_forbidden_ids(
        list(experiment.patches), list(rubric.forbidden_ids)
    )
    if forbidden_violations:
        raise RuntimeError(
            "proposer-emitted patches violate forbidden_ids: "
            + "; ".join(forbidden_violations)
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
    validation_errors = validate_post_apply(
        child_snapshot, list(experiment.patches), mutations
    )
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
        parent_historical = _load_historical_aggregate(
            workspace_root, resolved_epoch_id, parent_id
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
    _cache_gen_score(
        workspace_root, resolved_epoch_id, parent_id, tournament_result.parent_agg
    )
    _cache_gen_score(
        workspace_root, resolved_epoch_id, next_id, tournament_result.child_agg
    )

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
        append_to_lineage(
            workspace_root, resolved_epoch_id, promoted_gen, parent_id=parent_id
        )
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
        append_to_lineage(
            workspace_root, resolved_epoch_id, rejected_gen, parent_id=parent_id
        )

    # --- 13. Journal ---
    append_journal_entry(workspace_root, resolved_epoch_id, finalised)

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
) -> list[EvolveRoundOutcome]:
    """Loop :func:`evolve_once` up to ``rounds`` times.

    Stops early on ``max_consecutive_rejections`` rejected rounds in a
    row — that's a strong signal the proposer is stuck and the
    operator probably wants to inspect the rubric / patterns before
    spending more LLM calls. A successful promotion resets the
    consecutive-rejection counter.

    The list of :class:`EvolveRoundOutcome` returned has one entry per
    round attempted (which may be fewer than ``rounds`` if the
    early-stop fired).
    """
    if rounds <= 0:
        return []
    if max_consecutive_rejections <= 0:
        # 0 / negative effectively disables early-stop — protect against
        # nonsense values by treating them as "never stop early".
        max_consecutive_rejections = rounds + 1

    outcomes: list[EvolveRoundOutcome] = []
    consecutive_rejections = 0
    for round_idx in range(rounds):
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
        if outcome.tournament_decision == "promoted":
            consecutive_rejections = 0
        else:
            consecutive_rejections += 1
            if consecutive_rejections >= max_consecutive_rejections:
                log.warning(
                    "evolve_n_rounds: stopping after %d consecutive rejections "
                    "(round %d/%d)",
                    consecutive_rejections,
                    round_idx + 1,
                    rounds,
                )
                break
    return outcomes


# ---------------------------------------------------------------------------
# Small helpers — kept private so the public surface stays narrow.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


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
        raise FileNotFoundError(
            f"no generations under {gens_root}; the epoch has no baseline yet"
        )
    candidates = [p.name for p in gens_root.iterdir() if p.is_dir()]
    if not candidates:
        raise FileNotFoundError(
            f"no generations under {gens_root}; the epoch has no baseline yet"
        )

    def _key(name: str) -> tuple[int, int, str]:
        if name.startswith("v") and name[1:].isdigit():
            return (0, int(name[1:]), name)
        return (1, 0, name)

    return sorted(candidates, key=_key)[-1]


def _set_current_generation(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> None:
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
        entry.id: events_jsonl_path(
            workspace_root, epoch_id, parent_id, entry.id
        )
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
        pass_rate = sum(
            1 for loss in pass_eligible if loss.pass_fail
        ) / len(pass_eligible)
        pass_part = f", pass_rate={pass_rate:.2f} over {len(pass_eligible)} entries"
    else:
        pass_part = ""
    return (
        f"drift_loss_mean={drift_mean:.3f} over {len(losses)} runs" + pass_part
    )


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

    The workspace config carries the registered ``mutable_trees`` (the
    canonical source roots the operator is willing to mutate). On first
    invocation we copy each tree under
    ``epochs/{epoch}/generations/v0/snapshot/{tree_name}/`` so the
    orchestrator's parent-resolution step finds a baseline to compare
    against. Subsequent invocations are a no-op when ``v0`` already
    exists.

    The seed snapshot is also recorded in lineage (as the unparented
    promoted head) and marked as the current generation; the same
    bookkeeping the post-promotion path performs after every successful
    round. This keeps lineage truthful when the epoch is later
    summarised by the analysis pass.
    """
    gens_root = workspace_root / "epochs" / epoch_id / "generations"
    if gens_root.exists() and any(p.is_dir() for p in gens_root.iterdir()):
        return  # already have at least one generation; nothing to do
    raw_trees = (
        workspace_config.get("mutable_trees")
        or workspace_config.get("source_roots")
        or []
    )
    if not raw_trees:
        raise RuntimeError(
            "evolve_once: workspace_config has no 'mutable_trees' / "
            "'source_roots' — cannot seed a v0 baseline snapshot. "
            "Run `zicato register --mutable-tree ...` first."
        )

    snapshot_root = _snapshot_root(workspace_root, epoch_id, "v0")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    for raw in raw_trees:
        source = Path(raw).resolve()
        if not source.exists():
            raise FileNotFoundError(
                f"evolve_once: registered mutable tree {source} does not "
                "exist on disk; baseline snapshot cannot be seeded."
            )
        if source.is_file():
            # Files are copied directly — rare in practice but cheap to
            # support so the helper does not impose tree-only semantics.
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
