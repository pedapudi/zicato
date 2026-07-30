"""``zicato tournament`` honors the contract's resolved replicate count.

Before this, ``tournament_cmd`` called ``run_fast_mode`` / ``run_tournament``
without a ``replicates`` kwarg, so both runners fell back to their own
default of ``1`` — silently disagreeing with ``zicato evolve``, which always
threads the tournament structure's RESOLVED value (``strategy.replicates()``;
gauntlet defaults to 2). These tests pin the CLI to the same resolution path
(mirroring ``orchestrator.evolve_once``) for both ``--mode full`` and
``--mode fast``, and verify the new ``--replicates`` debug override reproduces
the historical single-run behaviour.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from zicato.core import BoardEntry, RuntimeConfig, ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.tournament.gate import GateOutcome
from zicato.tournament.runner import TournamentResult


def _board() -> list[BoardEntry]:
    return [
        BoardEntry(
            id="entry_a",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
        ),
    ]


def _make_runtime_config(tmp_path: Path) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_call,
        auxiliary_call_llm=aux_call,
    )


def _make_cli_stubs(
    monkeypatch: pytest.MonkeyPatch, *, weights: ScoringWeights | None = None
) -> None:
    """Wire CLI-side stubs so ``tournament_cmd`` runs without a real workspace."""
    loader_mod = types.SimpleNamespace(
        load_workspace_config=lambda root: {"mutable_trees": []},
        load_current_board=lambda root: _board(),
        load_current_board_with_meta=lambda root: (_board(), (), False),
        load_current_scoring=lambda root: weights or ScoringWeights(),
    )
    adapter_factory_mod = types.SimpleNamespace(
        make_adapter_from_config=lambda cfg: object(),
    )
    runtime_factory_mod = types.SimpleNamespace(
        make_runtime_config=lambda cfg, *, workspace_root: _make_runtime_config(workspace_root),
    )
    monkeypatch.setattr(
        "zicato.cli.commands.tournament._resolve_workspace_components",
        lambda: (loader_mod, adapter_factory_mod, runtime_factory_mod),
    )


def _fake_result() -> TournamentResult:
    return TournamentResult(
        parent_generation_id="v0",
        child_generation_id="v1",
        parent_agg={"scalar": 1.0, "pass_rate": 1.0},
        child_agg={"scalar": 0.0, "pass_rate": 1.0},
        outcome=GateOutcome(
            decision="promoted",
            reason="",
            delta_scalar=-1.0,
            delta_pass_rate=0.0,
        ),
        per_entry_losses={},
    )


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "current_epoch").write_text("e0", encoding="utf-8")
    snap_v0 = workspace / "epochs" / "e0" / "generations" / "v0" / "snapshot"
    snap_v1 = workspace / "epochs" / "e0" / "generations" / "v1" / "snapshot"
    snap_v0.mkdir(parents=True)
    snap_v1.mkdir(parents=True)
    return workspace


def _seed_historical_aggregate(workspace: Path, epoch_id: str, generation_id: str) -> None:
    """Write the ``gen_score.json`` fast-mode reads as the parent's cached aggregate."""
    import json

    from zicato.core.workspace import generation_dir

    gen_dir = generation_dir(workspace, epoch_id, generation_id)
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "gen_score.json").write_text(
        json.dumps({"scalar": 1.0, "pass_rate": 1.0}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# --mode full
# ---------------------------------------------------------------------------


def test_cli_full_mode_defaults_to_the_structure_resolved_replicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``--replicates`` ⇒ the gauntlet's resolved default (2), matching
    what ``zicato evolve`` uses for the same contract — not the runner's own
    bare fallback of 1."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd, ["v0", "v1", "--workspace", str(workspace)], catch_exceptions=False
    )

    assert res.exit_code == 0, res.output
    assert captured["replicates"] == 2


def test_cli_full_mode_replicates_override_reproduces_old_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--replicates 1`` reproduces the historical single-run duel."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--replicates", "1"],
        catch_exceptions=False,
    )

    assert res.exit_code == 0, res.output
    assert captured["replicates"] == 1


def test_cli_full_mode_honors_structure_params_replicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contract pinning ``params["replicates"]`` resolves that value, not
    the class default — same rule ``evolve_once`` follows."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    weights = ScoringWeights(
        tournament_structure=TournamentStructure(structure="gauntlet", params={"replicates": 4})
    )
    _make_cli_stubs(monkeypatch, weights=weights)

    captured: dict[str, Any] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd, ["v0", "v1", "--workspace", str(workspace)], catch_exceptions=False
    )

    assert res.exit_code == 0, res.output
    assert captured["replicates"] == 4


# ---------------------------------------------------------------------------
# --mode fast
# ---------------------------------------------------------------------------


def test_cli_fast_mode_defaults_to_the_structure_resolved_replicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)
    _seed_historical_aggregate(workspace, "e0", "v0")

    captured: dict[str, Any] = {}

    async def fake_run_fast_mode(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_fast_mode", fake_run_fast_mode)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--mode", "fast"],
        catch_exceptions=False,
    )

    assert res.exit_code == 0, res.output
    assert captured["replicates"] == 2


def test_cli_fast_mode_replicates_override_reproduces_old_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)
    _seed_historical_aggregate(workspace, "e0", "v0")

    captured: dict[str, Any] = {}

    async def fake_run_fast_mode(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_fast_mode", fake_run_fast_mode)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--mode", "fast", "--replicates", "1"],
        catch_exceptions=False,
    )

    assert res.exit_code == 0, res.output
    assert captured["replicates"] == 1
