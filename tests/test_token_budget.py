"""Tests for the per-round token budget (WS-H).

``RuntimeConfig.max_tokens_per_round`` (``0`` = OFF, the default) bounds one
round's OPPORTUNISTIC token spend (the ``cost:tokens_spent`` capture): the
orchestrator mints a fresh ``RoundTokenLedger`` per round; every fresh board
unit folds its ``LossProfile.tokens_spent`` into the tally at the one
choke point every unit routes through, and once the budget is spent the
schedulers stop LAUNCHING further board units / replicate slots — never
mid-unit — and the round settles with what it has (un-run units record the
same budget-exceeded losses a matchup-deadline trip synthesizes).

Coverage:

* rigged token-heavy runs ⇒ the round clips: only the first board unit
  runs live, the remaining units persist budget-exceeded losses, and the
  ``round_token_clipped`` health WARNING lands in the round report;
* budget off (the default) ⇒ byte-identical scheduling — every unit runs,
  no ledger consulted, no finding;
* the replicate loop stops scheduling FURTHER slots on a spent ledger and
  averages the completed replicates as-is;
* the ledger's tally/latch semantics, the factory threading, the bound
  validation, and the detector shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from tests._orchestrator_harness import (
    install_stub_adapter_factory,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
    valid_proposer_response,
)
from zicato.core.runtime import RoundTokenLedger, RuntimeConfig
from zicato.core.types import DriftCount, LossProfile
from zicato.epoch.lifecycle import new_epoch

# Grab the REAL reducer helper before any test masks zicato.telemetry in
# sys.modules — the unit cache persists a skipped unit through the writer
# (the clip test asserts on those persisted budget-exceeded losses).
from zicato.telemetry.reducer import (  # isort: skip
    write_loss_profile as _real_write_loss_profile,
)

# ---------------------------------------------------------------------------
# Workspace bootstrap — a MULTI-entry board so a clip can land mid-round
# ---------------------------------------------------------------------------


def _bootstrap_multi_entry_workspace(
    tmp_path: Path, *, entries: int = 3, runtime: dict[str, Any] | None = None
) -> tuple[Path, str]:
    """The test_orchestrator bootstrap with N board entries + a runtime block.

    ``runtime.parallelism`` defaults to 1 here so the board units run
    sequentially and the between-unit budget check observes each unit's
    tally deterministically.

    ``preflight_gate`` defaults to ``"off"``: these tests count the board
    units / tokens a ROUND spends, and the default-on achievable-signal
    pre-flight (issue #84) runs extra champion A/A units that would perturb
    that accounting — an orthogonal probe this fixture opts out of.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    runtime_block = {"parallelism": 1, "preflight_gate": "off", **(runtime or {})}
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-14T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": {"kind": "stub"},
                "runtime": runtime_block,
            }
        )
    )

    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"entry_{i}",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 60,
                    "input": f"hello {i}",
                }
            )
            for i in range(1, entries + 1)
        )
        + "\n"
    )
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        brief_source=brief_src,
        weights=deterministic_weights(promote_margin=0.01),
        auto_close_previous=False,
    )

    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source for tests."""\n'
        "\n"
        '# zicato:mutable id="greeting"\n'
        'GREETING = "hello"\n'
    )
    return workspace, cfg.id


def _install_token_heavy_run_single(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str]], *, tokens_per_run: int = 1000
) -> None:
    """Every live board unit run reports ``tokens_per_run`` tokens spent."""
    import zicato.tournament.runner as _runner_mod

    async def _token_heavy_run_single(
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
        calls.append((generation.id, entry.id))
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=2.0 if generation.id == "v0" else 1.0,
            pass_fail=True,
            tokens_spent=tokens_per_run,
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _token_heavy_run_single)


def _run_one_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    max_tokens_per_round: int | None,
) -> tuple[Path, str, Any, list[tuple[str, str]]]:
    runtime: dict[str, Any] = {}
    if max_tokens_per_round is not None:
        runtime["max_tokens_per_round"] = max_tokens_per_round
    workspace, epoch_id = _bootstrap_multi_entry_workspace(tmp_path, runtime=runtime)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    calls: list[tuple[str, str]] = []
    _install_token_heavy_run_single(monkeypatch, calls)
    # The telemetry stub masks the real reducer; reattach the real loss
    # writer so a skipped unit's budget-exceeded loss genuinely persists
    # (the on-disk shape the clip assertions — and resume — read).
    import sys

    stub_reducer = sys.modules["zicato.telemetry.reducer"]
    stub_reducer.write_loss_profile = _real_write_loss_profile  # type: ignore[attr-defined]

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([valid_proposer_response()]))
    return workspace, epoch_id, outcome, calls


# ---------------------------------------------------------------------------
# evolve_once — clip + finding, and off ⇒ byte-identical
# ---------------------------------------------------------------------------


def test_token_heavy_round_clips_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 3 board entries x (parent + child) x 1000 tokens each = 6000 for a
    # full round; a 1500 budget is spent after the FIRST unit's pair.
    workspace, epoch_id, outcome, calls = _run_one_round(
        monkeypatch, tmp_path, max_tokens_per_round=1500
    )

    # Exactly ONE board unit ran live (both its sides — a clip never splits
    # a pair); the remaining two units were never launched.
    assert len(calls) == 2
    assert {entry for _, entry in calls} == {"entry_1"}

    # The un-run units persisted the SAME budget-exceeded losses a
    # matchup-deadline trip records, on both sides.
    for gen in ("v0", "v1"):
        for entry in ("entry_2", "entry_3"):
            loss_path = (
                workspace / "epochs" / epoch_id / "generations" / gen / "runs" / entry / "loss.json"
            )
            body = json.loads(loss_path.read_text())
            assert body["abort_cause"] == "budget_exhausted"

    # The round still SETTLES (with what it has) — a clip is not a crash.
    assert outcome.tournament_decision in ("promoted", "rejected")

    # The round health report carries the token-clip WARNING.
    report = json.loads((workspace / "epochs" / epoch_id / "health" / "round_1.json").read_text())
    outage = next(f for f in report["findings"] if f["code"] == "round_token_clipped")
    assert outage["severity"] == "warning"
    assert outage["detail"]["max_tokens_per_round"] == 1500
    assert outage["detail"]["tokens_spent"] >= 1500


def test_budget_off_runs_every_unit_no_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id, outcome, calls = _run_one_round(
        monkeypatch, tmp_path, max_tokens_per_round=None
    )

    # Every unit ran live on both sides — no ledger, no skips.
    assert len(calls) == 6
    assert outcome.tournament_decision == "promoted"
    report_path = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    if report_path.exists():
        codes = {f["code"] for f in json.loads(report_path.read_text())["findings"]}
        assert "round_token_clipped" not in codes


# ---------------------------------------------------------------------------
# Replicate loop — stop scheduling FURTHER slots, settle with what completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replicate_slots_stop_on_spent_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    import zicato.tournament.scheduling as sched
    from zicato.core.types import Generation

    ledger = RoundTokenLedger(500)

    async def _a(s: str, u: str, m: str) -> str:
        return ""

    async def _b(s: str, u: str, m: str) -> str:
        return ""

    config = RuntimeConfig(
        instance_id="t",
        workspace_root=Path("/tmp/ws"),
        harness_call_llm=_a,
        auxiliary_call_llm=_b,
        max_tokens_per_round=500,
        token_ledger=ledger,
    )
    slots: list[int] = []

    async def _fake_full(**kwargs: Any) -> tuple[dict, dict]:
        slots.append(kwargs["replicate_index"])
        ledger.add(1000)  # slot 0 alone spends the whole budget
        return {}, {}

    monkeypatch.setattr(sched, "_run_board_units_full", _fake_full)
    gen = Generation(
        id="v0",
        epoch_id="e1",
        parent_id=None,
        snapshot_root=Path("/tmp/snap"),
        created_at="2026-07-01T00:00:00Z",
    )
    left, right, mode, _prov = await sched._run_replicated(
        adapter=None,
        left_gen=gen,
        right_gen=gen,
        board=[],
        weights=deterministic_weights(),
        config=config,
        workspace_root=Path("/tmp/ws"),
        epoch_id="e1",
        replicates=3,
        fast=True,
    )
    # Slot 0 ran; slots 1 and 2 were never scheduled; the ledger latched.
    assert slots == [0]
    assert ledger.clipped
    assert left == {} and right == {}


# ---------------------------------------------------------------------------
# Ledger semantics + knob threading + detector
# ---------------------------------------------------------------------------


def test_round_token_ledger_semantics() -> None:
    ledger = RoundTokenLedger(100)
    assert not ledger.exhausted
    ledger.add(60)
    assert not ledger.check_and_clip()
    assert not ledger.clipped
    ledger.add(-5)  # negative spends never shrink the tally
    ledger.add(40)
    assert ledger.exhausted
    assert ledger.check_and_clip()
    assert ledger.clipped  # latched
    # A zero budget never exhausts (the OFF sentinel shape).
    off = RoundTokenLedger(0)
    off.add(10_000)
    assert not off.exhausted
    assert not off.check_and_clip()


def test_runtime_factory_threads_max_tokens_per_round() -> None:
    from zicato.runtime_factory import make_runtime_config

    async def _a(s: str, u: str, m: str) -> str:
        return ""

    async def _b(s: str, u: str, m: str) -> str:
        return ""

    cfg = make_runtime_config(
        {"runtime": {"max_tokens_per_round": 250_000}},
        workspace_root=Path("/tmp/ws"),
        harness_call_llm=_a,
        auxiliary_call_llm=_b,
    )
    assert cfg.max_tokens_per_round == 250_000
    assert cfg.token_ledger is None  # the ledger is minted per round, never from config

    default_cfg = make_runtime_config(
        {"runtime": {}},
        workspace_root=Path("/tmp/ws"),
        harness_call_llm=_a,
        auxiliary_call_llm=_b,
    )
    assert default_cfg.max_tokens_per_round == 0


def test_runtime_config_validates_token_budget_bound() -> None:
    async def _a(s: str, u: str, m: str) -> str:
        return ""

    async def _b(s: str, u: str, m: str) -> str:
        return ""

    with pytest.raises(ValueError, match="max_tokens_per_round"):
        RuntimeConfig(
            instance_id="t",
            workspace_root=Path("/tmp/ws"),
            harness_call_llm=_a,
            auxiliary_call_llm=_b,
            max_tokens_per_round=-1,
        )


def test_detect_token_budget_clip_finding_shape() -> None:
    from zicato.health.diagnostics import detect_token_budget_clip

    assert detect_token_budget_clip(None) == []
    findings = detect_token_budget_clip((180_000, 150_000))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "round_token_clipped"
    assert finding.severity == "warning"
    assert finding.detail["tokens_spent"] == 180_000
    assert finding.detail["max_tokens_per_round"] == 150_000
