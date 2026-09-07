"""Attaching to a dashboard service, and the CLI command that does it.

Three things matter here and nothing else: the TUI attaches to a service
someone else is already running rather than starting a competing one, it reads
the SPAWNED service's port back from the endpoint file instead of assuming it,
and every failure carries the command that fixes it.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.tui import tui_cmd
from zicato.tui import MISSING_EXTRA
from zicato.tui.client import HttpClient, ServiceError
from zicato.tui.service import HOST, Attachment, attach, endpoint_file, read_endpoint, spawn_argv


def write_endpoint(workspace: Path, port: int, host: str = HOST) -> Path:
    path = endpoint_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": host, "port": port}) + "\n", encoding="utf-8")
    return path


@contextmanager
def dashboard_service(workspace: Path) -> Iterator[int]:
    """Serve the production dashboard over an ephemeral loopback socket."""
    import uvicorn

    from zicato.dashboard.server import create_app

    workspace.mkdir(parents=True)
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        port = listener.getsockname()[1]
        app = create_app(workspace, static_dir=workspace / "absent-static")
        app.state.bound_port = port
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "dashboard did not bind within five seconds"
            yield port
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "dashboard server did not stop"


@pytest.mark.integration
def test_automatic_attachment_rejects_a_real_foreign_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = tmp_path / "requested" / ".zicato"
    foreign = tmp_path / "foreign" / ".zicato"
    with dashboard_service(requested) as requested_port, dashboard_service(foreign) as foreign_port:
        requested_url = f"http://{HOST}:{requested_port}"
        foreign_url = f"http://{HOST}:{foreign_port}"
        write_endpoint(requested, foreign_port)
        recovered = []

        def recover(workspace: Path, **kwargs: object) -> Attachment:
            recovered.append(workspace)
            return Attachment(requested_url, HttpClient(requested_url), workspace=workspace)

        monkeypatch.setattr("zicato.tui.service._spawn", recover)
        attachment = attach(url=None, workspace=requested)
        assert attachment.url == requested_url
        assert attachment.workspace == requested.resolve()
        assert recovered == [requested.resolve()]
        attachment.close()
        assert HttpClient(foreign_url).get("/api/health")["workspace"] == str(foreign)
        write_endpoint(requested, requested_port)
        recovered.clear()
        attachment = attach(url=None, workspace=requested)
        assert attachment.url == requested_url
        assert not attachment.owned
        assert recovered == []
        attachment.close()
        assert HttpClient(requested_url).get("/api/health")["workspace"] == str(requested)


def test_health_publishes_an_absolute_workspace_from_a_relative_server_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from zicato.dashboard.server import create_app

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    app = create_app(Path(".zicato"), static_dir=tmp_path / "absent-static")
    assert TestClient(app).get("/api/health").json()["workspace"] == str(workspace)


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


@pytest.mark.parametrize("path_form", ["absolute", "parent-components", "symlink"])
def test_attach_prefers_a_running_service_over_starting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_form: str
) -> None:
    ws = tmp_path / ".zicato"
    write_endpoint(ws, 7899)
    served = str(ws)
    if path_form == "parent-components":
        served += "/../.zicato"
    elif path_form == "symlink":
        alias = tmp_path / "workspace-alias"
        alias.symlink_to(ws, target_is_directory=True)
        served = str(alias)
    monkeypatch.setattr(
        "zicato.tui.client.HttpClient.get",
        lambda self, path: {"status": "ok", "workspace": served},
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("attach must not spawn when a service is already answering")

    monkeypatch.setattr("subprocess.Popen", explode)
    attachment = attach(url=None, workspace=ws)
    assert attachment.url == "http://127.0.0.1:7899"
    assert attachment.workspace == ws.resolve()
    assert attachment.owned is False  # we did not start it, so we must not stop it


@pytest.mark.parametrize(
    "health",
    [
        None,
        [],
        "ok",
        {},
        {"status": "ok"},
        {"status": "ok", "workspace": None},
        {"status": "ok", "workspace": 12},
        {"status": "ok", "workspace": ""},
        {"status": "ok", "workspace": ".zicato"},
        {"status": "ok", "workspace": "/bad\0path"},
    ],
)
def test_missing_or_malformed_identity_uses_unavailable_endpoint_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health: object
) -> None:
    write_endpoint(tmp_path, 7899)
    recovered = Attachment("", HttpClient(""))
    monkeypatch.setattr("zicato.tui.client.HttpClient.get", lambda self, path: health)
    monkeypatch.setattr("zicato.tui.service._spawn", lambda *args, **kwargs: recovered)
    assert attach(url=None, workspace=tmp_path) is recovered


def test_spawn_discovery_rechecks_identity_until_the_requested_service_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningProc:
        def poll(self) -> None:
            return None

    proc = RunningProc()
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("zicato.tui.service.read_endpoint", lambda path: "http://127.0.0.1:7899")
    responses = iter(
        [
            {"status": "ok"},
            {"status": "ok", "workspace": "/another-workspace"},
            {"status": "ok", "workspace": str(tmp_path)},
        ]
    )
    observed = []

    def health(self: HttpClient, path: str) -> object:
        payload = next(responses)
        observed.append(payload)
        return payload

    monkeypatch.setattr("zicato.tui.client.HttpClient._fetch", health)
    from zicato.tui.service import _spawn

    attachment = _spawn(tmp_path, port=7899, timeout=1, sleep=0.001)
    assert len(observed) == 3
    assert attachment.workspace == tmp_path
    assert attachment.process is proc


def test_unverified_spawn_times_out_and_closes_only_its_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningProc:
        def poll(self) -> None:
            return None

    proc = RunningProc()
    closed = []
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("zicato.tui.service.read_endpoint", lambda path: "http://127.0.0.1:7899")
    monkeypatch.setattr(
        "zicato.tui.client.HttpClient.get",
        lambda self, path: {"status": "ok", "workspace": "/another-workspace"},
    )
    monkeypatch.setattr(Attachment, "close", lambda self: closed.append(self.process))
    from zicato.tui.service import _spawn

    with pytest.raises(ServiceError, match="did not report a bound port in time"):
        _spawn(tmp_path, port=7899, timeout=0.01, sleep=0.001)
    assert closed == [proc]


def test_explicit_url_selects_its_service_without_local_workspace_equality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "zicato.tui.client.HttpClient.get",
        lambda self, path: {"status": "ok", "workspace": "/operator-selected-workspace"},
    )
    attachment = attach(url="http://127.0.0.1:7899", workspace=tmp_path)
    assert attachment.url == "http://127.0.0.1:7899"
    assert attachment.workspace is None
    assert not attachment.owned


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
