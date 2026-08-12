"""Attaching to a dashboard service, and the CLI command that does it.

Three things matter here and nothing else: the TUI attaches to a service
someone else is already running rather than starting a competing one, it reads
the SPAWNED service's port back from the endpoint file instead of assuming it,
and every failure carries the command that fixes it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.tui import tui_cmd
from zicato.tui import MISSING_EXTRA
from zicato.tui.client import ServiceError
from zicato.tui.service import HOST, attach, endpoint_file, read_endpoint, spawn_argv


def write_endpoint(workspace: Path, port: int, host: str = HOST) -> Path:
    path = endpoint_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": host, "port": port}) + "\n", encoding="utf-8")
    return path


def test_endpoint_readback_matches_what_the_service_writes(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    write_endpoint(ws, 7895)
    assert read_endpoint(endpoint_file(ws)) == "http://127.0.0.1:7895"


def test_a_wildcard_bind_is_reached_over_loopback(tmp_path: Path) -> None:
    """A service bound to every interface is still a LOCAL surface to us."""
    ws = tmp_path / ".zicato"
    write_endpoint(ws, 7896, host="0.0.0.0")
    assert read_endpoint(endpoint_file(ws)) == "http://127.0.0.1:7896"


@pytest.mark.parametrize("body", ["", "{", "[]", '{"host": "h"}'])
def test_unreadable_endpoint_files_yield_none(tmp_path: Path, body: str) -> None:
    path = tmp_path / "dashboard.json"
    path.write_text(body, encoding="utf-8")
    assert read_endpoint(path) is None
    assert read_endpoint(tmp_path / "absent.json") is None


def test_spawn_argv_is_the_same_path_evolve_uses(tmp_path: Path) -> None:
    """One spawn path, so a TUI-started service behaves like an evolve-started one."""
    argv = spawn_argv(tmp_path, 7892)
    assert argv[:3] == [sys.executable, "-m", "zicato.dashboard"]
    assert "--host" in argv and argv[argv.index("--host") + 1] == HOST
    assert "--dashboard-bind" not in argv  # loopback only; there is no bind flag


def test_attach_prefers_a_running_service_over_starting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / ".zicato"
    write_endpoint(ws, 7899)
    monkeypatch.setattr("zicato.tui.client.HttpClient.get", lambda self, path: {"status": "ok"})

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("attach must not spawn when a service is already answering")

    monkeypatch.setattr("subprocess.Popen", explode)
    attachment = attach(url=None, workspace=ws)
    assert attachment.url == "http://127.0.0.1:7899"
    assert attachment.owned is False  # we did not start it, so we must not stop it


def test_a_stale_endpoint_file_does_not_wedge_the_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished run leaves its endpoint file behind; we must not attach to it."""
    ws = tmp_path / ".zicato"
    write_endpoint(ws, 7899)
    monkeypatch.setattr(
        "zicato.tui.client.HttpClient.get",
        lambda self, path: (_ for _ in ()).throw(ServiceError("dead")),
    )
    spawned: list[list[str]] = []

    class FakeProc:
        pid = 4242

        def poll(self) -> int | None:
            return None

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        spawned.append(argv)
        raise OSError("no service in this test")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with pytest.raises(ServiceError) as excinfo:
        attach(url=None, workspace=ws, timeout=0.2, sleep=0.01)
    assert spawned, "a stale endpoint must fall through to a spawn"
    assert excinfo.value.hint and "dashboard" in excinfo.value.hint


def test_attach_with_neither_url_nor_workspace_says_what_to_pass() -> None:
    with pytest.raises(ServiceError) as excinfo:
        attach(url=None, workspace=None)
    assert "--url" in (excinfo.value.hint or "")


def test_spawn_that_never_binds_is_reported_not_hung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)

    class DeadProc:
        pid = 4243

        def poll(self) -> int:
            return 1

    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: DeadProc())
    with pytest.raises(ServiceError, match="exited before binding"):
        attach(url=None, workspace=ws, timeout=1.0, sleep=0.01)


# ---------------------------------------------------------------------------
# The CLI command
# ---------------------------------------------------------------------------


def test_tui_is_registered_on_the_cli_root() -> None:
    from zicato.cli.discovery import build_cli_root

    assert "tui" in build_cli_root().commands


def test_cli_passes_the_view_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("zicato.tui.run_tui", fake_run)
    result = CliRunner().invoke(
        tui_cmd, ["--url", "http://127.0.0.1:7892", "--view", "candidate/v4"]
    )
    assert result.exit_code == 0, result.output
    assert seen["url"] == "http://127.0.0.1:7892"
    assert seen["view"] == "candidate/v4"
    assert seen["workspace"] is None  # an explicit --url never spawns


def test_missing_extra_is_an_instruction_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_import(**kwargs: object) -> None:
        raise ImportError("no textual")

    monkeypatch.setattr("zicato.tui.run_tui", raise_import)
    result = CliRunner().invoke(tui_cmd, ["--url", "http://127.0.0.1:7892"])
    assert result.exit_code != 0
    assert MISSING_EXTRA in result.output
    assert "Traceback" not in result.output


def test_service_error_surfaces_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_service(**kwargs: object) -> None:
        raise ServiceError("cannot reach the dashboard service", hint="start one with X")

    monkeypatch.setattr("zicato.tui.run_tui", raise_service)
    result = CliRunner().invoke(tui_cmd, ["--url", "http://127.0.0.1:7892"])
    assert result.exit_code != 0
    assert "cannot reach the dashboard service" in result.output
    assert "start one with X" in result.output
