"""Scaffolding shared by the tests that invoke zicato CLI commands.

Two kinds of scaffolding recur across the CLI suite and are collected
here: a monkeypatch installation that lets a test observe what the
``evolve`` command would have run without running it, and a registered
workspace that the ``reflect suggest`` family needs before any of its
subcommands will resolve.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.board.jsonl import save_board
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.epoch.lifecycle import new_epoch


def install_evolve_capture(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Replace ``evolve_n_rounds`` with a stub that records its kwargs.

    ``evolve`` imports ``zicato.orchestrator.evolve_n_rounds`` inside its
    coroutine rather than at module import, so patching the attribute on
    the module object is what the command sees at call time. The stub
    reports a normal completion through ``stop_reason_out`` when the
    caller supplied one, because the command reads that list after the
    await and would otherwise report an unfinished run.
    """

    async def _fake_evolve_n_rounds(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("completed")
        return []

    import zicato.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "evolve_n_rounds", _fake_evolve_n_rounds)


def registered_workspace(tmp_path: Path, epoch_name: str) -> tuple[Path, str]:
    """A registered workspace holding one epoch over a one-entry board.

    Returns the workspace root and the new epoch's id. The epoch name is
    the caller's, so a failure names the suite that built the workspace.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")
    entry = BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=30, input="hi")
    board_path = tmp_path / "board.jsonl"
    save_board([entry], board_path)
    cfg = new_epoch(ws, epoch_name, board_path, "steer", ScoringWeights())
    return ws, cfg.id
