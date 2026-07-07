"""A/A noise-floor calibration — unit, integration, and CLI surface tests.

The pure spread math and the margin/floor comparators are exercised with
synthetic scalars; the full measurement runs against target_0's
DETERMINISTIC planted-defect adapter, whose A/A floor is exactly ``0.0``
(every draw of the same generation scores identically) — the known-answer
case for the mechanism. A seeded-noise adapter mode (built separately)
will exercise the non-zero-floor case end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.health.diagnostics import detect_margin_below_noise_floor
from zicato.tournament.calibration import (
    delta_spread,
    margin_below_floor,
    measure_noise_floor,
)
from zicato_examples.target_0_convergence import mocks as t0_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"

ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}

#: v0 seeds three defect tokens: scalar 3.0 drift + (1 - 2/5) pass = 3.6.
EXPECTED_V0 = 3.6


# ---------------------------------------------------------------------------
# Pure spread math (synthetic scalars — the function-level noise case)
# ---------------------------------------------------------------------------


def test_delta_spread_zero_for_identical_draws() -> None:
    assert delta_spread([3.6, 3.6, 3.6, 3.6]) == (0.0, 0.0)


def test_delta_spread_known_values() -> None:
    max_abs, std = delta_spread([1.0, 2.0, 3.0])
    assert max_abs == 2.0
    # Population std of [1,2,3] is sqrt(2/3); the A/A delta std is sqrt(2)x.
    assert math.isclose(std, math.sqrt(2.0 * 2.0 / 3.0))


def test_delta_spread_degenerate_inputs() -> None:
    assert delta_spread([]) == (0.0, 0.0)
    assert delta_spread([1.5]) == (0.0, 0.0)


def test_margin_below_floor_comparisons() -> None:
    floor = {"max_abs_delta": 0.5}
    assert margin_below_floor(0.01, floor) is True
    assert margin_below_floor(0.5, floor) is False  # equal clears (not below)
    assert margin_below_floor(0.9, floor) is False
    # No measurement / malformed record ⇒ nothing to compare against.
    assert margin_below_floor(0.01, None) is False
    assert margin_below_floor(0.01, {"max_abs_delta": "junk"}) is False


# ---------------------------------------------------------------------------
# Health detector
# ---------------------------------------------------------------------------


def test_detector_silent_without_floor_or_when_margin_clears() -> None:
    assert detect_margin_below_noise_floor(None, 0.01, evidence_gate_on=False) == []
    floor = {"max_abs_delta": 0.5, "runs": 5, "generation_id": "v0"}
    assert detect_margin_below_noise_floor(floor, 0.6, evidence_gate_on=False) == []
    assert detect_margin_below_noise_floor(floor, None, evidence_gate_on=False) == []


def test_detector_warns_when_gate_off_and_informs_when_gate_on() -> None:
    floor = {"max_abs_delta": 0.5, "runs": 5, "generation_id": "v0"}
    (off,) = detect_margin_below_noise_floor(floor, 0.01, evidence_gate_on=False)
    assert off.code == "margin_below_noise_floor"
    assert off.severity == "warning"
    assert off.detail["evidence_gate_on"] is False

    (on,) = detect_margin_below_noise_floor(floor, 0.01, evidence_gate_on=True)
    assert on.severity == "info"
    assert on.detail["evidence_gate_on"] is True


# ---------------------------------------------------------------------------
# Integration — the deterministic adapter measures a floor of exactly 0.0
# ---------------------------------------------------------------------------


def _bootstrap(tmp_path: Path, extra_config: dict | None = None) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "adapter": ADAPTER_BLOCK,
                "mutable_trees": [str(AGENT_DIR)],
                **(extra_config or {}),
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Calibration brief\n- Remove defect tokens.\n")
    weights = _scoring_from_dict(json.loads(SCORING_PATH.read_text()))
    cfg = new_epoch(
        workspace,
        name="t0-noise-floor",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _run_rounds(workspace: Path, epoch_id: str, rounds: int = 1) -> list:
    from zicato.evolve.loop import evolve_n_rounds

    t0_mocks.reset()
    return asyncio.run(
        evolve_n_rounds(
            rounds=rounds,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )


def test_measure_noise_floor_deterministic_adapter_is_zero(tmp_path: Path) -> None:
    """K fresh draws of the same generation, through the real board-unit
    workers — the deterministic target scores identically every draw."""
    workspace, epoch_id = _bootstrap(tmp_path)
    _run_rounds(workspace, epoch_id)  # seeds v0 + promotes v1

    from zicato import adapter_factory, runtime_factory, workspace_loader
    from zicato.core.types import Generation
    from zicato.orchestrator import _resolve_current_generation, _snapshot_root

    workspace_config = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    champion_id = _resolve_current_generation(workspace, epoch_id)
    champion = Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)

    floor = asyncio.run(
        measure_noise_floor(
            adapter=adapter,
            generation=champion,
            board=workspace_loader.load_current_board(workspace),
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace,
            epoch_id=epoch_id,
            runs=3,
        )
    )
    assert floor.runs == 3
    assert floor.max_abs_delta == 0.0
    assert floor.delta_std == 0.0
    assert len(set(floor.scalars)) == 1  # every draw identical

    # Persist + round-trip through the epoch record (additive field).
    from zicato.epoch.lifecycle import set_epoch_noise_floor

    set_epoch_noise_floor(workspace, epoch_id, floor.to_json())
    reloaded = load_epoch(workspace, epoch_id)
    assert reloaded.noise_floor is not None
    assert reloaded.noise_floor["max_abs_delta"] == 0.0
    assert reloaded.noise_floor["generation_id"] == champion_id
    # A runtime measurement, never a contract input: the hash is untouched.
    assert reloaded.contract_hash == epoch_cfg.contract_hash


def test_epoch_open_calibration_step_persists_floor(tmp_path: Path) -> None:
    """The opt-in workspace knob measures the floor at epoch open (first
    evolve round) and every later round short-circuits on the record."""
    workspace, epoch_id = _bootstrap(tmp_path, extra_config={"calibrate_noise_floor": 3})
    _run_rounds(workspace, epoch_id)

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["runs"] == 3
    assert cfg.noise_floor["max_abs_delta"] == 0.0
    # Measured on the epoch's seed champion, before the round's duel.
    assert cfg.noise_floor["generation_id"] == "v0"
    assert cfg.noise_floor["scalars"] == [EXPECTED_V0] * 3


def test_board_audit_cli_measures_and_persists(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace, epoch_id = _bootstrap(tmp_path)
    _run_rounds(workspace, epoch_id)

    runner = CliRunner()
    result = runner.invoke(
        build_cli_root(),
        [
            "board",
            "audit",
            "--workspace",
            str(workspace),
            "--runs",
            "3",
            "--harness-call-llm",
            "zicato_examples.target_0_convergence.mocks:harness_llm",
            "--auxiliary-call-llm",
            "zicato_examples.target_0_convergence.mocks:aux_llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "A/A noise floor" in result.output
    assert "max |delta|:    0" in result.output
    assert "promote_margin clears the measured floor" in result.output

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["max_abs_delta"] == 0.0
