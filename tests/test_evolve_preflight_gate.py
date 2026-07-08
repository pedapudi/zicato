"""The achievable-signal pre-flight gate wired into evolve start (issue #84).

The contract pre-flight (:mod:`zicato.epoch.preflight`) measures whether a
contract's achievable signal clears its own A/A noise floor. Part 2 wires it
into the evolve entry path as DEFAULT-ON WARNING + opt-in HARD gate:

* ``runtime.preflight_gate == "warn"`` (the default) — measure once per epoch
  at evolve start, LOUDLY warn on a below-floor / saturated verdict, proceed.
* ``"refuse"`` — additionally HARD-STOP the run before spending rounds when
  the verdict is ``refuse``.
* ``"off"`` — skip the measurement entirely (byte-identical to pre-#84).

These tests drive the real ``evolve_once`` / ``evolve_n_rounds`` with a stub
adapter and a CANNED pre-flight verdict (no live LLM, no real degraded-probe
run) so the gate wiring is asserted deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from zicato.epoch.preflight import PreflightReport
from zicato.tournament.calibration import NoiseFloor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_preflight_gate(workspace: Path, mode: str) -> None:
    """Write ``runtime.preflight_gate`` into the workspace config."""
    cfg_path = workspace / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("runtime", {})["preflight_gate"] = mode
    cfg_path.write_text(json.dumps(cfg))


def _canned(verdict: str, *, signal: float, floor_max: float) -> tuple[PreflightReport, NoiseFloor]:
    report = PreflightReport(
        epoch_id="e",
        generation_id="v0",
        verdict=verdict,
        noise_floor_max_abs_delta=floor_max,
        noise_floor_runs=5,
        champion_scalars=(1.0, 1.0),
        degraded_scalar=1.0 + signal,
        signal=signal,
        degraded_mutation_id="m0",
        degraded_mutation_kind="span",
        degraded_file="agent.py",
        measured_at="2026-01-01T00:00:00+00:00",
    )
    floor = NoiseFloor(
        generation_id="v0",
        epoch_id="e",
        runs=5,
        scalars=(1.0, 1.0),
        max_abs_delta=floor_max,
        delta_std=0.0,
        measured_at="2026-01-01T00:00:00+00:00",
    )
    return report, floor


def _install_canned_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str,
    signal: float,
    floor_max: float,
    calls: list[Any],
) -> None:
    """Monkeypatch ``run_contract_preflight`` to a canned verdict (no probe run)."""

    async def _fake(**kwargs: Any) -> tuple[PreflightReport, NoiseFloor]:
        calls.append(kwargs)
        return _canned(verdict, signal=signal, floor_max=floor_max)

    monkeypatch.setattr("zicato.epoch.preflight.run_contract_preflight", _fake)


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    gate: str,
    verdict: str,
    signal: float = 0.0,
    floor_max: float = 0.0,
) -> tuple[Path, str, list[Any]]:
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_preflight_gate(workspace, gate)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    calls: list[Any] = []
    _install_canned_preflight(
        monkeypatch, verdict=verdict, signal=signal, floor_max=floor_max, calls=calls
    )
    return workspace, epoch_id, calls


def _run_once(monkeypatch: pytest.MonkeyPatch, workspace: Path, epoch_id: str) -> Any:
    from zicato.orchestrator import evolve_once

    return asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_refuse_verdict_below_floor_warns_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default (warn) mode: a below-floor verdict WARNS loudly but proceeds."""
    workspace, epoch_id, calls = _prepare(
        monkeypatch, tmp_path, gate="warn", verdict="refuse", signal=0.0, floor_max=0.0
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        outcome = _run_once(monkeypatch, workspace, epoch_id)

    assert outcome is not None  # the round ran to a verdict — not blocked
    assert len(calls) == 1  # the pre-flight WAS measured (default-on)
    msgs = [r.getMessage() for r in caplog.records if r.name == "zicato.orchestrator"]
    assert any("CONTRACT PRE-FLIGHT REFUSE" in m for m in msgs), msgs


def test_refuse_verdict_hard_gate_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opt-in refuse mode: a below-floor verdict HARD-STOPS evolve_once."""
    from zicato.epoch.preflight import PreflightRefusedError

    workspace, epoch_id, _ = _prepare(
        monkeypatch, tmp_path, gate="refuse", verdict="refuse", signal=0.0, floor_max=0.0
    )
    with pytest.raises(PreflightRefusedError):
        _run_once(monkeypatch, workspace, epoch_id)


def test_refuse_mode_stops_evolve_n_rounds_before_spending_rounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """evolve_n_rounds catches the hard refuse and reports a clean stop reason."""
    workspace, epoch_id, _ = _prepare(
        monkeypatch, tmp_path, gate="refuse", verdict="refuse", signal=0.0, floor_max=0.0
    )
    from zicato.evolve.loop import evolve_n_rounds

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()] * 3),
            stop_reason_out=stop_reason,
        )
    )
    # No round ran — the run refused BEFORE spending rounds.
    assert outcomes == []
    assert stop_reason == ["preflight_refused"]


def test_ok_verdict_proceeds_without_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real-signal (ok) contract: no pre-flight warning, run proceeds."""
    workspace, epoch_id, calls = _prepare(
        monkeypatch, tmp_path, gate="refuse", verdict="ok", signal=1.0, floor_max=0.1
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        outcome = _run_once(monkeypatch, workspace, epoch_id)

    assert outcome is not None
    assert len(calls) == 1
    assert not any(
        "CONTRACT PRE-FLIGHT" in r.getMessage() for r in caplog.records
    ), "an ok verdict must not warn about the noise floor"


def test_off_mode_skips_the_measurement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``preflight_gate='off'`` skips the pre-flight entirely (byte-identical)."""
    workspace, epoch_id, calls = _prepare(
        monkeypatch, tmp_path, gate="off", verdict="refuse", signal=0.0, floor_max=0.0
    )
    outcome = _run_once(monkeypatch, workspace, epoch_id)
    assert outcome is not None
    assert calls == []  # the measurement was never invoked
