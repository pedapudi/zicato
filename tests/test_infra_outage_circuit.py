"""Tests for the endpoint-outage circuit (WS-H).

Today N consecutive infra-aborted runs burn the round: the aborted runs
score worst-case, the gate rejects the child, and the loop immediately
re-proposes into the same dead endpoint. The circuit —
``RuntimeConfig.infra_abort_round_threshold`` (``0`` = OFF, the default) —
defers such a round instead: the tournament verdict is discarded, the
experiment persists UN-OUTCOMED (the exact shape the conservative
crash-resume reconciles), and the evolve loop backs off (exponential,
capped) before the next round.

Coverage:

* a rigged always-infra-abort ``_run_single`` ⇒ the round defers: no
  outcome / lineage / journal write, the ``infra_outage`` health WARNING
  lands in the round report, and ``prepare_resume`` classifies the
  leftover generation cleanly (discard-no-progress with zero cached
  units; resume-in-place with one);
* threshold off (the default) ⇒ the same rig settles exactly as today —
  an ordinary worst-case ``rejected`` round, fully finalized;
* the loop backs off exponentially (capped), skips both stop-policies for
  deferrals, and re-reconciles via ``prepare_resume`` before the next
  round;
* the knobs thread from the workspace ``runtime`` block through
  ``make_runtime_config`` and validate their bounds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_orchestrator import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.core.types import DriftCount, LossProfile
from zicato.orchestrator import DEFERRED_INFRA_DECISION, EvolveRoundOutcome, evolve_once
from zicato.runtime.resume import prepare_resume

# ---------------------------------------------------------------------------
# Rig: an always-infra-abort _run_single
# ---------------------------------------------------------------------------


def _install_infra_abort_run_single(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every board unit come back as an INFRA abort (a worker crash).

    Layered on top of ``_install_telemetry_stubs`` (which stubs the real
    subprocess ``_run_single``): the profile carries the runner's
    ``nonzero_exit`` abort cause — an :func:`is_infra_abort_cause` class,
    never a genuine budget exhaustion — and the worst-case
    not-completed scoring shape the real reducer would produce.
    """
    import zicato.tournament.runner as _runner_mod

    async def _infra_abort_run_single(
        *,
        adapter: Any,
        generation: Any,
        entry: Any,
        weights: Any,
        config: Any,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, side, match_id
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=1.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=10.0,
            pass_fail=None,
            abort_cause="nonzero_exit:1",
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _infra_abort_run_single)


def _set_runtime_block(workspace: Path, runtime: dict[str, Any]) -> None:
    """Merge a ``runtime`` block into the bootstrapped workspace config."""
    cfg_path = workspace / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["runtime"] = {**cfg.get("runtime", {}), **runtime}
    cfg_path.write_text(json.dumps(cfg))


def _rig_outage_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, threshold: int | None
) -> tuple[Path, str]:
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _install_infra_abort_run_single(monkeypatch)
    if threshold is not None:
        _set_runtime_block(workspace, {"infra_abort_round_threshold": threshold})
    return workspace, epoch_id


# ---------------------------------------------------------------------------
# evolve_once — the circuit itself
# ---------------------------------------------------------------------------


def test_all_infra_aborted_round_defers_un_outcomed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _rig_outage_workspace(monkeypatch, tmp_path, threshold=1)

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == DEFERRED_INFRA_DECISION
    assert "deferred_infra" in outcome.rejection_reason
    assert outcome.proposed_generation_id == "v1"

    # The experiment PERSISTS un-outcomed — the resume-compatible shape.
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"] is None

    # Nothing was finalized: no journal line, no champion advance, and the
    # fast-mode score caches were NOT poisoned with the aborted aggregates.
    journal = workspace / "epochs" / epoch_id / "journal.md"
    assert not journal.exists() or "v1" not in journal.read_text()
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists() or marker.read_text().strip() == "v0"
    assert not (v1_dir / "gen_score.json").exists()

    # The round health report carries the infra_outage WARNING.
    report = json.loads((workspace / "epochs" / epoch_id / "health" / "round_1.json").read_text())
    codes = {f["code"] for f in report["findings"]}
    assert "infra_outage" in codes
    outage = next(f for f in report["findings"] if f["code"] == "infra_outage")
    assert outage["severity"] == "warning"
    # Both sides of the single-entry duel aborted; threshold was 1.
    assert outage["detail"]["infra_aborted_runs"] == 2
    assert outage["detail"]["infra_abort_round_threshold"] == 1


