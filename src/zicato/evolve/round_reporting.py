"""Round-log emission, epoch-report refresh, and loop-health inputs.

The evolve loop calls in once per settled round. Nothing here may fail a
round: every emission and every workspace read is best-effort.
"""

# ruff: noqa: E402
from __future__ import annotations

import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from zicato.util import best_effort
from zicato.workspace import WorkspaceLayout, generation_ids

if TYPE_CHECKING:
    # No annotation-only imports: every cross-module reference in this file
    # is resolved lazily at its call site.
    pass

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


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
    so the full-render path can be swapped in per-epoch if wanted); the
    per-round refresh spends no tokens.
    """
    del auxiliary_call_llm, auxiliary_model  # no per-round LLM call by design
    with best_effort(
        "epoch analysis report regeneration",
        on_error=lambda exc: log.debug("epoch analysis report regeneration skipped: %s", exc),
    ):
        from zicato.analyzer import regenerate_epoch_report_deterministic  # noqa: PLC0415

        regenerate_epoch_report_deterministic(workspace_root, epoch_id)


# ---------------------------------------------------------------------------
# Durable per-round event log — best-effort emission
# ---------------------------------------------------------------------------


#: Wire token -> the execution plan's lifecycle step. The values are exactly
#: the step keys :data:`zicato.query.execution_plan.ROUND_STEPS` defines: a
#: step this table invents is one the plan cannot place a node under, so the
#: coordinate would name a position no reader can resolve. Kept as a literal
#: table here rather than imported, so ``evolve`` does not depend on
#: ``query``; a correspondence test holds the two sides equal
#: (``test_round_log_emission.py``).
_ROUND_LOG_STEP: dict[str, str] = {
    "proposal_attempted": "propose",
    "proposal_episode_settled": "propose",
    "candidate_sampled": "propose",
    "candidate_screened": "propose",
    "critique_selected": "propose",
    "experiment_minted": "propose",
    "patches_applied": "apply",
    "validation_failed": "apply",
    "harness_loaded": "run",
    "unit_completed": "run",
    "gate_evaluated": "gate",
    "holdout_released": "gate",
    "evidence_replicated": "gate",
    "decision_recorded": "decide",
    "frontier_updated": "decide",
}

#: Tokens that carry no step by design. The round's own boundaries are not
#: steps within it — the plan has five steps, and open/close are neither. This
#: set exists so steplessness is a stated decision rather than an omission: the
#: correspondence test requires every known event token to appear in exactly
#: one of these two tables, so a new token cannot become silently stepless.
_STEPLESS_EVENTS: frozenset[str] = frozenset({"round_opened", "round_closed"})


def _duel_scope(
    *,
    generation_id: str = "",
    opponent_generation_id: str = "",
    matchup_id: str = "",
) -> dict[str, Any]:
    """The plan scope shared by both events a settled duel emits.

    The unit and gate emitters describe the same duel from two angles, so
    they must describe it with the same shape: one challenger coordinate and
    the pair's identity beside it. Built here once so the two cannot drift
    into different keys or a different key order. The opponent and the
    matchup ride
    ``attributes`` because neither is a named coordinate in the envelope's
    vocabulary yet; that is a schema decision for the reader that needs them.
    """
    scope: dict[str, Any] = {}
    if generation_id:
        scope["generation_id"] = str(generation_id)
    attributes = {
        **({"matchup_id": matchup_id} if matchup_id else {}),
        **(
            {"opponent_generation_id": str(opponent_generation_id)}
            if opponent_generation_id
            else {}
        ),
    }
    if attributes:
        scope["attributes"] = attributes
    return scope


class _RoundLogEmitter:
    """Best-effort appender onto one round's durable RoundLog.

    A STORAGE failure must never fail a round — the live index dual-write
    (:func:`_ingest_experiment_into_index`) is the precedent: the canonical
    stores (``experiment.json``, lineage, journal) stay authoritative and
    the event log is a derived, replayable trace. A bind failure degrades
    to a permanent no-op emitter; an append that cannot reach the disk is
    logged at ``debug`` and swallowed.

    A SCHEMA mistake is not swallowed. Building the typed event happens
    outside that guard, so a payload field no event declares raises from the
    constructor rather than dropping the event. The reason is that ``seq``
    comes from the file's tail: a silently dropped event leaves a gap-free
    log, which reads as a round that never emitted the event at all.

    ``emit`` takes the wire ``type_token``, its payload fields and its
    optional plan ``scope`` — the same three-argument string-token seam the
    proposer-side callback uses (:attr:`ProposerContext
    .round_event_emitter`), so one signature serves both sides. Scope is a
    separate argument and never a payload key, so no emitter on the seam can
    forward it into an event constructor. An unknown token is silently
    dropped (never a crash on a vocabulary skew).
    """

    __slots__ = ("_log",)

    def __init__(self, workspace_root: Path, epoch_id: str, round_index: int) -> None:
        self._log: Any = None
        try:
            from zicato.epoch.round_log import RoundLog  # noqa: PLC0415

            self._log = RoundLog(workspace_root, epoch_id, round_index)
        except Exception as exc:  # noqa: BLE001 — emission must never fail a round
            log.debug("round-log emitter unavailable: %s", exc)

    def emit(
        self,
        type_token: str,
        fields: dict[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one typed event under its plan ``scope``.

        The emitter fills exactly one coordinate itself: the lifecycle
        ``step`` its wire token maps to in :data:`_ROUND_LOG_STEP`, which no
        payload carries. A token in :data:`_STEPLESS_EVENTS` gets none.
        Everything else comes from the caller, because only the caller knows
        it. In particular ``replicate`` is never derived from the payload: the
        one event that carries a replicate is ``unit_completed``, whose value
        is the aggregate placeholder ``0`` rather than the draw's true index
        (see :func:`_emit_tournament_units`), so promoting it would state a
        plan coordinate the loss files contradict.
        """
        if self._log is None:
            return
        from zicato.epoch.round_log import EVENT_TYPES  # noqa: PLC0415

        cls = EVENT_TYPES.get(type_token)
        if cls is None:
            return
        event = cls(**(fields or {}))
        coordinates = dict(scope or {})
        step = _ROUND_LOG_STEP.get(type_token)
        if step:
            coordinates.setdefault("step", step)
        try:
            self._log.append(event, scope=coordinates)
        except OSError as exc:
            log.debug("round-log emit %s skipped: %s", type_token, exc)


