"""Promotion confirmation, operator overrides, and integrity checks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Generation, TournamentDecision
from zicato.evolve.lifecycle_services import _now_iso
from zicato.runtime.control_consumer import GateOverride, claim_field_gate_overrides

if TYPE_CHECKING:
    from zicato.evolve.field_candidates import CandidateField
    from zicato.evolve.generation_phase import FieldRound

log = logging.getLogger("zicato.orchestrator")


def _field_override_provenance(workspace_root: Path, override: GateOverride) -> dict[str, Any]:
    """Build a per-generation override-status readback record.

    Surfaced on the durable field-tournament record so the dashboard's
    structure reader can render WHICH candidate an operator force-promoted /
    force-rejected, why, and when — the override is never silent. Shape:
    ``{action, ts, reason, state}`` where ``action`` is ``"promote"`` /
    ``"reject"``, ``state`` is ``"applied"`` (the override fired this round),
    and ``ts`` is the consume time. ``workspace_root`` is accepted so a future
    revision can fold in the consumed control-log sidecar without changing the
    callers.
    """
    del workspace_root  # reserved for a future control-log cross-reference
    return {
        "action": "promote" if override.decision == "promoted" else "reject",
        "ts": _now_iso(),
        "reason": override.reason,
        "state": "applied",
    }


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


@dataclass(frozen=True, slots=True)
class _CrowningHoldout:
    """The post-holdout crowning state for a resolved multi-challenger field.

    ``promoted_id`` is the (possibly demoted-to-``None``) crowned
    generation; ``reason_override`` carries the holdout cause when the
    confirmation flipped a train win; the remaining fields are the
    evidence the crowning challenger's :class:`OutcomeRecord` stamps
    (holdout block + the train/holdout scalar pair the generalization gap
    is measured from).
    """

    promoted_id: str | None
    reason_override: str | None = None
    holdout_block: dict[str, Any] | None = None
    holdout_child_scalar: float | None = None
    challenger_id: str | None = None
    challenger_train_scalar: float | None = None
    #: The crowning duel's gate delta, normalized to the champion-as-parent
    #: orientation (``challenger_scalar - champion_scalar``; negative =
    #: improvement) — the scalar evidence the opt-in gate-contradiction
    #: block re-derives against. ``None`` when no crowning duel ran.
    crowning_delta_scalar: float | None = None


def _released_holdout_confirmation(block: dict[str, Any] | None) -> bool | None:
    """Return the released confirmation bit, or ``None`` when none was released."""
    if (
        block is None
        or block.get("holdout_consulted") is not True
        or block.get("ladder_released") is not True
    ):
        return None
    confirmed = block.get("confirmed")
    return confirmed if isinstance(confirmed, bool) else None


async def _confirm_crowning_on_holdout(
    *,
    decision: Any,
    parent_id: str,
    champion_gen: Generation,
    generation_for: Callable[[str], Generation],
    adapter: Any,
    board: list[Any],
    weights: Any,
    config: Any,
    workspace_root: Path,
    epoch_id: str,
    disable_drift: tuple[Any, ...],
    judge_only: bool,
    fast_mode: bool,
    confirm_fn: Any,
) -> _CrowningHoldout:
    """Confirm a field's crowning train-promote on the holdout slice.

    OVERFITTING.md §3/§4 on the multi-challenger path: the structure
    resolved its leader on the TRAIN slice; a ``promoted`` crowning duel
    must ALSO confirm on the holdout — through the SAME Ladder-mediated
    machinery + per-epoch budget the gauntlet uses (``confirm_fn`` is
    :func:`zicato.tournament.runner.confirm_crowning_holdout`, injected so
    the decision shape is unit-testable). A released non-confirmation flips
    the crowning promote to a holdout reject: the champion stands and
    ``reason_override`` carries the cause. No crowning duel / a
    non-promote / an empty holdout ⇒ the decision passes through unchanged.

    The champion (parent) side is resolved defensively — ``left`` is the
    champion by the strategy's convention, but a future strategy that seeds
    the champion on the right still confirms the right pair. The crowning
    challenger's TRAIN-slice scalar is paired with its holdout-slice scalar
    so the generalization gap is measured on the SAME crowning duel
    (mirroring the gauntlet).
    """
    promoted_id = decision.promoted_generation_id
    crowning_result = (
        next(
            (m for m in decision.matchups if m.matchup_id == decision.crowning_matchup_id),
            None,
        )
        if decision.crowning_matchup_id
        else None
    )
    if crowning_result is None or decision.decision == "rejected":
        return _CrowningHoldout(promoted_id=promoted_id)

    champ_is_left = crowning_result.left_id == parent_id
    challenger_crown_id = crowning_result.right_id if champ_is_left else crowning_result.left_id
    champ_train_agg = crowning_result.left_agg if champ_is_left else crowning_result.right_agg
    challenger_train_agg = crowning_result.right_agg if champ_is_left else crowning_result.left_agg
    challenger_train_scalar = float(challenger_train_agg.get("scalar", 0.0))
    # The gate's delta treats LEFT as parent; normalize to the
    # champion-as-parent orientation for the contradiction re-derivation.
    raw_delta = float(crowning_result.outcome.delta_scalar)
    crowning_delta_scalar = raw_delta if champ_is_left else -raw_delta
    if decision.decision != "promoted" or promoted_id is None:
        return _CrowningHoldout(
            promoted_id=promoted_id,
            challenger_id=challenger_crown_id,
            challenger_train_scalar=challenger_train_scalar,
            crowning_delta_scalar=crowning_delta_scalar,
        )
    (
        crowning_outcome,
        holdout_block,
        holdout_child_scalar,
    ) = await confirm_fn(
        adapter=adapter,
        champion_gen=champion_gen,
        challenger_gen=generation_for(challenger_crown_id),
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
    reason_override: str | None = None
    if crowning_outcome.decision != "promoted":
        # The holdout flipped a bracket-leader's train win to a reject:
        # the champion stands, the crowned generation is demoted to a
        # dead branch, and the crowning reason carries the holdout cause.
        promoted_id = None
        reason_override = crowning_outcome.reason
    return _CrowningHoldout(
        promoted_id=promoted_id,
        reason_override=reason_override,
        holdout_block=holdout_block,
        holdout_child_scalar=holdout_child_scalar,
        challenger_id=challenger_crown_id,
        challenger_train_scalar=challenger_train_scalar,
        crowning_delta_scalar=crowning_delta_scalar,
    )


def _apply_field_overrides(
    *,
    workspace_root: Path,
    decision: Any,
    promoted_id: str | None,
    crowning_reason_override: str | None,
    field_overrides: dict[str, GateOverride],
    structure: str,
) -> tuple[str | None, set[str], dict[str, dict[str, Any]], Any]:
    """Re-resolve the field's crowning under claimed operator overrides — PURE.

    Given the strategy's ``decision``, the post-holdout ``promoted_id``, and
    the operator's claimed per-generation overrides, derives:

    * ``promoted_ids`` — the (possibly multi-element) promoted SET. With no
      override it is exactly ``{promoted_id}`` (or empty), so the
      single-promotion path is unaffected.
    * ``promoted_id`` — the PRIMARY head that advances
      ``current_generation``. The crowned leader when it survived;
      otherwise the lowest-scalar operator-promoted candidate (mirroring the
      gate's lower-scalar-wins convention); ``None`` when every leader was
      force-rejected (the champion stands).
    * ``override_provenance`` — the per-generation override-status readback
      for the durable field record (never a silent flip).
    * ``effective_decision`` — the EFFECTIVE crowning verdict the workspace
      will actually commit: the post-confirmation/post-override truth every
      durable store must describe (issue #20). A flip rewrites the decision
      to ``rejected`` with the cause and no promoted id; an override's
      reason is carried so the settled bracket / journal one-liner is
      legible.
    """
    promoted_ids: set[str] = {promoted_id} if promoted_id is not None else set()
    override_provenance: dict[str, dict[str, Any]] = {}
    if field_overrides:
        for gid, ov in field_overrides.items():
            override_provenance[gid] = _field_override_provenance(workspace_root, ov)
            if ov.decision == "promoted":
                promoted_ids.add(gid)
                log.warning(
                    "evolve: operator field override — generation %s force-promoted "
                    "(structure %s); recording as an explicit override. reason=%s",
                    gid,
                    structure,
                    ov.reason,
                )
            else:  # "rejected"
                promoted_ids.discard(gid)
                log.warning(
                    "evolve: operator field override — generation %s force-rejected "
                    "(structure %s); recording as an explicit override. reason=%s",
                    gid,
                    structure,
                    ov.reason,
                )
        # Re-resolve the PRIMARY head after the overrides mutated the set.
        if promoted_id is not None and promoted_id in promoted_ids:
            pass  # leader survived — primary head unchanged
        elif promoted_ids:
            promoted_id = min(
                promoted_ids,
                key=lambda g: (
                    float((_first_aggregate_for(g, decision) or {}).get("scalar", 0.0)),
                    g,
                ),
            )
        else:
            promoted_id = None

    override_decision_reason: str | None = None
    if field_overrides:
        head_ov = field_overrides.get(promoted_id) if promoted_id is not None else None
        if head_ov is not None and head_ov.decision == "promoted":
            override_decision_reason = f"operator override: {head_ov.reason}"
        elif promoted_id is None:
            # The leader was force-rejected and no candidate was promoted in
            # its place — the champion stands under the operator's reject.
            rej = next(
                (o for o in field_overrides.values() if o.decision == "rejected"),
                None,
            )
            if rej is not None:
                override_decision_reason = f"operator override: {rej.reason}"

    effective_decision = decision
    _decision_reason = (
        override_decision_reason
        if override_decision_reason is not None
        else (crowning_reason_override if crowning_reason_override is not None else decision.reason)
    )
    if promoted_id != decision.promoted_generation_id or _decision_reason != decision.reason:
        effective_decision = replace(
            decision,
            promoted_generation_id=promoted_id,
            decision=(
                TournamentDecision.PROMOTED
                if promoted_id is not None
                else TournamentDecision.REJECTED
            ),
            reason=_decision_reason,
        )
    return promoted_id, promoted_ids, override_provenance, effective_decision


@dataclass(frozen=True, slots=True)
class FieldVerdict:
    """One round's crowning after every check that may revise it.

    The strategy resolves a leader on the train slice; this value is what the
    workspace will actually commit, once the holdout confirmation, the opt-in
    integrity blocks, and any operator override have had their say.

    ``promoted_ids`` is the (possibly multi-element) promoted SET, every
    member of which is marked promoted in lineage, while ``promoted_id`` is
    the PRIMARY head that alone advances the champion pointer.
    ``effective_decision`` is the post-confirmation, post-override truth
    every durable store must describe (issue #20).  The remaining fields are
    the crowning challenger's evidence, stamped on its outcome record.
    """

    promoted_id: str | None
    promoted_ids: set[str]
    override_provenance: dict[str, dict[str, Any]]
    effective_decision: Any
    overrides: dict[str, GateOverride]
    reason_override: str | None
    holdout_block: dict[str, Any] | None
    holdout_child_scalar: float | None
    crowning_challenger_id: str | None
    crowning_challenger_train_scalar: float | None


async def resolve_field_verdict(
    field_round: FieldRound,
    candidates: CandidateField,
    decision: Any,
) -> FieldVerdict:
    """Confirm, block, or override the strategy's crowning, then record it.

    Three revisions apply in a fixed order, and each is recorded rather than
    silent:

    * **Holdout confirmation** (OVERFITTING.md §3/§4).  The structure
      resolved its leader on the TRAIN slice and ran ONE crowning
      champion-versus-survivor duel, also on train.  If that duel promoted
      and a holdout slice exists, the win must ALSO confirm on the holdout,
      through the shared Ladder-mediated machinery and the per-epoch
      ``ladder_state.json`` budget.  A released non-confirmation flips the
      crowning promote to a holdout reject and the champion stands.  An
      empty holdout — a small board, or the split disabled — means no
      holdout run and no Ladder move.
    * **Integrity blocking** (default OFF).  Diff containment on the crowned
      child's snapshot plus a gate-contradiction re-derivation against the
      crowning duel's delta, applied before anything persists and before the
      override claim below, so an explicit force-promote remains the
      operator's recorded prerogative.
    * **Operator overrides.**  The structure has settled and nothing is
      persisted yet, which is the safe point at which a force-promote or
      force-reject of ANY field candidate overrides the verdict.  An
      override may target a non-winner, the crowned leader, or several
      candidates; a one-candidate gauntlet is the degenerate case.  Only the
      claim is I/O: :func:`_apply_field_overrides` re-resolves the promoted
      set, the primary head, the provenance, and the effective decision
      purely.
    """

    from zicato.tournament.runner import confirm_crowning_holdout  # noqa: PLC0415

    crowning = await _confirm_crowning_on_holdout(
        decision=decision,
        parent_id=field_round.parent_id,
        champion_gen=candidates.champion,
        generation_for=candidates.generation,
        adapter=field_round.adapter,
        board=field_round.board,
        weights=field_round.weights,
        config=field_round.config,
        workspace_root=field_round.workspace_root,
        epoch_id=field_round.epoch_id,
        disable_drift=field_round.disable_drift,
        judge_only=field_round.judge_only,
        fast_mode=field_round.fast_mode,
        confirm_fn=confirm_crowning_holdout,
    )
    promoted_id = crowning.promoted_id
    reason_override = crowning.reason_override
    # The event records information the Ladder released. A consulted query
    # inside the noise band is charged but emits nothing because its result is
    # withheld. An exhausted Ladder also emits nothing because no query ran.
    released_confirmation = _released_holdout_confirmation(crowning.holdout_block)
    if released_confirmation is not None:
        field_round.round_log.emit(
            "holdout_released",
            {"confirmed": released_confirmation},
            {"generation_id": crowning.challenger_id},
        )

    if promoted_id is not None:
        block_reason = _integrity_block_reason(
            weights=field_round.weights,
            parent_snapshot_root=candidates.champion.snapshot_root,
            child_snapshot_root=candidates.by_id[promoted_id].snapshot_root,
            mutable_trees=_registered_mutable_trees(field_round.workspace_config),
            delta_scalar=crowning.crowning_delta_scalar,
        )
        if block_reason is not None:
            log.warning(
                "evolve: integrity block — generation %s crowning refused (%s)",
                promoted_id,
                block_reason,
            )
            promoted_id = None
            reason_override = block_reason

    overrides: dict[str, GateOverride] = claim_field_gate_overrides(
        field_round.workspace_root, [c.generation_id for c in candidates.challengers]
    )
    (
        promoted_id,
        promoted_ids,
        override_provenance,
        effective_decision,
    ) = _apply_field_overrides(
        workspace_root=field_round.workspace_root,
        decision=decision,
        promoted_id=promoted_id,
        crowning_reason_override=reason_override,
        field_overrides=overrides,
        structure=field_round.tournament_spec.structure,
    )
    # The round's terminal decision + provenance (operator overrides
    # explicit, never silent) — the post-holdout/post-override truth.
    field_round.round_log.emit(
        "decision_recorded",
        {
            "decision": str(effective_decision.decision),
            "provenance": {
                "structure": field_round.tournament_spec.structure,
                "reason": effective_decision.reason,
                "parent_generation_id": field_round.parent_id,
                "promoted_generation_id": promoted_id,
                "promoted_generation_ids": sorted(promoted_ids),
                "operator_override": bool(override_provenance),
                "operator_override_reason": next(
                    (
                        override.reason
                        for generation_id, override in overrides.items()
                        if generation_id == promoted_id or promoted_id is None
                    ),
                    "",
                ),
                "overrides": override_provenance,
            },
        },
    )
    return FieldVerdict(
        promoted_id=promoted_id,
        promoted_ids=promoted_ids,
        override_provenance=override_provenance,
        effective_decision=effective_decision,
        overrides=overrides,
        reason_override=reason_override,
        holdout_block=crowning.holdout_block,
        holdout_child_scalar=crowning.holdout_child_scalar,
        crowning_challenger_id=crowning.challenger_id,
        crowning_challenger_train_scalar=crowning.challenger_train_scalar,
    )


def _registered_mutable_trees(workspace_config: Any) -> list[str]:
    """The workspace's registered mutable-tree paths (empty when unset).

    The same config surface :func:`_ensure_baseline_snapshot` seeds from
    (``mutable_trees``, with ``source_roots`` as the older fallback key) and the
    same one the Rust supervisor reads for its out-of-band containment
    attestation — the two ends of the check share the rule surface.
    """
    raw = workspace_config.get("mutable_trees") or workspace_config.get("source_roots") or []
    return [str(t) for t in raw]


def _integrity_block_reason(
    *,
    weights: Any,
    parent_snapshot_root: Path,
    child_snapshot_root: Path,
    mutable_trees: list[str],
    delta_scalar: float | None,
) -> str | None:
    """The refusal reason when an opt-in integrity block fires, else ``None``.

    Consulted immediately before a GATE-DECIDED promotion is finalized — the
    in-band, opt-in twins of the supervisor's alarm-only integrity notary. Both
    checks default OFF (``ScoringWeights``); an explicit operator force-promote
    is never routed here (the override is recorded provenance rather than a
    silent flip, and blocking it would disable the control protocol).

    (a) **Diff containment** (``block_on_containment_violation``): every
        file outside the registered mutable trees must be byte-identical
        parent↔child (``zicato.evolve.containment`` mirrors
        ``crates/supervisor/src/diff_containment.rs``). Fail-open: an
        unreadable snapshot skips the check.
    (b) **Promotion-gate contradiction** (``block_on_gate_contradiction``):
        re-derive the gate's scalar rule ``delta_scalar <= -promote_margin``
        (``promotion_gate.rs check_row``, applied pre-persist) and refuse
        on contradiction. ``delta_scalar is None`` (no usable scalar
        evidence) skips the check — check_row's SkippedNoEvidence.
    """
    if weights.block_on_containment_violation:
        from zicato.evolve.containment import (  # noqa: PLC0415
            check_containment,
            containment_reason,
        )

        report = check_containment(parent_snapshot_root, child_snapshot_root, mutable_trees)
        if not report.contained:
            return containment_reason(report)
    if weights.block_on_gate_contradiction and delta_scalar is not None:
        margin = float(weights.promote_margin)
        if not (delta_scalar <= -margin):
            if delta_scalar > 0.0:
                detail = (
                    f"challenger regressed: loss rose by {delta_scalar:.6g} "
                    f"(a promotion needs the loss to drop by at least {margin:.6g})"
                )
            else:
                detail = (
                    f"improvement was insufficient: loss fell by only "
                    f"{-delta_scalar:.6g} (a promotion needs a drop of at "
                    f"least {margin:.6g})"
                )
            return f"gate_contradiction: recorded PROMOTE but {detail}; refused pre-persist"
    return None
