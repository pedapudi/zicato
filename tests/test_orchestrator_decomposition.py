"""Unit tests for the orchestrator's extracted pure round-decision helpers.

The Phase-4 decomposition split the multi-challenger driver's inline
decisions into unit-testable helpers on :mod:`zicato.orchestrator`:

* :func:`~zicato.evolve.propose_apply._mint_challenger_field` — the field-diversity
  accept / soft-reject verdict, separated from its persistence I/O;
* :func:`~zicato.evolve.gate._apply_field_overrides` — the operator-override
  head re-resolution (promoted set, primary head, provenance, effective
  decision);
* :func:`~zicato.evolve.gate._confirm_crowning_on_holdout` — the crowning
  holdout confirmation's decision shape (with the runner's confirm callable
  injected).

These branches were previously reachable only through the full evolve e2e
paths (diversity soft-reject, holdout demotion, override re-resolution);
the tests here pin their exact semantics directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import zicato.evolve.gate as gate
import zicato.evolve.propose_apply as propose_apply
from zicato.core.types import Experiment, Generation, HypothesisSpec, TournamentDecision
from zicato.runtime.control_consumer import GateOverride
from zicato.selection.strategy import MatchupResult, SelectionDecision
from zicato.tournament.gate import GateOutcome


def _experiment(gen_id: str, modulating: tuple[str, ...], core_idea: str) -> Experiment:
    return Experiment(
        id=f"exp_e1_{gen_id}",
        epoch_id="e1",
        generation_id=gen_id,
        parent_generation_id="v0",
        proposed_at="2026-01-01T00:00:00+00:00",
        hypothesis=HypothesisSpec(
            core_idea=core_idea,
            modulating=modulating,
            why="test",
            expected_drift_movements=(),
            expected_pass_rate_delta="0.0",
            risks="",
        ),
        patches=(),
        outcome=None,
    )


# ---------------------------------------------------------------------------
# _mint_challenger_field — the field-diversity accept / soft-reject decision
# ---------------------------------------------------------------------------


class TestMintChallengerField:
    def test_accepts_a_distinct_challenger(self) -> None:
        exp = _experiment("v1", ("m1",), "tighten the summary")
        decision = propose_apply._mint_challenger_field(exp, [], [], None)
        assert decision.action == "accept"

    def test_rejects_exact_inflight_duplicate(self) -> None:
        exp = _experiment("v2", ("m1", "m2"), "Tighten The Summary")
        # Same modulating id-set (order-insensitive) + case/whitespace-
        # normalized core idea as a minted sibling ⇒ duplicate.
        siblings = [(frozenset({"m2", "m1"}), "tighten the summary")]
        decision = propose_apply._mint_challenger_field(exp, siblings, [], None)
        assert decision.action == "reject_duplicate"

    def test_same_ids_different_idea_is_not_a_duplicate(self) -> None:
        exp = _experiment("v2", ("m1",), "a genuinely different idea")
        siblings = [(frozenset({"m1"}), "tighten the summary")]
        decision = propose_apply._mint_challenger_field(exp, siblings, [], None)
        assert decision.action == "accept"

    def test_empty_modulating_set_never_duplicates(self) -> None:
        exp = _experiment("v2", (), "tighten the summary")
        siblings = [(frozenset(), "tighten the summary")]
        decision = propose_apply._mint_challenger_field(exp, siblings, [], None)
        assert decision.action == "accept"

    def test_overlap_soft_reject_fires_above_tolerance(self) -> None:
        exp = _experiment("v3", ("m1", "m2"), "idea three")
        accepted = [frozenset({"m1", "m2", "m3"})]  # Jaccard 2/3 ≈ 0.667
        decision = propose_apply._mint_challenger_field(exp, [], accepted, 0.5)
        assert decision.action == "reject_overlap"
        assert decision.overlap_peer_index == 0
        assert abs(decision.overlap - 2 / 3) < 1e-9

    def test_overlap_at_tolerance_is_kept(self) -> None:
        # Strictly-greater-than semantics: overlap == tolerance is kept.
        exp = _experiment("v3", ("m1", "m2"), "idea three")
        accepted = [frozenset({"m1", "m2", "m3"})]
        decision = propose_apply._mint_challenger_field(exp, [], accepted, 2 / 3)
        assert decision.action == "accept"

    def test_overlap_check_skipped_without_tolerance(self) -> None:
        exp = _experiment("v3", ("m1",), "idea three")
        accepted = [frozenset({"m1"})]  # identical set — overlap 1.0
        decision = propose_apply._mint_challenger_field(exp, [], accepted, None)
        assert decision.action == "accept"

    def test_empty_candidate_set_never_overlap_rejected(self) -> None:
        exp = _experiment("v3", (), "idea three")
        accepted = [frozenset({"m1"})]
        decision = propose_apply._mint_challenger_field(exp, [], accepted, 0.0)
        assert decision.action == "accept"

    def test_duplicate_takes_precedence_over_overlap(self) -> None:
        exp = _experiment("v3", ("m1",), "same idea")
        siblings = [(frozenset({"m1"}), "same idea")]
        accepted = [frozenset({"m1"})]
        decision = propose_apply._mint_challenger_field(exp, siblings, accepted, 0.0)
        assert decision.action == "reject_duplicate"


# ---------------------------------------------------------------------------
# _apply_field_overrides — operator-override head re-resolution
# ---------------------------------------------------------------------------


def _matchup(left: str, right: str, left_scalar: float, right_scalar: float) -> MatchupResult:
    outcome = GateOutcome(
        decision=TournamentDecision.PROMOTED,
        reason="",
        delta_scalar=right_scalar - left_scalar,
        delta_pass_rate=0.0,
    )
    return MatchupResult(
        matchup_id=f"{left}:{right}",
        left_id=left,
        right_id=right,
        left_agg={"scalar": left_scalar},
        right_agg={"scalar": right_scalar},
        outcome=outcome,
    )


def _decision(
    promoted: str | None,
    *,
    reason: str = "",
    matchups: tuple[MatchupResult, ...] = (),
    crowning: str = "",
) -> SelectionDecision:
    return SelectionDecision(
        promoted_generation_id=promoted,
        decision=(
            TournamentDecision.PROMOTED if promoted is not None else TournamentDecision.REJECTED
        ),
        reason=reason,
        matchups=matchups,
        crowning_matchup_id=crowning,
    )


class TestApplyFieldOverrides:
    def test_no_overrides_is_identity(self, tmp_path: Any) -> None:
        decision = _decision("v2")
        promoted_id, promoted_ids, provenance, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id="v2",
            crowning_reason_override=None,
            field_overrides={},
            structure="swiss",
        )
        assert promoted_id == "v2"
        assert promoted_ids == {"v2"}
        assert provenance == {}
        assert effective is decision  # untouched object — byte-identical path

    def test_no_overrides_no_promotion(self, tmp_path: Any) -> None:
        decision = _decision(None, reason="gate: margin not cleared")
        promoted_id, promoted_ids, provenance, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id=None,
            crowning_reason_override=None,
            field_overrides={},
            structure="swiss",
        )
        assert promoted_id is None
        assert promoted_ids == set()
        assert effective is decision

    def test_holdout_demotion_rewrites_effective_decision(self, tmp_path: Any) -> None:
        # No operator override, but the holdout flipped the crown: the
        # effective decision must describe the post-confirmation truth.
        decision = _decision("v2", reason="promoted: gate cleared")
        promoted_id, promoted_ids, _prov, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id=None,  # demoted by the holdout before overrides
            crowning_reason_override="holdout_not_confirmed: worse on holdout",
            field_overrides={},
            structure="racing",
        )
        assert promoted_id is None
        assert promoted_ids == set()
        assert effective.decision == "rejected"
        assert effective.promoted_generation_id is None
        assert effective.reason == "holdout_not_confirmed: worse on holdout"

    def test_force_reject_of_leader_champion_stands(self, tmp_path: Any) -> None:
        decision = _decision("v2")
        overrides = {
            "v2": GateOverride(decision="rejected", generation_id="v2", reason="known flake")
        }
        promoted_id, promoted_ids, provenance, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id="v2",
            crowning_reason_override=None,
            field_overrides=overrides,
            structure="swiss",
        )
        assert promoted_id is None
        assert promoted_ids == set()
        assert provenance["v2"]["action"] == "reject"
        assert effective.decision == "rejected"
        assert effective.reason == "operator override: known flake"

    def test_force_promote_nonwinner_head_is_lowest_scalar(self, tmp_path: Any) -> None:
        # Leader v2 force-rejected, v3 + v4 force-promoted: the primary head
        # is the lowest-scalar promoted candidate (v4 at 1.0 beats v3 at 2.0).
        matchups = (
            _matchup("v0", "v3", 3.0, 2.0),
            _matchup("v0", "v4", 3.0, 1.0),
        )
        decision = _decision("v2", matchups=matchups)
        overrides = {
            "v2": GateOverride(decision="rejected", generation_id="v2", reason="cut it"),
            "v3": GateOverride(decision="promoted", generation_id="v3", reason="ship both"),
            "v4": GateOverride(decision="promoted", generation_id="v4", reason="ship both"),
        }
        promoted_id, promoted_ids, provenance, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id="v2",
            crowning_reason_override=None,
            field_overrides=overrides,
            structure="double_elim",
        )
        assert promoted_ids == {"v3", "v4"}
        assert promoted_id == "v4"
        assert effective.decision == "promoted"
        assert effective.promoted_generation_id == "v4"
        assert effective.reason == "operator override: ship both"
        assert set(provenance) == {"v2", "v3", "v4"}

    def test_leader_survives_extra_promote(self, tmp_path: Any) -> None:
        # An extra force-promote of a non-winner keeps the crowned leader as
        # the primary head.
        matchups = (_matchup("v0", "v3", 3.0, 0.5),)
        decision = _decision("v2", matchups=matchups)
        overrides = {
            "v3": GateOverride(decision="promoted", generation_id="v3", reason="also good"),
        }
        promoted_id, promoted_ids, _prov, effective = gate._apply_field_overrides(
            workspace_root=tmp_path,
            decision=decision,
            promoted_id="v2",
            crowning_reason_override=None,
            field_overrides=overrides,
            structure="swiss",
        )
        assert promoted_id == "v2"
        assert promoted_ids == {"v2", "v3"}
        # The leader was gate-decided (no override on it), so the reason is
        # the decision's own — the effective decision still names v2.
        assert effective.promoted_generation_id == "v2"


# ---------------------------------------------------------------------------
# _confirm_crowning_on_holdout — the crowning holdout confirmation decision
# ---------------------------------------------------------------------------


def _gen(gid: str) -> Generation:
    from pathlib import Path

    return Generation(
        id=gid,
        epoch_id="e1",
        parent_id=None,
        snapshot_root=Path("/nonexistent") / gid,
        created_at="2026-01-01T00:00:00+00:00",
    )


class TestConfirmCrowningOnHoldout:
    def _run(
        self,
        decision: SelectionDecision,
        confirm_fn: Any,
        tmp_path: Any,
    ) -> gate._CrowningHoldout:
        gens = {gid: _gen(gid) for gid in ("v0", "v2")}
        return asyncio.run(
            gate._confirm_crowning_on_holdout(
                decision=decision,
                parent_id="v0",
                champion_gen=gens["v0"],
                generation_for=lambda gid: gens[gid],
                adapter=object(),
                board=[],
                weights=object(),
                config=object(),
                workspace_root=tmp_path,
                epoch_id="e1",
                disable_drift=(),
                judge_only=False,
                fast_mode=False,
                confirm_fn=confirm_fn,
            )
        )

    def test_pass_through_without_crowning_duel(self, tmp_path: Any) -> None:
        calls: list[Any] = []

        async def _confirm(**kwargs: Any) -> Any:
            calls.append(kwargs)
            raise AssertionError("must not be called")

        decision = _decision("v2")  # no crowning_matchup_id
        result = self._run(decision, _confirm, tmp_path)
        assert result.promoted_id == "v2"
        assert result.reason_override is None
        assert result.challenger_id is None
        assert calls == []

    def test_pass_through_on_rejection(self, tmp_path: Any) -> None:
        async def _confirm(**kwargs: Any) -> Any:
            raise AssertionError("must not be called")

        matchups = (_matchup("v0", "v2", 3.0, 1.0),)
        decision = _decision(None, matchups=matchups, crowning="v0:v2")
        result = self._run(decision, _confirm, tmp_path)
        assert result.promoted_id is None
        assert result.challenger_id is None

    def test_confirmed_crowning_keeps_promotion(self, tmp_path: Any) -> None:
        async def _confirm(**kwargs: Any) -> Any:
            outcome = GateOutcome(
                decision=TournamentDecision.PROMOTED,
                reason="",
                delta_scalar=-2.0,
                delta_pass_rate=0.0,
            )
            return outcome, {"consulted": True}, 1.25

        matchups = (_matchup("v0", "v2", 3.0, 1.0),)
        decision = _decision("v2", matchups=matchups, crowning="v0:v2")
        result = self._run(decision, _confirm, tmp_path)
        assert result.promoted_id == "v2"
        assert result.reason_override is None
        assert result.holdout_block == {"consulted": True}
        assert result.holdout_child_scalar == 1.25
        assert result.challenger_id == "v2"
        assert result.challenger_train_scalar == 1.0

    def test_holdout_demotion_flips_crown(self, tmp_path: Any) -> None:
        async def _confirm(**kwargs: Any) -> Any:
            outcome = GateOutcome(
                decision=TournamentDecision.REJECTED,
                reason="holdout_not_confirmed: challenger worse on holdout",
                delta_scalar=0.5,
                delta_pass_rate=0.0,
            )
            return outcome, {"consulted": True}, 3.5

        matchups = (_matchup("v0", "v2", 3.0, 1.0),)
        decision = _decision("v2", matchups=matchups, crowning="v0:v2")
        result = self._run(decision, _confirm, tmp_path)
        assert result.promoted_id is None
        assert result.reason_override == "holdout_not_confirmed: challenger worse on holdout"
        assert result.challenger_id == "v2"
        assert result.challenger_train_scalar == 1.0
        assert result.holdout_child_scalar == 3.5

    def test_champion_on_the_right_is_resolved_defensively(self, tmp_path: Any) -> None:
        seen: dict[str, Any] = {}

        async def _confirm(**kwargs: Any) -> Any:
            seen.update(kwargs)
            outcome = GateOutcome(
                decision=TournamentDecision.PROMOTED,
                reason="",
                delta_scalar=-2.0,
                delta_pass_rate=0.0,
            )
            return outcome, None, None

        # Champion (v0) seeded on the RIGHT of the crowning duel.
        matchups = (_matchup("v2", "v0", 1.0, 3.0),)
        decision = _decision("v2", matchups=matchups, crowning="v2:v0")
        result = self._run(decision, _confirm, tmp_path)
        assert result.challenger_id == "v2"
        assert result.challenger_train_scalar == 1.0
        # The champion side's train aggregate is v0's (scalar 3.0).
        assert seen["train_parent_agg"] == {"scalar": 3.0}
        assert seen["train_child_agg"] == {"scalar": 1.0}
