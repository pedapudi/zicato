"""Opt-in integrity blocking modes — mutation containment + gate contradiction.

Two of the supervisor's alarm-only integrity checks have opt-in IN-BAND
blocking twins (default OFF):

* ``ScoringWeights.block_on_containment_violation`` — the promoted pair's
  byte-range evidence is verified pre-finalize
  (``zicato.epoch.containment.attest_generation``, the rule
  ``crates/supervisor/src/range_containment.rs`` checks out of band): a
  ``violated`` or ``evidence_mismatch`` child is REJECTED with a clear reason
  instead of promoted-with-alarm.
* ``ScoringWeights.block_on_gate_contradiction`` — the pre-persist
  re-derivation of the gate's scalar rule
  (``delta_scalar <= -promote_margin`` — ``promotion_gate.rs check_row``
  semantics): a recorded promote the scalars do not support is refused.

Default-off byte-identity is covered by the untouched full suite (every
existing promotion test runs with both knobs at their False default).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._containment_corpus import CORPUS, materialize_case
from tests._contract_pins import deterministic_weights
from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
)
from tests._workspace_support import read_experiment_record
from zicato.core.epoch import Generation
from zicato.core.types import TournamentStructure
from zicato.epoch.containment import attest_generation, loadable_artifacts
from zicato.epoch.genstore import default_generation_store
from zicato.evolve.gate import _integrity_block_reason
from zicato.evolve.generation_phase import current_generation

_CASES = {case["name"]: case for case in json.loads(CORPUS.read_text())["cases"]}


def _pair(root: Path) -> dict[str, Any]:
    """The corpus pair ``v0 -> v1`` as the gate names it."""

    def generation(gid: str, source: str) -> Generation:
        return Generation(gid, "epoch", None, root / source, "2026-05-14T00:00:00Z")

    return {
        "workspace_root": root,
        "epoch_id": "epoch",
        "parent": generation("v0", "parent"),
        "child": generation("v1", "child"),
    }


# ---------------------------------------------------------------------------
# _integrity_block_reason — the pure decision
# ---------------------------------------------------------------------------


class TestIntegrityBlockReason:
    def test_default_off_never_blocks(self, tmp_path: Path) -> None:
        materialize_case(_CASES["adjacent-expression"], tmp_path)
        weights = deterministic_weights(promote_margin=0.01)
        assert (
            _integrity_block_reason(
                weights=weights,
                **_pair(tmp_path),
                delta_scalar=+5.0,  # a blatant contradiction — still not checked
            )
            is None
        )

    def test_gate_contradiction_blocks_when_on(self, tmp_path: Path) -> None:
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        reason = _integrity_block_reason(weights=weights, **_pair(tmp_path), delta_scalar=+0.5)
        assert reason is not None and reason.startswith("gate_contradiction:")
        assert "regressed" in reason
        # Insufficient improvement (negative but inside the margin).
        reason2 = _integrity_block_reason(weights=weights, **_pair(tmp_path), delta_scalar=-0.005)
        assert reason2 is not None and "insufficient" in reason2

    def test_gate_contradiction_supported_promote_passes(self, tmp_path: Path) -> None:
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        assert (
            _integrity_block_reason(weights=weights, **_pair(tmp_path), delta_scalar=-1.0) is None
        )

    def test_gate_contradiction_skips_without_evidence(self, tmp_path: Path) -> None:
        # ``None`` delta = no usable scalar evidence — check_row's
        # SkippedNoEvidence, fail-open.
        weights = deterministic_weights(promote_margin=0.01, block_on_gate_contradiction=True)
        assert (
            _integrity_block_reason(weights=weights, **_pair(tmp_path), delta_scalar=None) is None
        )

    @pytest.mark.parametrize(
        "case,blocked_as",
        [
            ("literal", None),
            ("adjacent-expression", "violated"),
            ("forbidden-literal", "violated"),
            ("wrong-child-hash", "evidence_mismatch"),
            ("extra-observed-file", "evidence_mismatch"),
            ("missing-manifest", None),
        ],
    )
    def test_containment_blocks_violated_and_mismatched_pairs(
        self, tmp_path: Path, case: str, blocked_as: str | None
    ) -> None:
        materialize_case(_CASES[case], tmp_path)
        weights = deterministic_weights(promote_margin=0.01, block_on_containment_violation=True)
        reason = _integrity_block_reason(weights=weights, **_pair(tmp_path), delta_scalar=-1.0)
        if blocked_as is None:
            assert reason is None
        else:
            assert reason is not None
            assert reason.startswith(f"containment_violation: {blocked_as} — ")


# ---------------------------------------------------------------------------
# End-to-end through evolve_once (rigged applier / rigged gate)
# ---------------------------------------------------------------------------


def _escape_after_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Change each child's source outside its mutation units after its evidence exists."""
    import zicato.epoch.containment as containment

    publish = containment.write_containment_manifest

    def publish_then_escape(workspace_root: Path, **kwargs: Any) -> Any:
        result = publish(workspace_root, **kwargs)
        child = kwargs["genstore"].snapshot_path(kwargs["epoch_id"], kwargs["generation_id"])
        source = child / "agent.py"
        source.write_text(source.read_text() + "ESCAPED = True\n")
        return result

    monkeypatch.setattr(containment, "write_containment_manifest", publish_then_escape)


