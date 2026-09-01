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

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.health.diagnostics import detect_margin_below_noise_floor
from zicato.tournament.calibration import (
    delta_spread,
    margin_below_floor,
    measure_noise_floor,
)
from zicato_examples.target_0_convergence import mocks as t0_mocks

# Every unit here is target_0, whose adapter reads a generation as TEXT,
# and none of these tests is about the process boundary — so they run
# through the worker entry in-process (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("inline_worker")

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
                "generation_source_backend": "git",
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


def test_run_count_may_not_walk_out_of_the_reserved_calibration_block() -> None:
    """Draw ``j`` caches at ``CALIBRATION_REPLICATE_BASE + j``.

    A run count wider than the block would squat the contract pre-flight's
    range in both directions: a later ``board preflight`` reads these clean
    A/A draws as its own cached degraded probes, and every reader of the
    calibration band reads the pre-flight's degraded probes as champion
    behaviour. The mirror of the pre-flight's own probe-sample guard.
    """
    import pytest

    from zicato.tournament.calibration import CALIBRATION_REPLICATE_SPAN

    def _measure(runs: int) -> None:
        asyncio.run(
            measure_noise_floor(
                adapter=None,
                generation=None,  # type: ignore[arg-type]
                board=[],
                weights=None,  # type: ignore[arg-type]
                config=None,  # type: ignore[arg-type]
                workspace_root=Path("."),
                epoch_id="e0",
                runs=runs,
            )
        )

    with pytest.raises(ValueError, match="reserved replicate block"):
        _measure(CALIBRATION_REPLICATE_SPAN + 1)
    # The existing lower bound is unmoved, and the widest in-block count is
    # refused by neither guard.
    with pytest.raises(ValueError, match="at least 2 runs"):
        _measure(1)


def test_measure_noise_floor_deterministic_adapter_is_zero(tmp_path: Path) -> None:
    """K fresh draws of the same generation, through the real board-unit
    workers — the deterministic target scores identically every draw."""
    workspace, epoch_id = _bootstrap(tmp_path)
    _run_rounds(workspace, epoch_id)  # seeds v0 + promotes v1

    from zicato import adapter_factory, runtime_factory, workspace_loader
    from zicato.core.types import Generation
    from zicato.evolve.generation_phase import current_generation, snapshot_root

    workspace_config = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    champion_id = current_generation(workspace, epoch_id)
    champion = Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snapshot_root(workspace, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)

    progress: list[tuple[int, int]] = []
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
            on_draw=lambda done, total: progress.append((done, total)),
        )
    )
    # Progress is reported once per SETTLED draw, so the count an operator
    # reads is draws completed — never draws started.
    assert progress == [(1, 3), (2, 3), (3, 3)]
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


# ---------------------------------------------------------------------------
# Legibility — what the loop REPORTS while the measurement runs (issue #175)
# ---------------------------------------------------------------------------


class _RecordingBeater:
    """A :class:`HeartbeatBeater` stand-in recording the phases stamped on it."""

    def __init__(self) -> None:
        self.phases: list[str] = []

    def update(self, **fields: object) -> None:
        if fields.get("phase") is not None:
            self.phases.append(str(fields["phase"]))

    def bump_now(self) -> None:
        pass


def _calibrate(
    monkeypatch,
    *,
    workspace_config: dict,
    measured: object,
    noise_floor: object = None,
    board_size: int = 2,
    round_index: int = 4,
) -> _RecordingBeater:
    """Run the epoch-open calibration step against a stubbed measurement.

    ``measured`` is either a :class:`NoiseFloor` the fake measurement returns
    (reporting one ``on_draw`` per draw) or an exception it raises.
    """
    import zicato.epoch.lifecycle as lifecycle
    import zicato.tournament.calibration as calibration
    from zicato.evolve.round_prepare import _maybe_calibrate_noise_floor

    async def _fake_measure(*, runs: int, on_draw=None, **_kw: object) -> object:
        for draw in range(runs):
            if on_draw is not None:
                on_draw(draw + 1, runs)
        if isinstance(measured, Exception):
            raise measured
        return measured

    monkeypatch.setattr(calibration, "measure_noise_floor", _fake_measure)
    monkeypatch.setattr(lifecycle, "set_epoch_noise_floor", lambda *a, **k: None)

    beater = _RecordingBeater()
    asyncio.run(
        _maybe_calibrate_noise_floor(
            workspace_root=Path("."),
            epoch_id="e0",
            epoch_cfg=type("_Cfg", (), {"noise_floor": noise_floor})(),
            workspace_config=workspace_config,
            adapter=None,
            parent_gen=None,
            board=[None] * board_size,
            weights=None,
            config=None,
            disable_drift=(),
            judge_only=False,
            beater=beater,  # type: ignore[arg-type]
            round_index=round_index,
        )
    )
    return beater