def test_deferred_round_reconciles_cleanly_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a deferral, ``prepare_resume`` classifies the leftover
    generation with the standard conservative discipline, and a healed
    endpoint settles the re-run round normally."""
    workspace, epoch_id = _rig_outage_workspace(monkeypatch, tmp_path, threshold=1)

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    assert outcome.tournament_decision == DEFERRED_INFRA_DECISION

    # Infra aborts are never persisted to the unit cache, so a full-outage
    # round leaves ZERO cached units: the conservative reconciliation
    # discards it cleanly (never corrupts, never resumes garbage).
    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_no_progress"
    assert not (workspace / "epochs" / epoch_id / "generations" / "v1").exists()

    # Heal the endpoint (restore the stub's healthy _run_single) and run the
    # next round: it re-proposes v1 fresh and settles normally.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    healed = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    assert healed.tournament_decision == "promoted"
    assert healed.proposed_generation_id == "v1"


def test_partial_outage_leaves_resume_in_place_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deferral after SOME units completed classifies resume-in-place —
    the cached units are worth keeping and the cache HITs them on re-run."""
    workspace, epoch_id = _rig_outage_workspace(monkeypatch, tmp_path, threshold=1)

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    assert outcome.tournament_decision == DEFERRED_INFRA_DECISION

    # Simulate one completed unit having landed before the outage (a real
    # partial outage persists the completed units' loss.json; the rigged
    # all-abort run cached none, so write the marker the classifier reads).
    runs_dir = workspace / "epochs" / epoch_id / "generations" / "v1" / "runs" / "entry_a"
    runs_dir.mkdir(parents=True)
    (runs_dir / "loss.json").write_text("{}")

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "resume_tournament"
    assert plan.resumes_in_place
    assert plan.resume_generation_id == "v1"


