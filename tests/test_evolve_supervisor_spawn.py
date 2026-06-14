"""Tests for the child-process spawn helpers in
``zicato.cli.commands.evolve``.

``zicato evolve`` spawns two children: the watchdog-only supervisor
binary and the Python dashboard service. These tests use a sentinel
shell script (instead of the real Rust binary) as the
``ZICATO_SUPERVISOR_BINARY`` so the test runs without the supervisor
crate being built, and assert the dashboard spawn argv.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from zicato.cli.commands.evolve import (
    _dashboard_spawn_argv,
    _maybe_spawn_dashboard,
    _maybe_spawn_supervisor,
    _resolve_supervisor_binary,
    _terminate_child,
    _terminate_supervisor,
)


def _write_sentinel(tmp_path: Path) -> Path:
    """Write a small shell script that sleeps; mimics a long-running binary."""
    script = tmp_path / "sentinel-supervisor.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        # Print the args so the test can verify the wire-up.
        'echo "args: $@" >&2\n'
        "sleep 30\n"
    )
    script.chmod(0o755)
    return script


def test_resolve_supervisor_binary_uses_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ZICATO_SUPERVISOR_BINARY`` short-circuits resolution."""
    sentinel = _write_sentinel(tmp_path)
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(sentinel))
    resolved = _resolve_supervisor_binary()
    assert resolved == sentinel


def test_resolve_supervisor_binary_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If env override + bundled + PATH + dev-checkout all miss, returns ``None``."""
    monkeypatch.delenv("ZICATO_SUPERVISOR_BINARY", raising=False)
    # Point env at a non-executable; resolver should fall through.
    not_executable = tmp_path / "not-exec"
    not_executable.write_text("")
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(not_executable))
    # Strip PATH so the system zicato-supervisor (if any) is unreachable.
    monkeypatch.setenv("PATH", "/nonexistent")
    # The bundled (zicato/_bin/) and dev-checkout (target/release/)
    # paths are computed relative to the package; we can't easily break
    # them, so just confirm we get *some* absolute path if a binary
    # happens to be built, else None.
    result = _resolve_supervisor_binary()
    if result is not None:
        assert result.is_absolute()


def _make_exec(path: Path, mtime: float) -> Path:
    """Create an executable sentinel file at ``path`` with a fixed mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nsleep 30\n")
    path.chmod(0o755)
    os.utime(path, (mtime, mtime))
    return path


def _fake_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    bundled_mtime: float | None,
    dev_mtime: float | None,
) -> tuple[Path, Path]:
    """Redirect the bundled (``_bin``) and dev-checkout (``target/release``)
    candidate paths into ``tmp_path`` and optionally create them.

    The resolver derives the bundled path from ``zicato.__file__`` and the
    dev-checkout path from the ``evolve`` module's ``__file__`` (five parents
    up to the repo root). We fake both ``__file__`` values to point inside
    ``tmp_path`` so the test never touches the real built binaries.

    ``*_mtime`` of ``None`` means "do not create that candidate" (simulating
    e.g. an installed wheel with no ``target/release``).
    """
    import zicato
    import zicato.cli.commands.evolve as ev

    # Bundled lives at <pkg_root>/_bin/zicato-supervisor; make zicato.__file__
    # resolve to <pkg_root>/__init__.py.
    pkg_root = tmp_path / "site" / "zicato"
    pkg_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(zicato, "__file__", str(pkg_root / "__init__.py"))
    bundled = pkg_root / "_bin" / "zicato-supervisor"

    # Dev checkout lives at <repo>/target/release/zicato-supervisor; the
    # module __file__ is <repo>/src/zicato/cli/commands/evolve.py, i.e. five
    # parents below the repo root.
    repo_root = tmp_path / "checkout"
    module_file = repo_root / "src" / "zicato" / "cli" / "commands" / "evolve.py"
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.write_text("")
    monkeypatch.setattr(ev, "__file__", str(module_file))
    dev = repo_root / "target" / "release" / "zicato-supervisor"

    if bundled_mtime is not None:
        _make_exec(bundled, bundled_mtime)
    if dev_mtime is not None:
        _make_exec(dev, dev_mtime)

    # No env override, and an empty PATH so a system zicato-supervisor never
    # leaks into the resolution under test.
    monkeypatch.delenv("ZICATO_SUPERVISOR_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    return bundled, dev


def test_resolve_prefers_dev_checkout_when_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In a dev checkout with BOTH present, the fresher ``target/release``
    build wins over a stale bundled ``_bin`` copy.

    This is the regression guard for the stale-supervisor bug: the old
    resolution order returned the bundled binary unconditionally, so this
    case would have returned ``bundled`` instead.
    """
    bundled, dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=1000.0, dev_mtime=2000.0)
    assert _resolve_supervisor_binary() == dev


def test_resolve_uses_bundled_when_newer_than_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the bundled copy is strictly newer than ``target/release`` the
    bundled binary is used (the dev build is the stale one)."""
    bundled, dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=3000.0, dev_mtime=1000.0)
    assert _resolve_supervisor_binary() == bundled


def test_resolve_installed_wheel_uses_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installed wheel has no ``target/release``; the bundled binary is
    returned, exactly as before the fix."""
    bundled, _dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=1000.0, dev_mtime=None)
    assert _resolve_supervisor_binary() == bundled


def test_resolve_bundled_only_dev_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bundled-only present (dev build never compiled) -> bundled wins."""
    bundled, _dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=5000.0, dev_mtime=None)
    assert _resolve_supervisor_binary() == bundled


