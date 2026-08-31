"""Decision inputs and outcome support shared by round strategies."""

# ruff: noqa: E402
from __future__ import annotations

import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from zicato.evolve import generation_phase
from zicato.evolve.lifecycle_services import (
    _beat,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.util import best_effort

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
    """Settle one round as DEFERRED on the endpoint-outage circuit.

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
    """Read ONE champion loss profile per board entry, in board order.

    Board order keeps the view stable for detectors that care about ordering.
    Per entry the resolution is:

    1. The canonical duel slot ``runs/<entry>/loss.json`` (replicate 0) when it
       exists. It is a draw under the round's own conditions, so it always wins.
    2. Otherwise the champion's A/A calibration draws
       (``loss.r1000.json``, ``loss.r1001.json``, …), FOLDED across draws into
       one profile. The contract pre-flight writes them by running the champion
       over the whole board before the epoch's first duel exists, which is
       which is when step 1 finds nothing; without this the proposer opens every
       epoch with an empty baseline channel. Folding rather than picking keeps
       one profile per entry, so the outcome marginals' denominator stays "runs
       on the board" and detector counts do not multiply — and the draws differ
       by construction, which is why the band exists at all. The folded
       profile carries the first draw's ``aa-calibration:0`` ``match_id``, so a
       calibration-sourced baseline reads as what it is.
    3. Neither ⇒ the entry is skipped silently. A freshly-initialised epoch
       whose pre-flight is off has no champion telemetry at all.

    No other replicate band is read. The pre-flight's DELIBERATELY-DEGRADED
    probes cache in this same directory under the champion's own generation id
    (:data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE`), so a
    ``glob("loss*.json")`` would tell the proposer the champion fails in ways
    its real code does not; discovery goes through the reserved-base filter
    (:func:`zicato.tournament.unit_cache.own_code_board_draws`) and the
    calibration band is then selected by name. The band is enumerated from
    disk, never counted: draws accumulate across re-runs as cache hits, so what
    is persisted is a high-water mark and not the current run count.

    Holdout entries are never opened: the caller passes the TRAIN slice, and
    the calibration draws covering the full board are read one entry at a
    time, so nothing entry-identifying reaches the proposer.
    """
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

    # Nothing under evolve/ imports the unit cache at module scope; the
    # reserved-base
    # filter and the replicate fold both live there because that module owns
    # the (generation, entry, replicate) key this reader is inverting.
    from zicato.tournament.calibration import (  # noqa: PLC0415
        CALIBRATION_REPLICATE_BASE,
        CALIBRATION_REPLICATE_SPAN,
    )
    from zicato.tournament.unit_cache import (  # noqa: PLC0415
        _average_losses,
        own_code_board_draws,
    )

    def _read(path: Path) -> Any | None:
        try:
            return read_loss_profile(path)
        except (OSError, ValueError, KeyError):
            return None

    canonical: dict[str, Any] = {}
    uncovered: list[Any] = []
    for entry in board:
        lpath = loss_profile_path(workspace_root, epoch_id, parent_id, entry.id)
        profile = _read(lpath) if lpath.exists() else None
        if profile is None:
            uncovered.append(entry)
        else:
            canonical[entry.id] = profile

    # One board map per calibration draw index, so the fold is a single
    # `_average_losses` call over board-shaped runs rather than a per-entry
    # singleton wrap (which would fold nothing). `_average_losses` takes its
    # entry set from the FIRST map, which is sound here because every
    # calibration draw covers the WHOLE board (a draw that aborts raises
    # NoiseFloorInconclusive and persists nothing), so the lowest draw index
    # covers every entry any later draw does — and being lowest, it is also
    # the replicate whose provenance the folded profiles carry.
    band_end = CALIBRATION_REPLICATE_BASE + CALIBRATION_REPLICATE_SPAN
    draws: dict[int, dict[str, Any]] = {}
    for entry in uncovered:
        run_dir = loss_profile_path(workspace_root, epoch_id, parent_id, entry.id).parent
        for index, path in own_code_board_draws(run_dir):
            if not CALIBRATION_REPLICATE_BASE <= index < band_end:
                continue
            profile = _read(path)
            if profile is not None:
                draws.setdefault(index, {})[entry.id] = profile
    folded = _average_losses([draws[index] for index in sorted(draws)]) if draws else {}
    if folded:
        log.info(
            "proposer baseline: %d board entr(y/ies) have no duel replicate; "
            "folded %d A/A calibration draw(s) for %d of them",
            len(uncovered),
            len(draws),
            len(folded),
        )

    losses: list[Any] = []
    for entry in board:
        resolved = canonical.get(entry.id)
        if resolved is None:
            resolved = folded.get(entry.id)
        if resolved is not None:
            losses.append(resolved)
    return losses


def _build_events_paths(
    workspace_root: Path,
    epoch_id: str,
    parent_id: str,
    board: list[Any],
) -> dict[str, Path]:
    """Map entry id → the parent generation's transcript for that entry.

    Replicate-aware via :func:`any_unit_transcript`: at the first round's
    proposal the only draws on disk are the contract pre-flight's probe and
    the calibration band, none of which is replicate 0.
    """
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415
    from zicato.tournament.unit_cache import any_unit_transcript  # noqa: PLC0415

    return {
        entry.id: any_unit_transcript(
            events_jsonl_path(workspace_root, epoch_id, parent_id, entry.id)
        )
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
    signal, so the proposer prompt then carries no such section.
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
    sentinel — when the knob is off (the default, under which no extraction
    runs at all), when no pattern has
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


#: How many per-channel terms the loss summary reports. The summary is one
#: orienting line rather than a decomposition: past a handful of terms the ones the
#: contract weights most stop standing out, which is the defect this cap
#: exists to avoid re-introducing.
_LOSS_SUMMARY_TERMS_PER_CHANNEL: int = 4


def _render_loss_summary(losses: list[Any], priorities: Any = None) -> str:
    """Render a short human-readable loss summary for the proposer prompt.

    ``priorities`` is the round's :class:`~zicato.proposer.prompts
    .MetricPriorities`. When supplied, the summary reports only terms the
    contract actually scores, each channel in weight order: a contract that
    zeroes ``namespace_weights["drift:"]`` does not lead with a
    ``drift_loss_mean`` that contributes nothing to the scalar, and a
    heavily-weighted judge is named rather than left implicit. ``None`` (the
    default, and every caller that holds no weights) renders the unfiltered
    form.

    The empty sentinels are unchanged: no losses is still a baseline
    round, and a contract whose scored terms have no measurement yet says so
    rather than reporting an unweighted number to fill the line.
    """
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
    if priorities is None:
        return f"drift_loss_mean={drift_mean:.3f} over {len(losses)} runs" + pass_part

    parts: list[str] = []
    # ``drift_loss_mean`` is the drift channel's own aggregate — judges are a
    # separate channel, reported per judge below — so it is score-bearing
    # when, and only when, some drift kind survived the zero filter.
    if priorities.drift_kinds:
        parts.append(f"drift_loss_mean={drift_mean:.3f} over {len(losses)} runs")
    if priorities.pass_rate_weight and pass_eligible:
        parts.append(pass_part.lstrip(", "))
    parts.extend(
        _mean_named_terms(losses, priorities.judges, _judge_loss_values),
    )
    parts.extend(
        _mean_named_terms(losses, priorities.namespace_metrics, _unified_metric_values),
    )
    return ", ".join(parts) or "(the terms this contract scores have no measurement yet)"


def _judge_loss_values(loss: Any) -> dict[str, float]:
    """One run's per-judge drift attribution, by judge name.

    Reads ``weighted_loss`` — the contribution the aggregate ``drift_loss``
    already sums in — so a named judge's number and the drift mean beside it
    are on the same footing. The ``""`` catch-all bucket for unattributed
    ``custom`` drifts is not a judge and is skipped.
    """
    return {
        str(jl.judge_name): float(jl.weighted_loss)
        for jl in getattr(loss, "per_judge_loss", ()) or ()
        if getattr(jl, "judge_name", "")
    }


def _unified_metric_values(loss: Any) -> dict[str, float]:
    """One run's merged namespaced metric view, by metric name."""
    metrics = getattr(loss, "unified_metrics", None)
    if metrics is None:
        return {}
    return {str(mc.name): float(mc.count) for mc in metrics()}


def _mean_named_terms(
    losses: list[Any],
    targets: tuple[Any, ...],
    values_of: Callable[[Any], dict[str, float]],
) -> list[str]:
    """``name=mean`` for the top scored ``targets`` that the runs measured.

    ``targets`` arrives in the contract's own weight order, so truncating to
    :data:`_LOSS_SUMMARY_TERMS_PER_CHANNEL` keeps the terms the contract cares
    about most. A target no run reported is omitted rather than printed as
    zero — an unmeasured term and a measured-zero term are different findings.
    """
    per_name: dict[str, list[float]] = {}
    for loss in losses:
        for name, value in values_of(loss).items():
            per_name.setdefault(name, []).append(value)
    rendered: list[str] = []
    for target in targets:
        samples = per_name.get(target.name)
        if not samples:
            continue
        rendered.append(f"{target.name}={sum(samples) / len(samples):.3f}")
        if len(rendered) == _LOSS_SUMMARY_TERMS_PER_CHANNEL:
            break
    return rendered


def build_metric_priorities(board: list[Any], weights: Any, losses: list[Any]) -> Any:
    """Resolve what the frozen contract scores, per channel, weight-ordered.

    The prompt-side half of "the operator already answered what to work on".
    Every target is resolved through the SAME rules the scalar uses
    (:func:`zicato.scoring.builtins._kind_multiplier`,
    :func:`zicato.scoring.builtins.builtin_scalar`), and anything the contract
    weights at zero is dropped, so a target the prompt names can always move
    the score. Returns a :class:`~zicato.proposer.prompts.MetricPriorities`.

    Two resolutions are worth stating because they are easy to get wrong:

    * Custom judges are their own channel: each judge's per-judge weight is
      scaled by ``namespace_weights["judge:"]``, so zeroing that coefficient
      drops every judge — the same way they drop out of the scalar — while a
      zeroed ``drift:`` coefficient leaves them ranked.
    * Namespace metric NAMES come from the round's own loss profiles, because
      only the data knows whether this board reports ``cost:tokens_spent`` or
      ``rubric:slide_structure``. A round with no losses names the weighted
      namespace prefixes instead of inventing metric names, which is why this
      rides the same change as the baseline-loss reader.

    This does NOT touch the validator's accept-list
    (``_declared_custom_judge_names``): that set stays permissive, so a
    zero-weight judge that the prompt does not advertise is still parsed
    without a burned retry.
    """
    from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS  # noqa: PLC0415
    from zicato.proposer.prompts import MetricPriorities, ScoredTarget  # noqa: PLC0415

    namespace_weights = dict(getattr(weights, "namespace_weights", None) or {})
    drift_weight = float(namespace_weights.get("drift:", 0.0) or 0.0)
    judge_weight = float(namespace_weights.get("judge:", 0.0) or 0.0)
    per_kind = dict(getattr(weights, "per_kind_weights", None) or {})
    per_judge = dict(getattr(weights, "per_judge_weights", None) or {})
    default_judge = float(getattr(weights, "default_judge_weight", 1.0))

    def _ranked(targets: list[Any]) -> tuple[Any, ...]:
        return tuple(sorted(targets, key=lambda t: (-abs(t.weight), t.name)))

    judge_names: set[str] = set(str(name) for name in per_judge)
    for entry in board:
        for judge in getattr(entry, "judges", ()) or ():
            name = getattr(judge, "name", None)
            if name:
                judge_names.add(str(name))
    judges = [
        ScoredTarget(name, judge_weight * float(per_judge.get(name, default_judge)))
        for name in sorted(judge_names)
    ]

    drift_kinds = [
        # The `custom` kind is the judges' own channel — it resolves through
        # per_judge_weights, never per_kind_weights — so it is named there.
        ScoredTarget(kind, drift_weight * float(per_kind.get(kind, 1.0)))
        for kind in sorted(GOLDFIVE_DRIFT_KINDS)
        if kind != "custom" and not kind.startswith("custom:")
    ]

    observed: set[str] = set()
    for loss in losses:
        metrics = getattr(loss, "unified_metrics", None)
        if metrics is not None:
            observed.update(str(mc.name) for mc in metrics())
    namespace_metrics: list[Any] = []
    for namespace, weight in namespace_weights.items():
        # ``drift:`` and ``judge:`` are already advertised above, per kind and
        # per judge — the banded within-channel form the proposer acts on.
        # Naming them here as bare namespaces would double-advertise them.
        if namespace in ("drift:", "judge:") or not float(weight):
            continue
        names = sorted(name for name in observed if name.startswith(namespace))
        for name in names or [namespace]:
            namespace_metrics.append(ScoredTarget(name, float(weight)))

    return MetricPriorities(
        judges=_ranked([t for t in judges if t.weight]),
        drift_kinds=_ranked([t for t in drift_kinds if t.weight]),
        pass_rate_weight=float(getattr(weights, "pass_weight", 0.0) or 0.0),
        namespace_metrics=_ranked(namespace_metrics),
    )