def _floor(runs: int) -> object:
    from zicato.tournament.calibration import NoiseFloor

    return NoiseFloor(
        generation_id="v0",
        epoch_id="e0",
        runs=runs,
        scalars=(1.0,) * runs,
        max_abs_delta=0.0,
        delta_std=0.0,
        measured_at="2026-08-17T00:00:00Z",
    )


def test_calibration_owns_the_phase_and_counts_its_draws(monkeypatch) -> None:
    """The measurement stamps its OWN phase with live draw progress, then
    hands the round back its phase — a working calibration used to be
    indistinguishable from a round 0 that had hung."""
    beater = _calibrate(
        monkeypatch,
        workspace_config={"calibrate_noise_floor": 3},
        measured=_floor(3),
    )
    assert beater.phases == [
        "evolve_once:calibrating_noise_floor:0/3",
        "evolve_once:calibrating_noise_floor:1/3",
        "evolve_once:calibrating_noise_floor:2/3",
        "evolve_once:calibrating_noise_floor:3/3",
        "evolve_once:round_4",
    ]


def test_a_calibrating_phase_reads_as_active_work() -> None:
    """No segment of the calibration phase is an at-rest token, so a
    workspace mid-measurement reads ACTIVE rather than settled."""
    from zicato.query.runtime_view import is_active_phase
    from zicato.tournament.calibration import CALIBRATION_PHASE

    assert is_active_phase(CALIBRATION_PHASE)
    assert is_active_phase(f"{CALIBRATION_PHASE}:7/18")


def test_a_failed_calibration_still_hands_the_round_its_phase_back(monkeypatch) -> None:
    """The measurement is best-effort: a failure must not leave the heartbeat
    parked on a measurement that is no longer running."""
    beater = _calibrate(
        monkeypatch,
        workspace_config={"calibrate_noise_floor": 2},
        measured=RuntimeError("endpoint outage"),
    )
    assert beater.phases[0] == "evolve_once:calibrating_noise_floor:0/2"
    assert beater.phases[-1] == "evolve_once:round_4"


def test_a_skipped_calibration_touches_no_phase(monkeypatch) -> None:
    """Opted out, misconfigured, or already measured: the round's own phase
    stands untouched — byte-identical to the behaviour before the step
    reported itself."""
    for workspace_config, floor in (
        ({}, None),
        ({"calibrate_noise_floor": 0}, None),
        ({"calibrate_noise_floor": "three"}, None),
        ({"calibrate_noise_floor": 1}, None),
        ({"calibrate_noise_floor": 3}, {"max_abs_delta": 0.0}),
    ):
        beater = _calibrate(
            monkeypatch,
            workspace_config=workspace_config,
            measured=_floor(3),
            noise_floor=floor,
        )
        assert beater.phases == [], (workspace_config, floor)


def test_the_calibration_cost_is_named_before_the_first_draw(monkeypatch, caplog) -> None:
    """K draws x N board entries is knowable up front; the operator should not
    have to infer the shape from loss files landing on disk."""
    import logging

    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        _calibrate(
            monkeypatch,
            workspace_config={"calibrate_noise_floor": 3},
            measured=_floor(3),
            board_size=6,
        )
    cost = next(m for m in caplog.messages if "board-entry runs" in m)
    assert "3 draws x 6 board entries = 18 board-entry runs" in cost
    assert "serially" in cost
