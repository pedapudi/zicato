"""Opt-in integrity blocking modes — diff containment + gate contradiction.

Two of the supervisor's alarm-only integrity checks gained opt-in
IN-BAND blocking twins (default OFF):

* ``ScoringWeights.block_on_containment_violation`` — the Python-side
  pre-finalize diff-containment check (``zicato.evolve.containment``,
  mirroring ``crates/supervisor/src/diff_containment.rs``): a promoted
  child whose diff escaped the registered mutable trees is REJECTED with
  a clear reason instead of promoted-with-alarm.
* ``ScoringWeights.block_on_gate_contradiction`` — the pre-persist
  re-derivation of the gate's scalar rule
  (``delta_scalar <= -promote_margin`` — ``promotion_gate.rs check_row``
  semantics): a recorded promote the scalars do not support is refused.

Default-off byte-identity is covered by the untouched full suite (every
existing promotion test runs with both knobs at their False default).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from tests._orchestrator_harness import (
    _bootstrap_workspace,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
    run_evolve_once,
)
from zicato.evolve.containment import (
    check_containment,
    containment_reason,
    mutable_basenames,
)
from zicato.evolve.gate import _integrity_block_reason

# ---------------------------------------------------------------------------
# check_containment — the supervisor's rule surface, mirrored
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestCheckContainment:
    def test_clean_pair_is_contained(self, tmp_path: Path) -> None:
        parent, child = tmp_path / "p", tmp_path / "c"
        for root in (parent, child):
            _write(root, "agent/policy.py", "A = 1\n")
            _write(root, "support/util.py", "B = 2\n")
        report = check_containment(parent, child, ["/reg/agent"])
        assert report.contained
        assert report.violations == ()
        assert report.skipped_reason is None

    def test_in_bounds_change_is_fine(self, tmp_path: Path) -> None:
        parent, child = tmp_path / "p", tmp_path / "c"
        _write(parent, "agent/policy.py", "A = 1\n")
        _write(child, "agent/policy.py", "A = 2\n")  # the mutation surface
        report = check_containment(parent, child, ["/reg/agent"])
        assert report.contained

    def test_out_of_bounds_change_added_deleted(self, tmp_path: Path) -> None:
        parent, child = tmp_path / "p", tmp_path / "c"
        _write(parent, "agent/policy.py", "A = 1\n")
        _write(child, "agent/policy.py", "A = 2\n")
        _write(parent, "support/util.py", "B = 2\n")
        _write(child, "support/util.py", "B = 3\n")  # changed out-of-bounds
        _write(child, "support/new.py", "C = 1\n")  # added out-of-bounds
        _write(parent, "support/gone.py", "D = 1\n")  # deleted out-of-bounds
        report = check_containment(parent, child, ["/reg/agent"])
        assert not report.contained
        by_path = {v.path: v.kind for v in report.violations}
        assert by_path == {
            "support/util.py": "changed",
            "support/new.py": "added",
            "support/gone.py": "deleted",
        }
        reason = containment_reason(report)
        assert reason.startswith("containment_violation:")
        assert "support/util.py" in reason

    def test_empty_mutable_trees_is_trivially_contained(self, tmp_path: Path) -> None:
        parent, child = tmp_path / "p", tmp_path / "c"
        _write(parent, "anything.py", "A = 1\n")
        _write(child, "anything.py", "A = 2\n")
        assert check_containment(parent, child, []).contained

    def test_missing_root_is_a_fail_open_skip(self, tmp_path: Path) -> None:
        parent = tmp_path / "p"
        _write(parent, "agent/policy.py", "A = 1\n")
        report = check_containment(parent, tmp_path / "missing", ["/reg/agent"])
        assert report.contained  # fail-open — never a false quarantine
        assert report.skipped_reason is not None
        report2 = check_containment(tmp_path / "missing", parent, ["/reg/agent"])
        assert report2.contained
        assert report2.skipped_reason is not None

    def test_mutable_basenames_semantics(self) -> None:
        assert mutable_basenames(["/a/b/agent", "support/"]) == frozenset({"agent", "support"})
        assert mutable_basenames([]) == frozenset()


# ---------------------------------------------------------------------------
# _integrity_block_reason — the pure decision
# ---------------------------------------------------------------------------


class TestIntegrityBlockReason:
    def test_default_off_never_blocks(self, tmp_path: Path) -> None:
        weights = deterministic_weights(promote_margin=0.01)
        assert (
            _integrity_block_reason(
                weights=weights,
                parent_snapshot_root=tmp_path / "p",
                child_snapshot_root=tmp_path / "c",
                mutable_trees=["/reg/agent"],
                delta_scalar=+5.0,  # a blatant contradiction — still not checked
            )
            is None
        )

    def test_gate_contradiction_blocks_when_on(self, tmp_path: Path) -> None:
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        reason = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=tmp_path / "p",
            child_snapshot_root=tmp_path / "c",
            mutable_trees=[],
            delta_scalar=+0.5,
        )
        assert reason is not None and reason.startswith("gate_contradiction:")
        assert "regressed" in reason
        # Insufficient improvement (negative but inside the margin).
        reason2 = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=tmp_path / "p",
            child_snapshot_root=tmp_path / "c",
            mutable_trees=[],
            delta_scalar=-0.005,
        )
        assert reason2 is not None and "insufficient" in reason2

    def test_gate_contradiction_supported_promote_passes(self, tmp_path: Path) -> None:
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        assert (
            _integrity_block_reason(
                weights=weights,
                parent_snapshot_root=tmp_path / "p",
                child_snapshot_root=tmp_path / "c",
                mutable_trees=[],
                delta_scalar=-1.0,
            )
            is None
        )

    def test_gate_contradiction_skips_without_evidence(self, tmp_path: Path) -> None:
        # ``None`` delta = no usable scalar evidence — check_row's
        # SkippedNoEvidence, fail-open.
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        assert (
            _integrity_block_reason(
                weights=weights,
                parent_snapshot_root=tmp_path / "p",
                child_snapshot_root=tmp_path / "c",
                mutable_trees=[],
                delta_scalar=None,
            )
            is None
        )

    def test_containment_blocks_when_on(self, tmp_path: Path) -> None:
        parent, child = tmp_path / "p", tmp_path / "c"
        _write(parent, "agent/policy.py", "A = 1\n")
        _write(child, "agent/policy.py", "A = 2\n")
        _write(parent, "support/util.py", "B = 1\n")
        _write(child, "support/util.py", "B = 2\n")
        weights = deterministic_weights(promote_margin=0.01, block_on_containment_violation=True)
        reason = _integrity_block_reason(
            weights=weights,
            parent_snapshot_root=parent,
            child_snapshot_root=child,
            mutable_trees=["/reg/agent"],
            delta_scalar=-1.0,
        )
        assert reason is not None and reason.startswith("containment_violation:")


# ---------------------------------------------------------------------------
# End-to-end through evolve_once (rigged applier / rigged gate)
# ---------------------------------------------------------------------------


def _set_scoring_flag(workspace: Path, epoch_id: str, **flags: Any) -> None:
    """Flip integrity knobs on the epoch's frozen scoring.json in place.

    The epoch is already minted; rewriting scoring.json is the test's rig
    (a real operator would roll the epoch — these knobs are contract
    fields — but the orchestrator reads the file at round start either
    way).
    """
    scoring_path = workspace / "epochs" / epoch_id / "scoring.json"
    body = json.loads(scoring_path.read_text())
    body.update(flags)
    scoring_path.write_text(json.dumps(body))


def _declare_mutable_trees(workspace: Path, trees: list[str]) -> None:
    config_path = workspace / "config.json"
    body = json.loads(config_path.read_text())
    body["mutable_trees"] = trees
    config_path.write_text(json.dumps(body))


def test_containment_block_rejects_out_of_bounds_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rigged applier surface: the child's whole diff is OUTSIDE the
    registered mutable trees (the fixture snapshot keeps ``agent.py`` at
    the snapshot root while the registered tree basename is ``elsewhere``),
    so an otherwise-promotable child is REJECTED with the containment
    reason and the champion stands."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _declare_mutable_trees(workspace, [str(tmp_path / "elsewhere")])
    _set_scoring_flag(workspace, epoch_id, block_on_containment_violation=True)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("containment_violation:")
    assert "agent.py" in outcome.rejection_reason
    # The champion pointer did NOT advance (the fixture writes the marker
    # only on a crowning); v1 is a dead branch.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()
    body = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )
    assert body["outcome"]["tournament_decision"] == "rejected"
    assert body["outcome"]["rejection_reason"].startswith("containment_violation:")


def test_containment_block_off_promotes_with_alarm_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default OFF: the identical out-of-bounds child still promotes —
    containment stays the supervisor's alarm-only concern."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _declare_mutable_trees(workspace, [str(tmp_path / "elsewhere")])
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )
    assert outcome.tournament_decision == "promoted"


def test_gate_contradiction_block_refuses_unsupported_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rigged gate: evaluate_gate is patched to PROMOTE a regressing child
    (delta_scalar +1.0 against margin 0.01). With the knob ON the
    orchestrator re-derives the scalar rule pre-persist and refuses."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_scoring_flag(workspace, epoch_id, block_on_gate_contradiction=True)
    _install_stub_adapter_factory(monkeypatch)
    # Child is WORSE than the parent — a promote is a contradiction.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 2.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    import zicato.tournament.runner as _runner_mod
    from zicato.core.types import TournamentDecision
    from zicato.tournament.gate import GateOutcome

    def _rigged_gate(parent_agg: Any, child_agg: Any, weights: Any, **_kw: Any) -> GateOutcome:
        del weights
        return GateOutcome(
            decision=TournamentDecision.PROMOTED,
            reason="",
            delta_scalar=float(child_agg.get("scalar", 0.0)) - float(parent_agg.get("scalar", 0.0)),
            delta_pass_rate=0.0,
        )

    monkeypatch.setattr(_runner_mod, "evaluate_gate", _rigged_gate)

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("gate_contradiction:")
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()


def test_gate_contradiction_block_off_keeps_rigged_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default OFF: the same rigged promote persists (alarm-only parity —
    the supervisor's out-of-band scan owns the alarm)."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 2.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    import zicato.tournament.runner as _runner_mod
    from zicato.core.types import TournamentDecision
    from zicato.tournament.gate import GateOutcome

    def _rigged_gate(parent_agg: Any, child_agg: Any, weights: Any, **_kw: Any) -> GateOutcome:
        del weights
        return GateOutcome(
            decision=TournamentDecision.PROMOTED,
            reason="",
            delta_scalar=float(child_agg.get("scalar", 0.0)) - float(parent_agg.get("scalar", 0.0)),
            delta_pass_rate=0.0,
        )

    monkeypatch.setattr(_runner_mod, "evaluate_gate", _rigged_gate)

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )
    assert outcome.tournament_decision == "promoted"


def test_supported_promote_passes_with_both_knobs_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A genuinely-supported, in-bounds promotion is untouched by the
    blocking modes (no mutable_trees registered ⇒ trivially contained;
    the real gate's delta clears the margin)."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_scoring_flag(
        workspace,
        epoch_id,
        block_on_containment_violation=True,
        block_on_gate_contradiction=True,
    )
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(
        workspace, epoch_id, _make_aux_responder([_valid_proposer_response()])
    )
    assert outcome.tournament_decision == "promoted"
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v1"
