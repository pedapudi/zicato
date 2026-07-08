"""Contract pre-flight — unit, integration, CLI, and epoch-open hook tests.

The pure verdict math and the synthetic-degradation rules are exercised
with synthetic values; the full measurement runs against target_0's
planted-defect adapters:

* the DETERMINISTIC adapter measures a floor of exactly ``0.0`` and a
  strictly positive achievable signal → verdict ``ok``;
* a no-expectation variant of the board saturates (the degraded tree
  emits the same number of drift frames as the champion, and no
  predicate can tell them apart) → the ``warn`` verdict — the historical
  ``1.000000`` null-run signature;
* the seeded-noise adapter at high sigma swamps the perturbation signal
  under the A/A floor → the ``refuse`` verdict.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.epoch.preflight import (
    PREFLIGHT_REPLICATE_BASE,
    VERDICT_OK,
    VERDICT_REFUSE,
    VERDICT_WARN,
    degraded_content_for,
    preflight_verdict,
    run_contract_preflight,
)
from zicato.health.diagnostics import detect_preflight_verdict
from zicato.tournament.calibration import CALIBRATION_REPLICATE_BASE
from zicato_examples.target_0_convergence import mocks as t0_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"

DETERMINISTIC_ADAPTER = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}


def _noisy_adapter(sigma: float) -> dict:
    return {
        "kind": "import",
        "factory": "zicato_examples.target_0_convergence.harness:make_noisy_adapter",
        "args": [{"noise_sigma": sigma}],
    }


# ---------------------------------------------------------------------------
# Pure verdict math + degradation rules
# ---------------------------------------------------------------------------


def test_preflight_replicate_base_clears_calibration_slots() -> None:
    # The degraded draw must never collide with duels (0..) or A/A draws.
    assert PREFLIGHT_REPLICATE_BASE > CALIBRATION_REPLICATE_BASE


def test_verdict_ok_when_signal_clears_floor() -> None:
    import pytest

    verdict, signal = preflight_verdict([3.6, 3.6, 3.6], 3.0, 0.0)
    assert verdict == VERDICT_OK
    assert signal == pytest.approx(0.6)


def test_verdict_refuse_when_signal_at_or_below_floor() -> None:
    # Floor 1.0 (noisy draws), degraded moved only 0.5 from the mean.
    verdict, signal = preflight_verdict([3.0, 4.0], 4.0, 1.0)
    assert verdict == VERDICT_REFUSE
    assert signal == 0.5
    # Exactly-at-floor also refuses (signal <= floor).
    verdict, _ = preflight_verdict([3.0, 5.0], 5.0, 1.0)
    assert verdict == VERDICT_REFUSE


def test_verdict_warn_on_exact_saturation_beats_refuse() -> None:
    # Every probe identical — the 1.000000 signature. Even though
    # signal (0) <= floor (0) also holds, the saturation diagnosis wins.
    verdict, signal = preflight_verdict([1.0, 1.0, 1.0], 1.0, 0.0)
    assert verdict == VERDICT_WARN
    assert signal == 0.0


def test_degraded_content_rules() -> None:
    from zicato.core.mutation import MutationPoint

    def _point(kind: str, content: str, suffix: str = ".py") -> MutationPoint:
        return MutationPoint(
            id="p",
            kind=kind,  # type: ignore[arg-type]
            file=Path(f"/tmp/x{suffix}"),
            source_root=Path("/tmp"),
            line_start=1,
            line_end=1,
            content=content,
            content_hash="",
        )

    # Span: deterministic scramble (reversal) — a pure function of content.
    assert degraded_content_for(_point("span", "abc")) == "cba"
    # Empty span: a fixed garbage token, never a no-op probe.
    assert degraded_content_for(_point("span", "  ")) == "zicato-preflight-degraded"
    # Code region: blanked control flow, always-valid Python.
    assert degraded_content_for(_point("code", "if x:\n    y()\n")) == "pass\n"
    # Whole .py file: a comment-only module (parses, exports nothing).
    assert degraded_content_for(_point("file", "X = 1\n")).startswith("#")
    # Whole non-.py file: reversed content.
    assert degraded_content_for(_point("file", "abc", suffix=".md")) == "cba"


# ---------------------------------------------------------------------------
# Health detector
# ---------------------------------------------------------------------------


def test_detector_silent_without_record_or_on_ok() -> None:
    assert detect_preflight_verdict(None) == []
    assert detect_preflight_verdict({"verdict": "ok", "signal": 1.0}) == []
    assert detect_preflight_verdict({"verdict": "junk"}) == []
    assert detect_preflight_verdict({}) == []


def test_detector_fires_critical_on_refuse_and_warning_on_saturation() -> None:
    (refuse,) = detect_preflight_verdict(
        {"verdict": "refuse", "signal": 0.1, "noise_floor_max_abs_delta": 0.5}
    )
    assert refuse.code == "preflight_signal_below_floor"
    assert refuse.severity == "critical"
    assert refuse.detail["signal"] == 0.1
    assert refuse.detail["noise_floor_max_abs_delta"] == 0.5

    (warn,) = detect_preflight_verdict({"verdict": "warn", "signal": 0.0})
    assert warn.code == "preflight_saturated_contract"
    assert warn.severity == "warning"


# ---------------------------------------------------------------------------
# Integration — target_0 through the real board-unit workers
# ---------------------------------------------------------------------------


def _bootstrap(
    tmp_path: Path,
    *,
    adapter_block: dict | None = None,
    board_source: Path | None = None,
    extra_config: dict | None = None,
    agent_dir: Path | None = None,
) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "adapter": adapter_block or DETERMINISTIC_ADAPTER,
                "mutable_trees": [str(agent_dir or AGENT_DIR)],
                **(extra_config or {}),
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Pre-flight brief\n- Remove defect tokens.\n")
    weights = _scoring_from_dict(json.loads(SCORING_PATH.read_text()))
    cfg = new_epoch(
        workspace,
        name="t0-preflight",
        board_source=board_source or BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _seed_baseline(workspace: Path, epoch_id: str) -> object:
    """Materialise v0 (no rounds, no proposer) and return the champion."""
    from zicato import workspace_loader
    from zicato.core.types import Generation
    from zicato.orchestrator import (
        _ensure_baseline_snapshot,
        _resolve_current_generation,
        _snapshot_root,
    )

    workspace_config = workspace_loader.load_workspace_config(workspace)
    _ensure_baseline_snapshot(workspace, epoch_id, workspace_config)
    champion_id = _resolve_current_generation(workspace, epoch_id)
    return Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )


def _run_preflight(workspace: Path, epoch_id: str, runs: int = 3) -> tuple:
    from zicato import adapter_factory, runtime_factory, workspace_loader

    champion = _seed_baseline(workspace, epoch_id)
    workspace_config = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)
    return asyncio.run(
        run_contract_preflight(
            adapter=adapter,
            generation=champion,  # type: ignore[arg-type]
            board=workspace_loader.load_current_board(workspace),
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace,
            epoch_id=epoch_id,
            runs=runs,
        )
    )


def test_deterministic_adapter_ok_verdict(tmp_path: Path) -> None:
    """Floor exactly 0.0, perturbation signal > 0 ⇒ OK, and the degraded
    tree was ephemeral — the lineage carries only v0."""
    workspace, epoch_id = _bootstrap(tmp_path)
    report, floor = _run_preflight(workspace, epoch_id)

    assert floor.max_abs_delta == 0.0
    assert report.verdict == VERDICT_OK
    assert report.signal > 0.0
    assert report.noise_floor_max_abs_delta == 0.0
    # The synthetic degradation targeted the FIRST enumerated point.
    assert report.degraded_mutation_id == "style_rules"
    assert report.degraded_mutation_kind == "span"
    # The degraded tree never entered the lineage: only v0 exists.
    from zicato.epoch.genstore import default_generation_store

    assert default_generation_store(workspace).list_generations(epoch_id) == ["v0"]
    # The real champion snapshot is untouched (still carries its defects).
    policy = (Path(report.degraded_file).name, "policy.py")
    assert policy[0] == policy[1]
    snapshot_policy = (
        default_generation_store(workspace).snapshot_root(epoch_id, "v0") / "agent" / "policy.py"
    ).read_text()
    assert "verbose-prose; omit-summary; skip-citations" in snapshot_policy

    # Persist + round-trip through the epoch record (additive field).
    from zicato.epoch.lifecycle import set_epoch_preflight

    before = load_epoch(workspace, epoch_id)
    set_epoch_preflight(workspace, epoch_id, report.to_json())
    reloaded = load_epoch(workspace, epoch_id)
    assert reloaded.preflight is not None
    assert reloaded.preflight["verdict"] == "ok"
    assert reloaded.preflight["generation_id"] == "v0"
    # A runtime measurement, never a contract input: the hash is untouched.
    assert reloaded.contract_hash == before.contract_hash


def _no_expectation_board(tmp_path: Path) -> Path:
    """target_0's board with every ``expectation`` stripped — a board that
    cannot discriminate the deterministic harness's defect tokens (each
    token still emits exactly one drift frame, degraded or not)."""
    out = tmp_path / "board_saturating.jsonl"
    lines = []
    for line in BOARD_PATH.read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload.pop("expectation", None)
        lines.append(json.dumps(payload))
    out.write_text("\n".join(lines) + "\n")
    return out


def test_saturating_board_warns(tmp_path: Path) -> None:
    """A no-expectation board scores champion and degraded identically —
    the 1.000000 saturation signature ⇒ WARN (not REFUSE)."""
    board = _no_expectation_board(tmp_path)
    workspace, epoch_id = _bootstrap(tmp_path, board_source=board)
    report, floor = _run_preflight(workspace, epoch_id)

    assert floor.max_abs_delta == 0.0
    assert report.degraded_scalar == report.champion_scalars[0]
    assert report.signal == 0.0
    assert report.verdict == VERDICT_WARN
    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_saturated_contract"
    assert finding.severity == "warning"


def _single_unknown_token_agent(tmp_path: Path) -> Path:
    """A policy whose ONE token is unknown to the harness.

    Unknown tokens emit one drift frame each but suppress no feature, and
    the pre-flight's scramble (a reversal) turns one unknown token into
    another — so the degraded tree's TRUE quality equals the champion's.
    Any measured difference is pure observation noise: exactly the
    "noise swamps the achievable signal" pathology REFUSE exists for.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "__init__.py").write_text("")
    (agent / "policy.py").write_text(
        '"""Single-unknown-token policy for the pre-flight REFUSE case."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        '# zicato:mutable id="style_rules" role="writing_policy"\n'
        'STYLE_RULES = "quirky-tone"\n'
        "\n"
        '__all__ = ["STYLE_RULES"]\n'
    )
    return agent


