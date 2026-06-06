"""Anti-regression tests for the REAL ``dashboard.server.run`` startup path.

Two robustness fixes are exercised here without binding a socket or
starting a server (``uvicorn.run`` is monkeypatched to a no-op):

1. The harmonograf status line is printed to stdout for ALL outcomes —
   ``launched`` / ``reused`` / unavailable (with a diagnosable reason) —
   because logging is not configured this early in ``run`` and a logger
   call would silently vanish.
2. The ``Dashboard:`` banner prints the BOUND port (what ``_pick_port``
   resolved), not the requested one, so a TIME_WAIT ``+1`` bounce does
   not print a wrong URL.

These call ``server.run`` directly so they cover the exact ordering in
``run`` that silently failed live.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import zicato.dashboard.server as server_mod


class _Handle:
    """Minimal stand-in for ``WorkspaceHarmonografHandle``."""

    def __init__(self, *, web_url: str, launched: bool, reason: str = "") -> None:
        self.web_url = web_url
        self.grpc_target = ""
        self.launched = launched
        self.reason = reason

    def shutdown(self) -> None:  # pragma: no cover - trivial
        return None


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handle: _Handle,
    requested_port: int,
    bound_port: int,
) -> None:
    """Neuter the side effects of ``run`` so only the banner ordering runs."""
    # uvicorn.run is imported lazily inside run(); patch the module attr.
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    # The harmonograf handle the helper would resolve.
    monkeypatch.setattr(server_mod, "_ensure_workspace_harmonograf", lambda _root: handle)
    # Simulate a TIME_WAIT bounce: the bound port differs from requested.
    monkeypatch.setattr(server_mod, "_pick_port", lambda _h, _p, *a, **k: bound_port)
    # Don't write any endpoint file.
    monkeypatch.setattr(server_mod, "_publish_endpoint", lambda *a, **k: None)
    # create_app pulls in the static bundle / state reader; stub it out — the
    # banner ordering under test does not need a real app.
    monkeypatch.setattr(server_mod, "create_app", lambda *a, **k: _FakeApp())


class _FakeApp:
    class _State:
        pass

    def __init__(self) -> None:
        self.state = _FakeApp._State()


def _run(server: Any, *, requested_port: int) -> None:
    server.run(
        workspace_root=Path("/nonexistent-workspace"),
        host="127.0.0.1",
        port=requested_port,
        static_dir=Path("/nonexistent-static"),
    )


def test_run_prints_bound_port_not_requested(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``Dashboard:`` URL uses the BOUND port (7893), not requested (7892)."""
    handle = _Handle(web_url="http://127.0.0.1:42017", launched=True)
    _patch_run(monkeypatch, handle=handle, requested_port=7892, bound_port=7893)

    _run(server_mod, requested_port=7892)

    out = capsys.readouterr().out
    assert "Dashboard: http://127.0.0.1:7893" in out
    # The requested port must NOT be advertised as the dashboard URL.
    assert "Dashboard: http://127.0.0.1:7892" not in out


def test_run_prints_launched_harmonograf_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handle = _Handle(web_url="http://127.0.0.1:42017", launched=True)
    _patch_run(monkeypatch, handle=handle, requested_port=7892, bound_port=7893)

    _run(server_mod, requested_port=7892)

    out = capsys.readouterr().out
    assert "harmonograf: http://127.0.0.1:42017 (launched)" in out


def test_run_prints_reused_harmonograf_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handle = _Handle(web_url="http://127.0.0.1:42017", launched=False)
    _patch_run(monkeypatch, handle=handle, requested_port=7892, bound_port=7893)

    _run(server_mod, requested_port=7892)

    out = capsys.readouterr().out
    assert "harmonograf: http://127.0.0.1:42017 (reused)" in out


def test_run_prints_unavailable_harmonograf_status_with_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No-op handle (empty web_url): the status line carries the reason."""
    handle = _Handle(web_url="", launched=False, reason="workspace absent")
    _patch_run(monkeypatch, handle=handle, requested_port=7892, bound_port=7892)

    _run(server_mod, requested_port=7892)

    out = capsys.readouterr().out
    assert "harmonograf: unavailable — continuing without execution deep-links" in out
    assert "workspace absent" in out


def test_echo_status_never_raises_on_bad_handle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Printing the status line is fully isolated — a bad handle never raises."""

    class _Bad:
        @property
        def web_url(self) -> str:
            raise RuntimeError("attribute access exploded")

    # Must not raise.
    server_mod._echo_harmonograf_status(_Bad())
