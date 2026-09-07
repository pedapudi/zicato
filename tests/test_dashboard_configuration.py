"""Dashboard entry points serve the assets selected by their configuration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from zicato.config import resolve_configuration
from zicato.dashboard.server import create_app
from zicato.dashboard.static_assets import resolve_static_dir


@pytest.mark.parametrize("entry_point", ["command", "module"])
@pytest.mark.parametrize(
    "selection", ["relative", "absolute", "project", "flag", "missing", "default"]
)
def test_dashboard_entry_points_serve_selected_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_point: str, selection: str
) -> None:
    workspace = tmp_path / "project" / ".zicato"
    workspace.mkdir(parents=True)
    assets = workspace.parent / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("workspace assets")
    foreign = tmp_path / "other-directory"
    foreign.mkdir()
    override = foreign / "override"
    override.mkdir()
    (override / "index.html").write_text("explicit assets")
    monkeypatch.chdir(foreign)
    configured = str(assets) if selection == "absolute" else "assets"
    if selection == "missing":
        configured = "missing-assets"
    if selection != "default":
        (workspace / "config.json").write_text(
            json.dumps({"dashboard": {"static_dir": configured}})
        )
    captured = []

    def serve(*, workspace_root, static_dir, **kwargs):
        with TestClient(create_app(workspace_root, static_dir, read_only=True)) as client:
            response = client.get("/")
            if selection == "missing":
                assert client.get("/app.js").status_code == 404
        captured.append((static_dir, response.status_code, response.text))

    argv = ["--workspace", str(workspace.parent if selection == "project" else workspace)]
    if selection == "flag":
        argv += ["--static-dir", "override"]
    if entry_point == "command":
        command = import_module("zicato.cli.commands.dashboard")
        monkeypatch.setattr("zicato.dashboard.server.run", serve)
        result = CliRunner().invoke(command.dashboard_cmd, argv)
        assert result.exit_code == 0, result.output
    else:
        module = import_module("zicato.dashboard.__main__")
        monkeypatch.setattr(module, "run", serve)
        monkeypatch.setattr(sys, "argv", ["zicato.dashboard", *argv])
        module.main()
    expected = (
        override
        if selection == "flag"
        else resolve_static_dir()
        if selection == "default"
        else workspace.parent / "missing-assets"
        if selection == "missing"
        else assets
    )
    assert len(captured) == 1
    path, status, body = captured[0]
    assert path == expected
    assert status == 200
    if selection == "missing":
        assert "UI bundle was not found" in body
    if selection not in {"default", "missing"}:
        assert body == ("explicit assets" if selection == "flag" else "workspace assets")


@pytest.mark.parametrize("entry_point", ["command", "module"])
def test_dashboard_rejects_malformed_authored_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_point: str
) -> None:
    (tmp_path / "config.json").write_text('{"dashboard":{"static_dir":12}}')

    def unexpected_service(**kwargs):
        pytest.fail("invalid dashboard configuration started a service")

    argv = ["--workspace", str(tmp_path)]
    if entry_point == "command":
        command = import_module("zicato.cli.commands.dashboard")
        monkeypatch.setattr("zicato.dashboard.server.run", unexpected_service)
        result = CliRunner().invoke(command.dashboard_cmd, argv)
        assert result.exit_code != 0
        assert "config.dashboard.static_dir" in result.output
    else:
        module = import_module("zicato.dashboard.__main__")
        monkeypatch.setattr(module, "run", unexpected_service)
        monkeypatch.setattr(sys, "argv", ["zicato.dashboard", *argv])
        with pytest.raises(ValueError, match=r"config.dashboard.static_dir"):
            module.main()


@pytest.mark.parametrize("custom", [False, True])
def test_evolve_carries_accepted_assets_to_spawned_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    command = import_module("zicato.cli.commands.evolve")
    invocation_module = import_module("zicato.evolve.invocation")
    loop_module = import_module("zicato.evolve.loop")
    workspace = tmp_path / "project" / ".zicato"
    workspace.mkdir(parents=True)
    assets = workspace.parent / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("accepted assets")
    foreign = tmp_path / "child-directory"
    foreign.mkdir()
    raw = {"dashboard": {"static_dir": "assets"}} if custom else {}
    accepted = resolve_configuration(raw)
    (workspace / "config.json").write_text("invalid after configuration was captured")

    @asynccontextmanager
    async def invocation(*args, **kwargs):
        async with AsyncExitStack() as resources:
            yield SimpleNamespace(configuration=accepted, resources=resources)

    async def no_service(*args, **kwargs):
        return None

    async def no_rounds(*args, **kwargs):
        return []

    # The actual child parses the module arguments and serves an HTTP request.
    # Replacing the final server runner avoids binding a port or starting telemetry.
    probe = """
import json, runpy
from starlette.testclient import TestClient
from zicato.dashboard import server
def serve(*, workspace_root, static_dir, **kwargs):
    with TestClient(server.create_app(workspace_root, static_dir, read_only=True)) as client:
        response = client.get('/')
    print(json.dumps([str(static_dir), response.status_code, response.text]))
server.run = serve
runpy.run_module('zicato.dashboard', run_name='__main__')
"""
    results = []
    spawn = asyncio.create_subprocess_exec

    async def dashboard_process(*argv, **kwargs):
        assert argv[1:3] == ("-m", "zicato.dashboard")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        process = await spawn(
            argv[0],
            "-c",
            probe,
            *argv[3:],
            cwd=foreign,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        results.append(json.loads(stdout))
        return process

    monkeypatch.setattr(invocation_module, "validated_invocation", invocation)
    monkeypatch.setattr(loop_module, "_evolve_n_rounds", no_rounds)
    monkeypatch.setattr(command, "_maybe_spawn_supervisor", no_service)
    monkeypatch.setattr(command, "_report_dashboard_url", no_service)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", dashboard_process)
    result = CliRunner().invoke(command.evolve_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output or repr(result.exception)
    expected = assets if custom else resolve_static_dir()
    assert len(results) == 1
    path, status, body = results[0]
    assert path == str(expected)
    assert status == 200
    if custom:
        assert body == "accepted assets"