_STORES = pytest.mark.parametrize("store", ["directory", "git"])


@_STORES
def test_containment_block_rejects_a_child_whose_source_contradicts_its_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str
) -> None:
    """Rigged escape: after the child's byte-range evidence is published, its
    source gains a line outside every mutation unit, so the evidence no longer
    binds the files. An otherwise-promotable child is REJECTED with the
    containment reason and the champion stands."""
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(promote_margin=0.01, block_on_containment_violation=True),
        generation_store=store,
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _escape_after_evidence(monkeypatch)

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("containment_violation: evidence_mismatch")
    assert "agent.py" in outcome.rejection_reason
    # The recorded champion stays v0; v1 is a dead branch.
    assert current_generation(workspace, epoch_id) == "v0"
    body = read_experiment_record(
        workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json"
    )
    assert body["outcome"]["tournament_decision"] == "rejected"
    assert body["outcome"]["rejection_reason"].startswith("containment_violation:")


@_STORES
def test_containment_block_off_promotes_with_alarm_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str
) -> None:
    """Default OFF: the identical escaped child still promotes —
    containment stays the supervisor's alarm-only concern."""
    workspace, epoch_id = bootstrap_workspace(tmp_path, generation_store=store)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _escape_after_evidence(monkeypatch)

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    assert outcome.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"


def test_gate_contradiction_block_refuses_unsupported_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rigged gate: evaluate_gate is patched to PROMOTE a regressing child
    (delta_scalar +1.0 against margin 0.01). With the knob ON the
    orchestrator re-derives the scalar rule pre-persist and refuses."""
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01,
            block_on_gate_contradiction=True,
            tournament_structure=TournamentStructure(structure="gauntlet"),
        ),
    )
    install_stub_adapter_factory(monkeypatch)
    # Child is WORSE than the parent — a promote is a contradiction.
    install_telemetry_stubs(
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

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("gate_contradiction:")
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()


def test_gate_contradiction_block_off_keeps_rigged_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default OFF: the same rigged promote persists (alarm-only parity —
    the supervisor's out-of-band scan owns the alarm)."""
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01, tournament_structure=TournamentStructure(structure="gauntlet")
        ),
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
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

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    assert outcome.tournament_decision == "promoted"


@_STORES
def test_supported_promote_passes_with_both_knobs_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str
) -> None:
    """A genuinely-supported, contained promotion is untouched by the
    blocking modes (the applier's edit stays inside its mutation unit; the
    real gate's delta clears the margin)."""
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01,
            block_on_containment_violation=True,
            block_on_gate_contradiction=True,
        ),
        generation_store=store,
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    assert outcome.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    genstore = default_generation_store(workspace)
    attestation = attest_generation(
        workspace,
        epoch_id=epoch_id,
        parent_generation_id="v0",
        generation_id="v1",
        parent_root=genstore.snapshot_path(epoch_id, "v0"),
        child_root=genstore.snapshot_path(epoch_id, "v1"),
    )
    assert attestation.status == "contained", attestation


@pytest.mark.integration
@_STORES
def test_regression_gated_round_leaves_the_canonical_tree_contained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str
) -> None:
    """With the regression gate on, the child's suite runs and writes bytecode
    and caches, yet the child's canonical tree holds no artifact and its
    byte-range evidence still attests contained."""
    for name in [key for key in os.environ if key.startswith("PYTEST_")]:
        monkeypatch.delenv(name)
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01,
            block_on_containment_violation=True,
            regression_gate_enabled=True,
            regression_test_command=(sys.executable, "-m", "pytest", "tests/", "-q"),
        ),
        generation_store=store,
        extra_source={"tests/test_greeting.py": "def test_truth():\n    assert True\n"},
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "promoted", outcome.rejection_reason
    genstore = default_generation_store(workspace)
    child = genstore.snapshot_path(epoch_id, "v1")
    assert (child / "tests" / "test_greeting.py").is_file()
    assert loadable_artifacts(child) == []
    assert not any(path.name in {"__pycache__", ".pytest_cache"} for path in child.rglob("*"))
    attestation = attest_generation(
        workspace,
        epoch_id=epoch_id,
        parent_generation_id="v0",
        generation_id="v1",
        parent_root=genstore.snapshot_path(epoch_id, "v0"),
        child_root=child,
    )
    assert attestation.status == "contained", attestation