def _emit_tournament_units(
    round_log: _RoundLogEmitter,
    tournament_result: Any,
    *,
    parent_generation_id: str = "",
    child_generation_id: str = "",
    matchup_id: str = "",
) -> None:
    """Emit ``unit_completed`` events for a settled duel's board units.

    Emitted as an AGGREGATE after the duel settles (per-unit emission at
    the runner layer would thread a callback through the subprocess-worker
    boundary — too invasive for the runner's contract): one event per
    ``(entry, side)`` pair off the duel's ``per_entry_losses`` map, with
    ``replicate=0`` (the runner's per-entry map carries the canonical
    replicate; extra replicates fold into the aggregates upstream and are
    not re-derivable here). Best-effort like every emission.

    ``parent_generation_id`` / ``child_generation_id`` NAME the two sides.
    A field round settles several matchups into ONE round log, so the same
    ``entry_id`` arrives with ``side="child"`` once per challenger; without
    the generation id those events collide and no reader can separate them.
    ``matchup_id`` distinguishes repeat or field matchups involving the same
    generation. The entry and the side stay payload fields and are NOT copied
    into the scope: a coordinate the payload already states does not need a
    second copy that could drift from it. The placeholder replicate reaches
    neither — the absent ``replicate`` coordinate is the honest statement that
    this event does not name a draw.
    """
    per_entry = getattr(tournament_result, "per_entry_losses", None) or {}
    try:
        entry_ids = sorted(per_entry)
    except Exception:  # noqa: BLE001 — emission must never fail a round
        return
    side_generations = {"parent": parent_generation_id, "child": child_generation_id}
    side_opponents = {"parent": child_generation_id, "child": parent_generation_id}
    for entry_id in entry_ids:
        for side in ("parent", "child"):
            round_log.emit(
                "unit_completed",
                {"entry_id": str(entry_id), "replicate": 0, "side": side},
                _duel_scope(
                    generation_id=side_generations[side],
                    opponent_generation_id=side_opponents[side],
                    matchup_id=matchup_id,
                ),
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
            # ALSO in the scope, though the payload states it: a reader that
            # groups by challenger must not have to know which event types
            # happen to repeat the id in their own payload.
            {"generation_id": gen_id},
        )


# ``str_tuple`` and the tree-import-gap reader (``epoch_tree_import_gaps``)
# live in zicato.health.inputs — pure workspace reads shared with the
# standalone `zicato health` CLI, which needs the same findings from a
# point-in-time invocation rather than a live round.


