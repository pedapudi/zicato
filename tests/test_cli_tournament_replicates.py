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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._runtime_builders import (
    prepare_tournament_epoch,
    record_tournament_score,
    runtime_config,
)
from zicato.core import BoardEntry, ScoringWeights
from zicato.core.tournament import TournamentStructure
from zicato.epoch.lifecycle import current_epoch_id
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


def _make_cli_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use deterministic runtime construction with the real frozen contract reader."""
    loader_mod = types.SimpleNamespace(
        load_workspace_config=lambda root: {"mutable_trees": []},
    )
    adapter_factory_mod = types.SimpleNamespace(
        make_adapter_from_config=lambda cfg, *, workspace_root: object(),
    )
    runtime_factory_mod = types.SimpleNamespace(
        make_runtime_config=lambda cfg, *, workspace_root, execution_roles: replace(
            runtime_config(workspace_root), execution_roles=execution_roles
        ),
    )
    monkeypatch.setattr(
        "zicato.cli.commands.tournament._resolve_workspace_components",
        lambda: (loader_mod, adapter_factory_mod, runtime_factory_mod),
    )
    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *args, **kwargs: None)


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


def _make_workspace(tmp_path: Path, weights: ScoringWeights | None = None) -> Path:
    workspace = tmp_path / "ws"
    epoch_id = prepare_tournament_epoch(
        workspace, runtime_config(workspace), _board(), weights or ScoringWeights()
    )
    for generation_id in ("v0", "v1"):
        (workspace / "epochs" / epoch_id / "generations" / generation_id / "snapshot").mkdir(
            parents=True
        )
    return workspace


def _seed_historical_aggregate(workspace: Path, generation_id: str) -> None:
    """Publish the selected epoch's parent aggregate for fast-mode admission."""
    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    record_tournament_score(
        workspace,
        epoch_id,
        generation_id,
        {"scalar": 1.0, "pass_rate": 1.0, "base_seed": None, "generation_id": generation_id},
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

    weights = ScoringWeights(
        tournament_structure=TournamentStructure(structure="gauntlet", params={"replicates": 4})
    )
    workspace = _make_workspace(tmp_path, weights)
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
    _seed_historical_aggregate(workspace, "v0")

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
    _seed_historical_aggregate(workspace, "v0")

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


@pytest.mark.parametrize(
    "aggregate",
    [
        {"generation_id": "v0"},
        {"generation_id": "v0", "base_seed": 17},
    ],
)
def test_fast_cli_remeasures_a_champion_without_requested_seed_and_parent_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate: dict[str, Any]
) -> None:
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)
    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    record_tournament_score(workspace, epoch_id, "v0", {"scalar": 1.0, **aggregate})
    captured: dict[str, Any] = {}

    async def paired(**kwargs: Any) -> TournamentResult:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", paired)
    result = CliRunner().invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--mode", "fast"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert captured["parent_gen"].id == "v0"
    assert captured["champion_force_fresh"] is False


def test_fast_cli_refuses_a_champion_aggregate_for_another_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = _make_workspace(tmp_path)
    _make_cli_stubs(monkeypatch)
    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    (workspace / "epochs" / epoch_id / "generations/v0/gen_score.json").write_text(
        json.dumps({"format_version": 1, "scalar": 1.0, "generation_id": "v2", "base_seed": None})
    )
    calls: list[dict[str, Any]] = []

    async def paired(**kwargs: Any) -> TournamentResult:
        calls.append(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", paired)
    result = CliRunner().invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--mode", "fast"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "generation_id 'v2' does not match 'v0'" in result.output
    assert calls == []


def test_cli_explicit_epoch_uses_that_epochs_frozen_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit epoch selects its own board, scoring, gate, and output paths."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = tmp_path / "ws"

    current_board = [
        BoardEntry(
            id="current_entry",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="current",
        )
    ]
    selected_board = [
        BoardEntry(
            id="selected_entry",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="selected",
        )
    ]
    epochs = {}
    for name, board, replicates in (
        ("selected", selected_board, 5),
        ("current", current_board, 7),
    ):
        weights = ScoringWeights(
            tournament_structure=TournamentStructure(
                structure="gauntlet",
                params={"replicates": replicates},
            )
        )
        epochs[name] = prepare_tournament_epoch(
            workspace, runtime_config(workspace), board, weights, name=name
        )
    selected_epoch = epochs["selected"]
    assert current_epoch_id(workspace) == epochs["current"]

    for generation_id in ("v0", "v1"):
        (workspace / "epochs" / selected_epoch / "generations" / generation_id / "snapshot").mkdir(
            parents=True
        )

    events: list[str] = []
    loader_mod = types.SimpleNamespace(
        load_workspace_config=lambda root: events.append("config") or {"mutable_trees": []},
    )
    adapter_factory_mod = types.SimpleNamespace(
        make_adapter_from_config=lambda cfg, *, workspace_root: events.append("adapter")
        or object(),
    )
    runtime_factory_mod = types.SimpleNamespace(
        make_runtime_config=lambda cfg, *, workspace_root, execution_roles: replace(
            runtime_config(workspace_root), execution_roles=execution_roles
        ),
    )
    monkeypatch.setattr(
        "zicato.cli.commands.tournament._resolve_workspace_components",
        lambda: (loader_mod, adapter_factory_mod, runtime_factory_mod),
    )

    def fake_gate(root: Path, *, epoch_id: str, live_contract: bool) -> None:
        assert root == workspace
        assert epoch_id == selected_epoch
        assert live_contract is False
        events.append("gate")

    monkeypatch.setattr("zicato.check.require_workspace_valid", fake_gate)
    captured: dict[str, Any] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        events.append("run")
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    result = CliRunner().invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--epoch", selected_epoch],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert events == ["gate", "config", "adapter", "run"]
    assert captured["epoch_id"] == selected_epoch
    assert [entry.id for entry in captured["board"]] == ["selected_entry"]
    assert captured["replicates"] == 5


def test_cli_workspace_gate_stops_before_adapter_construction_or_spend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from zicato.check import CheckReport, Finding, WorkspaceCheckError
    from zicato.cli.commands.tournament import tournament_cmd

    workspace = tmp_path / "ws"
    workspace.mkdir()
    events: list[str] = []
    loader_mod = types.SimpleNamespace(
        load_workspace_config=lambda root: events.append("config"),
    )
    adapter_factory_mod = types.SimpleNamespace(
        make_adapter_from_config=lambda cfg, *, workspace_root: events.append("adapter"),
    )
    runtime_factory_mod = types.SimpleNamespace(
        make_runtime_config=lambda cfg, *, workspace_root: events.append("runtime"),
    )
    monkeypatch.setattr(
        "zicato.cli.commands.tournament._resolve_workspace_components",
        lambda: (loader_mod, adapter_factory_mod, runtime_factory_mod),
    )

    def reject_workspace(root: Path, *, epoch_id: str, live_contract: bool) -> None:
        events.append("gate")
        raise WorkspaceCheckError(
            CheckReport(
                workspace_root=str(root),
                findings=(Finding("broken_contract", "contract is not measurable", {}),),
            )
        )

    monkeypatch.setattr("zicato.check.require_workspace_valid", reject_workspace)

    result = CliRunner().invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--epoch", "selected_epoch"],
    )

    assert result.exit_code != 0
    assert events == ["gate"]
    assert "contract is not measurable" in result.output