def test_threshold_off_settles_exactly_as_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the circuit off (the default), the same rigged outage burns the
    round the historical way: worst-case scoring, an ordinary finalized
    ``rejected`` outcome — the un-opted-in path is untouched."""
    workspace, epoch_id = _rig_outage_workspace(monkeypatch, tmp_path, threshold=None)

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )

    assert outcome.tournament_decision == "rejected"
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"] is not None
    assert body["outcome"]["tournament_decision"] == "rejected"
    # No infra_outage finding without the circuit.
    report_path = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    if report_path.exists():
        codes = {f["code"] for f in json.loads(report_path.read_text())["findings"]}
        assert "infra_outage" not in codes


# ---------------------------------------------------------------------------
# evolve_n_rounds — backoff + reconciliation between rounds
# ---------------------------------------------------------------------------


def _deferred_outcome(round_idx: int) -> EvolveRoundOutcome:
    return EvolveRoundOutcome(
        parent_generation_id="v0",
        proposed_generation_id=f"v{round_idx + 1}",
        tournament_decision=DEFERRED_INFRA_DECISION,
        rejection_reason="deferred_infra: rigged",
        parent_scalar=0.0,
        child_scalar=0.0,
        delta_scalar=0.0,
    )


def _promoted_outcome(round_idx: int) -> EvolveRoundOutcome:
    return EvolveRoundOutcome(
        parent_generation_id=f"v{round_idx}",
        proposed_generation_id=f"v{round_idx + 1}",
        tournament_decision="promoted",
        rejection_reason="",
        parent_scalar=1.0,
        child_scalar=0.5,
        delta_scalar=-0.5,
    )


def test_loop_backs_off_exponentially_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import zicato.evolve.gauntlet as gauntlet
    import zicato.evolve.loop as loop_mod
    import zicato.orchestrator as orch

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_runtime_block(workspace, {"infra_backoff_base_s": 0.01, "infra_backoff_cap_s": 0.02})

    async def _permissive_aux(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    decisions = ["defer", "defer", "defer", "promote", "defer"]
    calls: list[int] = []

    async def _mock_evolve_once(*, round_index: int = 0, **_kwargs: Any) -> EvolveRoundOutcome:
        idx = len(calls)
        calls.append(round_index)
        if decisions[idx] == "defer":
            return _deferred_outcome(round_index)
        return _promoted_outcome(round_index)

    monkeypatch.setattr(gauntlet, "evolve_once", _mock_evolve_once)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(loop_mod, "_sleep_for_backoff", _record_sleep)

    reconciles: list[str] = []
    real_prepare_resume = loop_mod.prepare_resume

    def _recording_prepare_resume(root: Path, epoch: str) -> Any:
        plan = real_prepare_resume(root, epoch)
        reconciles.append(plan.classification)
        return plan

    monkeypatch.setattr(loop_mod, "prepare_resume", _recording_prepare_resume)

    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            workspace_root=workspace,
            rounds=5,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_permissive_aux,
            # Three deferrals in a row must NOT trip this breaker — a
            # deferral is evidence about the endpoint, not the stream.
            max_consecutive_rejections=2,
        )
    )

    # Every round ran (no early stop from the rejection breaker).
    assert len(outcomes) == 5
    assert [o.tournament_decision for o in outcomes] == [
        DEFERRED_INFRA_DECISION,
        DEFERRED_INFRA_DECISION,
        DEFERRED_INFRA_DECISION,
        "promoted",
        DEFERRED_INFRA_DECISION,
    ]
    # Exponential from base, capped, and RESET by the settled round; no
    # sleep after the final round (nothing left to back off for).
    assert sleeps == [0.01, 0.02, 0.02]
    # The loop re-reconciled after every deferral: 4 deferral reconciles on
    # top of the one standard loop-start reconcile.
    assert len(reconciles) == 5


# ---------------------------------------------------------------------------
# Knob threading + validation
# ---------------------------------------------------------------------------


def test_runtime_factory_threads_the_circuit_knobs() -> None:
    from zicato.runtime_factory import make_runtime_config

    async def _a(s: str, u: str, m: str) -> str:
        return ""

    async def _b(s: str, u: str, m: str) -> str:
        return ""

    cfg = make_runtime_config(
        {
            "runtime": {
                "infra_abort_round_threshold": 3,
                "infra_backoff_base_s": 5,
                "infra_backoff_cap_s": 60,
            }
        },
        workspace_root=Path("/tmp/ws"),
        harness_call_llm=_a,
        auxiliary_call_llm=_b,
    )
    assert cfg.infra_abort_round_threshold == 3
    assert cfg.infra_backoff_base_s == 5.0
    assert cfg.infra_backoff_cap_s == 60.0

    default_cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=Path("/tmp/ws"),
        harness_call_llm=_a,
        auxiliary_call_llm=_b,
    )
    assert default_cfg.infra_abort_round_threshold == 0  # circuit OFF
    assert default_cfg.infra_backoff_base_s == 30.0
    assert default_cfg.infra_backoff_cap_s == 480.0


def test_runtime_config_validates_circuit_bounds() -> None:
    from zicato.core.runtime import RuntimeConfig

    async def _a(s: str, u: str, m: str) -> str:
        return ""

    async def _b(s: str, u: str, m: str) -> str:
        return ""

    with pytest.raises(ValueError, match="infra_abort_round_threshold"):
        RuntimeConfig(
            instance_id="t",
            workspace_root=Path("/tmp/ws"),
            harness_call_llm=_a,
            auxiliary_call_llm=_b,
            infra_abort_round_threshold=-1,
        )
    with pytest.raises(ValueError, match="infra_backoff"):
        RuntimeConfig(
            instance_id="t",
            workspace_root=Path("/tmp/ws"),
            harness_call_llm=_a,
            auxiliary_call_llm=_b,
            infra_backoff_base_s=-0.5,
        )


def test_detect_infra_outage_finding_shape() -> None:
    from zicato.health.diagnostics import detect_infra_outage

    assert detect_infra_outage(None) == []
    findings = detect_infra_outage((4, 2))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "infra_outage"
    assert finding.severity == "warning"
    assert finding.detail["infra_aborted_runs"] == 4
    assert finding.detail["infra_abort_round_threshold"] == 2