def test_noisy_adapter_refuses_when_signal_below_floor(tmp_path: Path) -> None:
    """High-sigma seeded noise + a perturbation that cannot move true
    quality ⇒ the A/A floor swamps the measured signal — the
    signal<=floor REFUSE path — and the detector fires."""
    workspace, epoch_id = _bootstrap(
        tmp_path,
        adapter_block=_noisy_adapter(0.45),
        agent_dir=_single_unknown_token_agent(tmp_path),
    )
    report, floor = _run_preflight(workspace, epoch_id, runs=5)

    assert floor.max_abs_delta > 0.0
    assert report.signal <= floor.max_abs_delta
    assert report.verdict == VERDICT_REFUSE
    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_signal_below_floor"
    assert finding.severity == "critical"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_board_preflight_cli_measures_and_persists(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace, epoch_id = _bootstrap(tmp_path)
    _seed_baseline(workspace, epoch_id)

    runner = CliRunner()
    result = runner.invoke(
        build_cli_root(),
        [
            "board",
            "preflight",
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
    assert "Contract pre-flight" in result.output
    assert "achievable signal" in result.output
    assert "verdict:           OK" in result.output

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.preflight is not None
    assert cfg.preflight["verdict"] == "ok"
    # The pre-flight's A/A draws double as the noise-floor measurement.
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["max_abs_delta"] == 0.0


# ---------------------------------------------------------------------------
# Epoch-open hook (opt-in workspace knob, mirroring calibrate_noise_floor)
# ---------------------------------------------------------------------------


def test_epoch_open_hook_persists_verdict(tmp_path: Path) -> None:
    from zicato.evolve.loop import evolve_n_rounds

    workspace, epoch_id = _bootstrap(tmp_path, extra_config={"contract_preflight": 3})
    t0_mocks.reset()
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.preflight is not None
    assert cfg.preflight["verdict"] == "ok"
    assert cfg.preflight["noise_floor_runs"] == 3
    # Measured on the epoch's seed champion at epoch open.
    assert cfg.preflight["generation_id"] == "v0"
    # The shared A/A draws also persisted the noise floor.
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["max_abs_delta"] == 0.0


def test_preflight_voids_on_infra_abort_instead_of_persisting_a_poisoned_floor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An endpoint outage during the epoch's first round must VOID the
    pre-flight (best-effort skip), not fold the aborted draws into a persisted
    noise floor / verdict.

    Regression for the issue-#84 review (Finding 1): the default-on pre-flight
    is a new consumer of champion A/A draws; without the ``is_infra_abort_cause``
    guard a transient outage poisons the epoch's floor and — under the hard
    gate — would falsely disqualify a contract that an outage merely made
    un-measurable. With the guard, ``run_contract_preflight`` raises
    :class:`NoiseFloorInconclusive` (the caller's ``best_effort`` turns it into
    a skip + re-measure next round); the default ``measure_noise_floor`` path
    (``raise_on_infra_abort=False`` — the ``board audit`` surface) is unchanged.
    """
    import pytest

    import zicato.tournament.runner as _runner_mod
    from zicato.core.types import DriftCount, LossProfile
    from zicato.tournament.calibration import NoiseFloorInconclusive, measure_noise_floor

    async def _infra_abort_run_single(
        *,
        adapter: object,
        generation: object,
        entry: object,
        weights: object,
        config: object,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, side, match_id
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",  # type: ignore[attr-defined]
            entry_id=entry.id,  # type: ignore[attr-defined]
            generation_id=generation.id,  # type: ignore[attr-defined]
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=1.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=10.0,
            pass_fail=None,
            abort_cause="nonzero_exit:1",  # an is_infra_abort_cause class
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _infra_abort_run_single)

    # The strict pre-flight consumer VOIDS the measurement rather than persist
    # an outage-derived floor.
    workspace, epoch_id = _bootstrap(tmp_path)
    with pytest.raises(NoiseFloorInconclusive):
        _run_preflight(workspace, epoch_id)

    # Backward-compat: the default calibration surface (board audit) still
    # tolerates aborts and returns a floor — the guard is opt-in.
    from zicato import adapter_factory, runtime_factory, workspace_loader

    champion = _seed_baseline(workspace, epoch_id)
    wc = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(wc)
    config = runtime_factory.make_runtime_config(
        wc,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)
    floor = asyncio.run(
        measure_noise_floor(
            adapter=adapter,
            generation=champion,  # type: ignore[arg-type]
            board=workspace_loader.load_current_board(workspace),
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace,
            epoch_id=epoch_id,
            runs=3,
        )
    )
    assert floor.runs == 3  # tolerated (raise_on_infra_abort defaults False)
