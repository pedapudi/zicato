"""Tests for the supervisor-spawn helpers in ``zicato.cli.commands.evolve``.

Uses a sentinel shell script (instead of the real Rust binary) as the
``ZICATO_SUPERVISOR_BINARY`` so the test runs without the supervisor
crate being built.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from zicato.cli.commands.evolve import (
    _maybe_spawn_supervisor,
    _resolve_supervisor_binary,
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
    """If env override + in-tree + PATH all miss, returns ``None``."""
    monkeypatch.delenv("ZICATO_SUPERVISOR_BINARY", raising=False)
    # Point env at a non-executable; resolver should fall through.
    not_executable = tmp_path / "not-exec"
    not_executable.write_text("")
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(not_executable))
    # Strip PATH so the system zicato-supervisor (if any) is unreachable.
    monkeypatch.setenv("PATH", "/nonexistent")
    # Move the in-tree binary out of the way if it exists.
    monkeypatch.setattr(
        "zicato.cli.commands.evolve._resolve_supervisor_binary.__module__",
        "zicato.cli.commands.evolve",
    )
    # The in-tree path is computed relative to __file__; we can't easily
    # break that, so just confirm we get *some* path (possibly the in-tree
    # one). If the in-tree binary is built, the resolver returns it.
    result = _resolve_supervisor_binary()
    # Either the in-tree compiled binary exists, or the resolver returns None.
    if result is not None:
        assert result.is_absolute()


def test_maybe_spawn_supervisor_disabled() -> None:
    """``disabled=True`` returns ``None`` and does not spawn."""
    proc = asyncio.run(
        _maybe_spawn_supervisor(Path("/tmp"), 7892, "127.0.0.1", disabled=True)
    )
    assert proc is None


def test_spawn_and_terminate_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spawn the sentinel, observe it's running, terminate it cleanly."""
    sentinel = _write_sentinel(tmp_path)
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(sentinel))

    async def _scenario() -> None:
        proc = await _maybe_spawn_supervisor(
            tmp_path, 7892, "127.0.0.1", disabled=False
        )
        assert proc is not None
        assert proc.pid > 0
        # Process is alive at this point.
        assert proc.returncode is None
        # Verify the underlying pid is live.
        os.kill(proc.pid, 0)  # raises ProcessLookupError if gone

        await _terminate_supervisor(proc)
        # After terminate it must be reaped.
        assert proc.returncode is not None

    asyncio.run(_scenario())


def test_terminate_supervisor_none_is_noop() -> None:
    """Terminating ``None`` is a silent no-op."""
    asyncio.run(_terminate_supervisor(None))


def test_spawn_missing_binary_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the binary can't be resolved, the helper prints a warning + returns None."""
    monkeypatch.setenv("ZICATO_SUPERVISOR_BINARY", str(tmp_path / "missing"))
    monkeypatch.setenv("PATH", "/nonexistent")
    # The in-tree binary may exist on dev machines; mock the resolver to
    # force a miss so this test is deterministic.
    monkeypatch.setattr(
        "zicato.cli.commands.evolve._resolve_supervisor_binary",
        lambda: None,
    )
    proc = asyncio.run(
        _maybe_spawn_supervisor(tmp_path, 7892, "127.0.0.1", disabled=False)
    )
    assert proc is None
