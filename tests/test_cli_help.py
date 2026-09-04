"""Tests for the streamlined zicato CLI: the evolve-centric happy path,
the happy-path / advanced command grouping, and the thorough ``--help``.

Three things are exercised here:

1. ``zicato --help`` is complete — every discovered command appears, the
   happy path (``init`` then ``evolve``) is presented in its own section
   before the advanced commands, and the epilog carries usage examples.
2. The ``help`` command works as an alias for ``--help`` (bare and
   per-command).
3. ``zicato evolve`` resolves the contract and auto-opens an epoch on a
   contract change — the auto-epoching default is wired through the CLI
   command, not just the orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from tests._cli_support import install_evolve_capture
from zicato.cli.commands.evolve import evolve_cmd
from zicato.cli.discovery import (
    HAPPY_PATH_COMMANDS,
    ZicatoGroup,
    build_cli_root,
)

# ---------------------------------------------------------------------------
# Stub LLM callables — referenced by dotted path from the evolve CLI tests.
# ---------------------------------------------------------------------------


async def _target_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _aux_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


# ---------------------------------------------------------------------------
# 1. Thorough --help: every command documented, happy path first
# ---------------------------------------------------------------------------


def test_root_is_a_zicato_group() -> None:
    """The root group is the section-rendering ZicatoGroup subclass."""
    root = build_cli_root()
    assert isinstance(root, ZicatoGroup)


def test_help_lists_every_discovered_command() -> None:
    """``--help`` names every command the discovery layer registered.

    Nothing may be silently dropped from the help screen — a new
    operator must be able to see the whole tool from ``--help`` alone.
    """
    root = build_cli_root()
    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    for name in root.commands:
        assert name in result.output, f"{name!r} missing from --help"


def test_help_has_happy_path_and_advanced_sections() -> None:
    """``--help`` renders two labelled sections, happy path before advanced."""
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    out = result.output

    happy_marker = "Primary commands"
    advanced_marker = "Advanced namespaces"
    assert happy_marker in out
    assert advanced_marker in out
    # The happy-path section must come first.
    assert out.index(happy_marker) < out.index(advanced_marker)


def test_happy_path_section_lists_init_then_evolve() -> None:
    """The happy-path section contains exactly init then evolve, in order."""
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    out = result.output

    assert HAPPY_PATH_COMMANDS == ("init", "evolve")
    # Within the rendered help, init appears before evolve, and both sit
    # above the advanced section.
    init_at = out.index("\n  init")
    evolve_at = out.index("\n  evolve")
    advanced_at = out.index("Advanced namespaces")
    assert init_at < evolve_at < advanced_at


def test_help_explains_auto_epoching() -> None:
    """``--help`` explains, in prose, how evolve decides on an epoch."""
    runner = CliRunner()
    result = runner.invoke(build_cli_root(), ["--help"])
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "contract" in out
    assert "epoch" in out


def test_advanced_command_summaries_are_not_truncated() -> None:
    """No advanced command's one-line summary is a bare RST/ellipsis stub.

    The pre-streamlining help truncated long docstrings to ``...`` and
    leaked RST ``\\`\\``-markup. Explicit ``short_help`` strings fix that;
    this guards the regression.
    """
    root = build_cli_root()
    for name, cmd in root.commands.items():
        short = cmd.get_short_help_str(limit=200)
        assert "``" not in short, f"{name!r} short help leaks RST markup: {short!r}"
        assert not short.endswith("..."), f"{name!r} short help is truncated: {short!r}"


def test_every_command_has_a_help_screen() -> None:
    """Each command (and each subcommand) responds to ``--help`` cleanly.

    The ``help`` command is the one exception: it has no ``--help``
    option of its own (``zicato help help`` is the way to see it), so it
    is checked through that form instead.
    """
    root = build_cli_root()
    runner = CliRunner()
    for name, cmd in root.commands.items():
        if name == "help":
            result = runner.invoke(root, ["help", "help"])
            assert result.exit_code == 0, f"help help failed: {result.output}"
            continue
        result = runner.invoke(root, [name, "--help"])
        assert result.exit_code == 0, f"{name} --help failed: {result.output}"
        if isinstance(cmd, click.Group):
            for sub_name in cmd.commands:
                sub_result = runner.invoke(root, [name, sub_name, "--help"])
                assert (
                    sub_result.exit_code == 0
                ), f"{name} {sub_name} --help failed: {sub_result.output}"


# Evolve resolves the contract and auto-epochs on a contract change.


def test_evolve_passes_auto_epoch_true_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``zicato evolve`` enables contract-hash auto-epoching by default.

    The orchestrator does the contract resolve + epoch roll; the CLI's
    job is to pass ``auto_epoch=True`` unless ``--no-auto-epoch`` is
    given. This pins that wiring.
    """
    captured: dict[str, Any] = {}
    install_evolve_capture(monkeypatch, captured)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_cli_help:_target_call_llm",
            "--auxiliary-call-llm",
            "tests.test_cli_help:_aux_call_llm",
            "--no-dashboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["auto_epoch"] is True


def test_evolve_no_auto_epoch_flag_disables_auto_epoching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-auto-epoch`` flips the orchestrator's ``auto_epoch`` off."""
    captured: dict[str, Any] = {}
    install_evolve_capture(monkeypatch, captured)

    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--harness-call-llm",
            "tests.test_cli_help:_target_call_llm",
            "--auxiliary-call-llm",
            "tests.test_cli_help:_aux_call_llm",
            "--no-auto-epoch",
            "--no-dashboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["auto_epoch"] is False


def test_evolve_resolves_and_auto_epochs_on_contract_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end-ish: ``zicato evolve`` resolves the contract and rolls
    the epoch when that contract has drifted.

    The orchestrator's ``evolve_n_rounds`` is stubbed (no real loop /
    no model traffic / no subprocess spawn), but the *real*
    contract-hash auto-epoching path runs: the stub calls
    ``ensure_epoch_for_contract`` itself, exactly as the real
    orchestrator does before the round loop. The first invocation
    creates an epoch from the contract; after the live board is
    edited, the second invocation must open a *second* epoch.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir(parents=True)

    # Live contract source files, alongside the workspace.
    board = tmp_path / "board.jsonl"
    board.write_text(
        json.dumps(
            {
                "id": "entry-a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": {"prompt": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Improve the greeting.\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"pass_weight": 1.0}), encoding="utf-8")

    # config.json with the contract block pointing at the live files.
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "adk_entrypoint": "pkg.agent:root",
                "mutable_trees": [],
                "source_roots": [],
                "contract": {
                    "board_path": str(board),
                    "rubric_path": str(brief),
                    "scoring_path": str(scoring),
                },
            }
        ),
        encoding="utf-8",
    )

    # Stub evolve_n_rounds: drive the real auto-epoch resolver, exactly
    # as the orchestrator does, then return no outcomes.
    from zicato.orchestrator import ensure_epoch_for_contract

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        if kwargs.get("epoch_id") is None:
            await ensure_epoch_for_contract(
                kwargs["workspace_root"],
                auto_epoch=kwargs.get("auto_epoch", True),
                aux_call_llm=kwargs["evaluation_call_llm"],
                epoch_name=kwargs.get("epoch_name"),
            )
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("completed")
        return []

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)

    from zicato.epoch.lifecycle import list_epochs

    runner = CliRunner()

    # First evolve — no epoch yet, auto-epoching creates the first one.
    first = runner.invoke(
        evolve_cmd,
        [
            "--workspace",
            str(workspace),
            "--harness-call-llm",
            "tests.test_cli_help:_target_call_llm",
            "--auxiliary-call-llm",
            "tests.test_cli_help:_aux_call_llm",
            "--no-dashboard",
        ],
    )
    assert first.exit_code == 0, first.output
    epochs_after_first = list_epochs(workspace)
    assert len(epochs_after_first) == 1, epochs_after_first

    # Edit the live board — the evaluation contract has now drifted.
    board.write_text(
        board.read_text(encoding="utf-8")
        + json.dumps(
            {
                "id": "entry-b",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": {"prompt": "goodbye"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Second evolve — the contract changed, so a new epoch must open.
    second = runner.invoke(
        evolve_cmd,
        [
            "--workspace",
            str(workspace),
            "--harness-call-llm",
            "tests.test_cli_help:_target_call_llm",
            "--auxiliary-call-llm",
            "tests.test_cli_help:_aux_call_llm",
            "--no-dashboard",
        ],
    )
    assert second.exit_code == 0, second.output
    epochs_after_second = list_epochs(workspace)
    assert (
        len(epochs_after_second) == 2
    ), f"contract drift should have rolled the epoch: {epochs_after_second}"
