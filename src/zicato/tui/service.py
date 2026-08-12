"""Attaching to a dashboard service — connect to one, or start one.

``zicato tui --url http://127.0.0.1:7892`` attaches to a service someone else
is running (an ``evolve`` loop's, typically). With no ``--url`` the TUI spawns
its own against the named workspace, using the SAME path ``evolve`` uses:
``python -m zicato.dashboard`` in its own session, with the bound port read
back from ``runtime/dashboard.json`` rather than assumed — the service walks
``+1`` when its preferred port is taken, so a guessed port is a wrong port.

The bind is loopback, always. There is no ``--dashboard-bind``: the operator
views the workspace from the host that holds it, and a TUI over SSH is a
terminal on that host, not a reason to open a port.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from zicato.tui.client import HttpClient, ServiceError

#: Loopback only — never a routable bind. Matches the dashboard / evolve rule.
HOST = "127.0.0.1"
DEFAULT_PORT = 7892


def endpoint_file(workspace_root: Path) -> Path:
    """The file the dashboard service writes its actually-bound host/port to."""
    from zicato.runtime.paths import dashboard_endpoint_path

    return dashboard_endpoint_path(workspace_root)


def read_endpoint(path: Path) -> str | None:
    """Return ``http://host:port`` from a written endpoint file, or None."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    port = record.get("port")
    host = record.get("host") or HOST
    if not isinstance(port, int):
        return None
    # A service bound to every interface is still reached over loopback.
    if host in ("0.0.0.0", "::", ""):
        host = HOST
    return f"http://{host}:{port}"


def spawn_argv(workspace_root: Path, port: int) -> list[str]:
    """The argv that launches the dashboard service for ``workspace_root``."""
    return [
        sys.executable,
        "-m",
        "zicato.dashboard",
        "--workspace",
        str(workspace_root),
        "--host",
        HOST,
        "--port",
        str(port),
    ]


@dataclass
class Attachment:
    """A live connection, plus the process to stop if the TUI started it."""

    url: str
    client: HttpClient
    process: subprocess.Popen[bytes] | None = None
    workspace: Path | None = None

    @property
    def owned(self) -> bool:
        """True when the TUI spawned the service and must tear it down."""
        return self.process is not None

    def close(self) -> None:
        """Stop an owned service. Attached-to services are left alone.

        Signals the child's own session (it is a session leader, as in
        ``evolve``), so the service's own children go with it and nothing
        outside that session is touched.
        """
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:
                return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass


def attach(
    *,
    url: str | None,
    workspace: Path | None,
    port: int = DEFAULT_PORT,
    timeout: float = 20.0,
    sleep: float = 0.1,
) -> Attachment:
    """Connect to a dashboard service, spawning one when no ``url`` is given.

    Order of preference, and each step is a deliberate one:

    1. An explicit ``--url`` wins outright — the operator named a service.
    2. An endpoint file for this workspace that answers: attach to the running
       ``evolve`` loop's dashboard rather than starting a competing one.
    3. Spawn a service, then read its endpoint file back for the real port.
    """
    if url:
        client = HttpClient(url)
        client.get("/api/health")  # fail fast, with the connect hint attached
        return Attachment(url=url, client=client)

    if workspace is None:
        raise ServiceError(
            "no service to attach to",
            hint="pass `--url <dashboard url>` or `--workspace <path>`",
        )
    workspace = workspace.resolve()

    existing = read_endpoint(endpoint_file(workspace))
    if existing:
        client = HttpClient(existing)
        try:
            client.get("/api/health")
        except ServiceError:
            pass  # a stale endpoint file from a finished run; spawn our own
        else:
            return Attachment(url=existing, client=client, workspace=workspace)

    return _spawn(workspace, port=port, timeout=timeout, sleep=sleep)


def _spawn(workspace: Path, *, port: int, timeout: float, sleep: float) -> Attachment:
    marker = endpoint_file(workspace)
    # Drop a stale endpoint file so the readback below can only ever observe
    # the service THIS call started.
    try:
        marker.unlink()
    except OSError:
        pass
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
            spawn_argv(workspace, port),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ServiceError(
            f"could not start a dashboard service ({exc})",
            hint="install the dashboard extra: `uv sync --extra dashboard`",
        ) from exc

    attachment = Attachment(url="", client=HttpClient(""), process=proc, workspace=workspace)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ServiceError(
                "the dashboard service exited before binding a port",
                hint=("check it starts standalone: " f"`zicato dashboard --workspace {workspace}`"),
            )
        found = read_endpoint(marker)
        if found:
            client = HttpClient(found)
            try:
                client.get("/api/health")
            except ServiceError:
                time.sleep(sleep)
                continue
            attachment.url = found
            attachment.client = client
            return attachment
        time.sleep(sleep)

    attachment.close()
    raise ServiceError(
        "the dashboard service did not report a bound port in time",
        hint=f"try `zicato dashboard --workspace {workspace}` and then `zicato tui --url ...`",
    )


__all__ = [
    "DEFAULT_PORT",
    "HOST",
    "Attachment",
    "attach",
    "endpoint_file",
    "read_endpoint",
    "spawn_argv",
]