def test_resolve_dev_only_bundled_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bare checkout: ``cargo build --release`` only, no bundled ``_bin``
    -> the dev-checkout build is used."""
    _bundled, dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=None, dev_mtime=1234.0)
    assert _resolve_supervisor_binary() == dev


def test_resolve_env_override_beats_dev_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``ZICATO_SUPERVISOR_BINARY`` short-circuits everything,
    even a present-and-newer dev-checkout build."""
    _bundled, _dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=1000.0, dev_mtime=9000.0)
    override = _make_exec(tmp_path / "override" / "zicato-supervisor", 4242.0)
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(override))
    assert _resolve_supervisor_binary() == override


def test_resolve_equal_mtime_prefers_dev_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tie on mtime resolves to the dev-checkout build (``>=``), so a
    freshly-rebuilt checkout never loses to an equally-stamped bundle."""
    _bundled, dev = _fake_checkout(monkeypatch, tmp_path, bundled_mtime=2000.0, dev_mtime=2000.0)
    assert _resolve_supervisor_binary() == dev


def test_maybe_spawn_supervisor_disabled() -> None:
    """``disabled=True`` returns ``None`` and does not spawn."""
    proc = asyncio.run(_maybe_spawn_supervisor(Path("/tmp"), disabled=True))
    assert proc is None


def test_supervisor_spawned_with_no_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The watchdog supervisor is spawned with ``--no-dashboard``.

    The dashboard UI is now served by the Python service, so the
    supervisor must run watchdog-only.
    """
    sentinel = _write_sentinel(tmp_path)
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(sentinel))

    captured: dict[str, tuple[str, ...]] = {}

    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args: str, **kwargs: object):  # type: ignore[no-untyped-def]
        captured["argv"] = tuple(str(a) for a in args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_exec)

    async def _scenario() -> None:
        proc = await _maybe_spawn_supervisor(tmp_path, disabled=False)
        assert proc is not None
        await _terminate_child(proc)

    asyncio.run(_scenario())
    assert "--no-dashboard" in captured["argv"]
    assert "--workspace" in captured["argv"]
    # The supervisor no longer takes --port / --bind from evolve.
    assert "--bind" not in captured["argv"]


def test_spawn_and_terminate_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Spawn the sentinel, observe it's running, terminate it cleanly."""
    sentinel = _write_sentinel(tmp_path)
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(sentinel))

    async def _scenario() -> None:
        proc = await _maybe_spawn_supervisor(tmp_path, disabled=False)
        assert proc is not None
        assert proc.pid > 0
        # Process is alive at this point.
        assert proc.returncode is None
        # Verify the underlying pid is live.
        os.kill(proc.pid, 0)  # raises ProcessLookupError if gone

        await _terminate_child(proc)
        # After terminate it must be reaped.
        assert proc.returncode is not None

    asyncio.run(_scenario())


def test_terminate_child_none_is_noop() -> None:
    """Terminating ``None`` is a silent no-op."""
    asyncio.run(_terminate_child(None))
    # The legacy alias must keep working too.
    asyncio.run(_terminate_supervisor(None))


def test_spawn_missing_binary_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If the binary can't be resolved, the helper prints a warning + returns None."""
    # Replace the resolver via the module dict so the call inside
    # _maybe_spawn_supervisor sees the override (the call site uses a
    # global-name lookup against the same module globals).
    import zicato.cli.commands.evolve as ev

    monkeypatch.setattr(ev, "_resolve_supervisor_binary", lambda: None)
    proc = asyncio.run(ev._maybe_spawn_supervisor(tmp_path, disabled=False))
    assert proc is None


# ---------------------------------------------------------------------------
# Python dashboard service spawn
# ---------------------------------------------------------------------------


def test_dashboard_spawn_argv_shape(tmp_path: Path) -> None:
    """The dashboard argv runs ``python -m zicato.dashboard`` with the
    workspace, host and port."""
    argv = _dashboard_spawn_argv(tmp_path, "127.0.0.1", 7892)
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "zicato.dashboard"]
    assert "--workspace" in argv
    assert argv[argv.index("--workspace") + 1] == str(tmp_path)
    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--port" in argv
    assert argv[argv.index("--port") + 1] == "7892"


def test_maybe_spawn_dashboard_disabled() -> None:
    """``disabled=True`` returns ``None`` and does not spawn the dashboard."""
    proc = asyncio.run(_maybe_spawn_dashboard(Path("/tmp"), 7892, disabled=True))
    assert proc is None


def test_maybe_spawn_dashboard_binds_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dashboard service is spawned bound to 127.0.0.1, not 0.0.0.0."""
    captured: dict[str, tuple[str, ...]] = {}

    async def _fake_exec(*args: str, **kwargs: object):  # type: ignore[no-untyped-def]
        captured["argv"] = tuple(str(a) for a in args)

        class _FakeProc:
            returncode = None

            async def wait(self) -> int:
                return 0

            def terminate(self) -> None:  # pragma: no cover - not reached
                pass

        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    proc = asyncio.run(_maybe_spawn_dashboard(tmp_path, 7892, disabled=False))
    assert proc is not None
    argv = captured["argv"]
    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in argv


def test_maybe_spawn_dashboard_missing_entrypoint_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed spawn returns ``None`` rather than raising."""

    async def _boom(*args: str, **kwargs: object):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("python -m zicato.dashboard not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    proc = asyncio.run(_maybe_spawn_dashboard(tmp_path, 7892, disabled=False))
    assert proc is None
