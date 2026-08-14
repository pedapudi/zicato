"""Decision inputs and outcome support shared by round strategies."""

# ruff: noqa: E402
from __future__ import annotations

import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.evolve import generation_phase
from zicato.evolve.lifecycle_services import (
    _beat,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.util import best_effort

if TYPE_CHECKING:
    # Annotation-only — the proposer module is imported lazily inside
    # ``evolve_once`` (see the module docstring on lazy imports), so its
    # exception type is referenced here purely for type annotations.
    pass

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]

from zicato.evolve.round_api import DEFERRED_INFRA_DECISION, EvolveRoundOutcome
from zicato.evolve.round_prepare import _assess_and_persist_loop_health
from zicato.evolve.round_reporting import _RoundLogEmitter


def _field_failure_summary(field_status: list[dict[str, Any]]) -> str:
    """Bucket a multi-challenger field's per-slot failures by reason.

    Each rejected slot already carries a condensed ``reason`` (the
    proposer's :func:`_short_reject_reason` line, or a field-diversity
    soft-reject code). Counting them turns "the field failed" into "every
    slot hit the same parse error" or "four slots, four different
    failures" — two situations with different first moves. Empty when no
    slot recorded a status, which leaves the caller's reason as it was.
    """
    causes: dict[str, int] = {}
    for status in field_status:
        if not isinstance(status, dict) or status.get("status") == "applied":
            continue
        reason = str(status.get("reason") or "").strip() or "(no reason recorded)"
        causes[reason] = causes.get(reason, 0) + 1
    if not causes:
        return ""
    ranked = sorted(causes.items(), key=lambda kv: -kv[1])
    total = sum(causes.values())
    return f"{total} slot(s): " + ", ".join(f"{count}x {reason}" for reason, count in ranked)


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
        generation_phase.round_number(next_id) or round_index,
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
