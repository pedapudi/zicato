"""No zicato Python code writes, prunes, moves, or deletes the supervisor's ``proctor/``.

The supervisor keeps its tamper-evident audit ledger in the workspace's
``proctor/`` directory. The ledger is evidence about the orchestrator, so the
orchestrator's own code must never change it. Two independent observations
pin that across the operations that delete or rewrite workspace state: an
audit hook that records every write, removal, rename, or recursive delete
that reaches the directory from this process, and a byte-level comparison of
the directory before and after, which also covers the worker subprocesses
the hook cannot see.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    target_call_llm,
)
from zicato.workspace.layout import WorkspaceLayout

#: Directories under observation, and what reached them.
_WATCHED: list[str] = []
_TOUCHES: list[tuple[str, str]] = []

#: Audited events whose path arguments name something being changed.
_CHANGING_EVENTS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.chown",
        "shutil.move",
        "shutil.rmtree",
    }
)
#: Copies change only their destination, the last path argument.
_COPY_EVENTS = frozenset(
    {"shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree"}
)
#: Events that also take a directory's contents with it when they name an ancestor.
_RECURSIVE_EVENTS = frozenset({"os.rename", "shutil.move", "shutil.rmtree"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _paths(event: str, args: tuple[Any, ...]) -> list[str]:
    candidates = [arg for arg in args if isinstance(arg, str | bytes | os.PathLike)]
    if event == "open":
        path, mode, flags = args
        writes = isinstance(mode, str) and any(c in mode for c in "wax+")
        writes = writes or (isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        named = isinstance(path, str | bytes | os.PathLike)
        return [os.path.abspath(os.fsdecode(path))] if writes and named else []
    if event in _COPY_EVENTS:
        candidates = candidates[-1:]
    elif event not in _CHANGING_EVENTS:
        return []
    return [os.path.abspath(os.fsdecode(path)) for path in candidates]


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _WATCHED or (event != "open" and event not in _CHANGING_EVENTS | _COPY_EVENTS):
        return
    for path in _paths(event, args):
        for root in _WATCHED:
            inside = path == root or path.startswith(root + os.sep)
            ancestor = event in _RECURSIVE_EVENTS and root.startswith(path.rstrip(os.sep) + os.sep)
            if inside or ancestor:
                _TOUCHES.append((event, path))


sys.addaudithook(_audit)


def _contents(root: Path) -> dict[str, bytes | None]:
    """Every entry beneath ``root``: file bytes, or ``None`` for a directory."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


@contextmanager
def _observed(workspace: Path) -> Iterator[None]:
    """Seed ``proctor/`` with ledger files and fail if anything changes them."""
    proctor_dir = WorkspaceLayout.from_root(workspace).proctor_dir
    (proctor_dir / "archive").mkdir(parents=True)
    (proctor_dir / "audit_ledger.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")
    (proctor_dir / "archive" / "audit_ledger.1.jsonl").write_bytes(b"\x00older chain\n")
    before = _contents(proctor_dir)
    _TOUCHES.clear()
    _WATCHED.append(os.path.abspath(proctor_dir))
    try:
        yield
    finally:
        _WATCHED.clear()
    assert _TOUCHES == []
    assert _contents(proctor_dir) == before


@pytest.fixture
def proctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A bootstrapped directory-store workspace whose ``proctor/`` is under observation."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    with _observed(workspace):
        yield workspace, epoch_id


def _cli(*args: str) -> None:
    from zicato.cli.discovery import build_cli_root

    result = CliRunner().invoke(build_cli_root(), list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output


def _read_through_the_dashboard(workspace: Path, tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from zicato.dashboard.server import create_app

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with TestClient(create_app(workspace, static, read_only=True)) as client:
        for route in ("/api/state", "/api/health", "/api/environment", "/api/logs"):
            assert client.get(route).status_code == 200, route


def test_loop_maintenance_and_reinitialisation_leave_proctor_untouched(
    proctor: tuple[Path, str], tmp_path: Path
) -> None:
    """Rounds, dashboard reads, repairs, a contract publication, epoch close and gc, and init."""
    from zicato.contract_draft import operations
    from zicato.contract_draft.draft import TournamentDraft
    from zicato.orchestrator import evolve_n_rounds

    workspace, epoch_id = proctor
    asyncio.run(
        evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            max_consecutive_rejections=5,
            stop_on_degenerate_health=False,
        )
    )
    _read_through_the_dashboard(workspace, tmp_path)
    ws = str(workspace)
    for command in ("index", "generations", "v0-baseline"):
        _cli("repair", command, "--workspace", ws)
    _cli("repair", "report", "--workspace", ws, "--no-llm")
    draft = TournamentDraft.from_workspace(workspace)
    operations.set_brief(draft, "Published while the ledger is observed.\n")
    operations.apply(draft, workspace, confirm=True)
    _cli("epoch", "close", epoch_id, "--workspace", ws)
    _cli("epoch", "gc", epoch_id, "--workspace", ws, "--keep-promoted-only", "--apply")
    _cli("init", "--workspace", ws, "--force", "--reset-lineage")


def test_interrupted_generation_discard_leaves_proctor_untouched(
    proctor: tuple[Path, str],
) -> None:
    """Resume clears runtime state and discards an interrupted candidate."""
    from dataclasses import replace

    from zicato.epoch.journal import read_experiment, write_experiment
    from zicato.orchestrator import evolve_n_rounds
    from zicato.runtime.resume import prepare_resume

    workspace, epoch_id = proctor
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            stop_on_degenerate_health=False,
        )
    )
    # A proposed and applied candidate whose tournament never ran a unit.
    layout = WorkspaceLayout.from_root(workspace)
    latest = max(int(path.name[1:]) for path in layout.generations_dir(epoch_id).iterdir())
    settled = read_experiment(workspace, epoch_id, f"v{latest}")
    interrupted = f"v{latest + 1}"
    write_experiment(
        workspace,
        epoch_id,
        interrupted,
        replace(settled, id=f"{settled.id}-interrupted", generation_id=interrupted, outcome=None),
    )
    snapshot = layout.generation_dir(epoch_id, interrupted) / "snapshot"
    snapshot.mkdir()
    (snapshot / "agent.py").write_text("GREETING = 'hi'\n", encoding="utf-8")
    assert prepare_resume(workspace, epoch_id).classification == "discard_no_progress"


@pytest.mark.integration
@pytest.mark.slow
def test_git_store_round_and_gc_leave_proctor_untouched(tmp_path: Path) -> None:
    """The default git generation store: a measured round, an index repair, and gc."""
    from tests._recommended_loop_support import bootstrap, run_round

    workspace, epoch_id, _ = bootstrap(tmp_path)
    with _observed(workspace):
        asyncio.run(run_round(workspace, epoch_id))
        ws = str(workspace)
        _cli("repair", "index", "--workspace", ws)
        _cli("epoch", "gc", epoch_id, "--workspace", ws, "--keep-promoted-only", "--apply")
