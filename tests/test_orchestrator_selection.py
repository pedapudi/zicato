"""Tests for the orchestrator's SelectionStrategy wiring.

Proves the gauntlet decision helper reproduces the historical
promote-on-gate behaviour from a single TournamentResult — the
back-compat baseline — and that a non-gauntlet structure routes through
its strategy without re-deciding the duel.
"""

from __future__ import annotations

from pathlib import Path

from zicato.core.types import TournamentStructure
from zicato.evolve.gate import _gauntlet_decision_from_result
from zicato.tournament.gate import GateOutcome
from zicato.tournament.runner import TournamentResult


def _tournament_result(
    *, parent_scalar: float, child_scalar: float, decision: str, reason: str = ""
) -> TournamentResult:
    return TournamentResult(
        parent_generation_id="v0",
        child_generation_id="v1",
        parent_agg={"scalar": parent_scalar, "drift_loss_mean": parent_scalar},
        child_agg={"scalar": child_scalar, "drift_loss_mean": child_scalar},
        outcome=GateOutcome(
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            delta_scalar=child_scalar - parent_scalar,
            delta_pass_rate=0.0,
        ),
        per_entry_losses={},
    )


def test_gauntlet_decision_promotes_on_gate(tmp_path: Path) -> None:
    spec = TournamentStructure(structure="gauntlet")
    result = _tournament_result(parent_scalar=1.0, child_scalar=0.5, decision="promoted")
    dec = _gauntlet_decision_from_result(spec, "v0", "v1", tmp_path / "snap_v1", result)
    assert dec.decision == "promoted"
    assert dec.promoted_generation_id == "v1"
    # The verdict mirrors the gate's — no re-decision.
    assert dec.matchups[0].outcome.decision == "promoted"


def test_gauntlet_decision_rejects_when_gate_rejects(tmp_path: Path) -> None:
    spec = TournamentStructure(structure="gauntlet")
    result = _tournament_result(
        parent_scalar=0.5, child_scalar=1.0, decision="rejected", reason="challenger regressed"
    )
    dec = _gauntlet_decision_from_result(spec, "v0", "v1", tmp_path / "snap_v1", result)
    assert dec.decision == "rejected"
    assert dec.promoted_generation_id is None
    assert dec.reason == "challenger regressed"


def test_gauntlet_decision_passes_deferred_through(tmp_path: Path) -> None:
    spec = TournamentStructure(structure="gauntlet")
    result = _tournament_result(parent_scalar=0.5, child_scalar=0.5, decision="deferred")
    dec = _gauntlet_decision_from_result(spec, "v0", "v1", tmp_path / "snap_v1", result)
    # A deferred gate verdict flows through; the orchestrator maps it to
    # "rejected" for loop bookkeeping (not re-decided here).
    assert dec.decision == "deferred"
    assert dec.promoted_generation_id is None


def test_single_elim_field_size_one_degrades_to_gauntlet_decision(tmp_path: Path) -> None:
    # A structure with one challenger collapses to the gauntlet's single
    # full-board duel; the orchestrator helper feeds it the one result.
    spec = TournamentStructure(structure="single_elim", params={"field_size": 1})
    result = _tournament_result(parent_scalar=1.0, child_scalar=0.4, decision="promoted")
    dec = _gauntlet_decision_from_result(spec, "v0", "v1", tmp_path / "snap_v1", result)
    assert dec.promoted_generation_id == "v1"