def _emit_gate_evaluated(
    round_log: _RoundLogEmitter,
    outcome: Any,
    *,
    parent_agg: Any = None,
    child_agg: Any = None,
    weights: Any = None,
    generation_id: str = "",
    opponent_generation_id: str = "",
    matchup_id: str = "",
) -> None:
    """Emit the ``gate_evaluated`` event for one settled duel's gate verdict.

    ``rule_fired`` carries the gate's own ``reason`` verbatim (the string
    that names which rule rejected; empty on a clean promote — the gate
    reports no rule for a pass).

    The scalars the gate decided on are recorded STRUCTURALLY alongside it, on
    BOTH decisions, so a promoted duel carries its numbers and a downstream
    effect-size analysis is not missing exactly its promotions (see
    :class:`~zicato.epoch.round_log.GateEvaluated`). They come from the same
    aggregates and weights the gate itself was handed, so the event cannot
    disagree with the decision it describes.

    The three sources are keyword-optional: a caller that supplies none of
    them still emits a well-formed event with the scalars ABSENT (``None``)
    rather than fabricating zeros. Same tolerance within a call — a
    hand-built aggregate carrying no ``scalar``, or a non-numeric one,
    leaves that field absent
    rather than failing the round, matching the best-effort discipline of
    every other emission.

    The gate's ``attributable_regressions`` — the entries that regressed on
    their own evidence whatever the verdict (issue #130) — travel too, and only
    when non-empty, so an ordinary duel's payload carries no such key. They
    are recorded ALONGSIDE ``rule_fired``, never inside it: on a promotion
    ``rule_fired`` is empty by invariant, and that is the case where this list
    has something to say.
    """
    fields: dict[str, Any] = {
        "rule_fired": str(getattr(outcome, "reason", "") or ""),
        "decision": str(getattr(outcome, "decision", "") or ""),
    }
    regressions = getattr(outcome, "attributable_regressions", ()) or ()
    if regressions:
        fields["attributable_regressions"] = tuple(str(entry_id) for entry_id in regressions)
    for key, agg in (("champion_scalar", parent_agg), ("challenger_scalar", child_agg)):
        if isinstance(agg, dict):
            raw = agg.get("scalar")
            if _is_real_number(raw):
                fields[key] = float(raw)
    margin = getattr(weights, "promote_margin", None)
    if _is_real_number(margin):
        fields["margin_required"] = float(margin)
    # A field round gates each matchup into ONE round log; without the
    # challenger's id every verdict reads as the round's own.
    round_log.emit(
        "gate_evaluated",
        fields,
        _duel_scope(
            generation_id=generation_id,
            opponent_generation_id=opponent_generation_id,
            matchup_id=matchup_id,
        ),
    )


def _promoted_entry_regressions(tournament_result: Any) -> dict[str, dict[str, Any]] | None:
    """Return the per-entry regression evidence for a duel that PROMOTED (#130).

    ``None`` unless the gate promoted AND named entries in its
    ``attributable_regressions`` — a rejected challenger is discarded, so
    nothing it regressed enters the lineage and there is nothing to warn
    about. Otherwise the detail comes from
    :func:`zicato.tournament.gate.attributable_regression_detail` over the
    SAME aggregates the gate decided on, so the health finding can never name
    an entry the outcome did not.

    Best-effort like every other health input: a result shaped differently
    than expected (a stubbed runner, a hand-built outcome) yields ``None``
    rather than failing the round.
    """
    try:
        outcome = getattr(tournament_result, "outcome", None)
        if outcome is None or str(getattr(outcome, "decision", "")) != "promoted":
            return None
        if not getattr(outcome, "attributable_regressions", ()):
            return None
        parent_agg = tournament_result.parent_agg
        child_agg = tournament_result.child_agg
        if not isinstance(parent_agg, dict) or not isinstance(child_agg, dict):
            return None
        from zicato.tournament.gate import attributable_regression_detail  # noqa: PLC0415

        detail = attributable_regression_detail(parent_agg, child_agg)
    except Exception as exc:  # noqa: BLE001 — a health input never fails a round
        log.debug("attributable per-entry regression detail unavailable: %s", exc)
        return None
    return {entry_id: dict(row) for entry_id, row in detail.items()} or None


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

    layout = WorkspaceLayout.from_root(workspace_root)
    for gen_id in generation_ids(layout, epoch_id):
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


# ``epoch_noise_floor_inputs`` and ``epoch_preflight_record`` live in
# zicato.health.inputs alongside the tree-import-gap reader, for the same
# reason: the CLI needs them too.
