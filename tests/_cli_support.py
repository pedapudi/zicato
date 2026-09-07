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

from tests._stub_adapter import STUB_ADAPTER_FACTORY
from zicato.board.jsonl import save_board
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.epoch.lifecycle import new_epoch


def install_evolve_capture(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Capture CLI parsing at the private loop seam without workspace IO.

    Invocation ownership has separate real boundary tests. This helper supplies
    only the resolved settings and resource stack needed to inspect CLI arguments.
    """
    from contextlib import AsyncExitStack, asynccontextmanager
    from types import SimpleNamespace

    from zicato.core.settings import resolve_configuration
    from zicato.evolve import invocation, loop

    @asynccontextmanager
    async def context(workspace_root, epoch, instance, *, overlay=None, prepare_contract=None):
        captured["workspace_root"] = workspace_root
        captured["invocation_overlay"] = overlay
        async with AsyncExitStack() as resources:
            yield SimpleNamespace(
                configuration=resolve_configuration({}, overlay=overlay), resources=resources
            )

    async def capture(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        stop_reason_out = kwargs.get("stop_reason_out")
        if stop_reason_out is not None:
            stop_reason_out.append("completed")
        return []

    monkeypatch.setattr(invocation, "validated_invocation", context)
    monkeypatch.setattr(loop, "_evolve_n_rounds", capture)


def registered_workspace(tmp_path: Path, epoch_name: str) -> tuple[Path, str]:
    """A registered workspace holding one epoch over a one-entry board.

    Returns the workspace root and the new epoch's id. The epoch name is
    the caller's, so a failure names the suite that built the workspace.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(
        json.dumps({"runtime": {}, "adapter": {"kind": "import", "factory": STUB_ADAPTER_FACTORY}}),
        encoding="utf-8",
    )
    entry = BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=30, input="hi")
    board_path = tmp_path / "board.jsonl"
    save_board([entry], board_path)
    cfg = new_epoch(ws, epoch_name, board_path, "steer", ScoringWeights())
    return ws, cfg.id
