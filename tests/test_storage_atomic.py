"""Tests for :mod:`zicato.storage._atomic` — durability upgrades.

The directory-fsync upgrade cannot be power-loss-tested from userspace;
these tests pin the BEHAVIOURAL contract instead: writes and claims
still land correctly, the parent directory really is fsynced (observed
via monkeypatch), and a directory that cannot be fsynced degrades
silently rather than failing the write.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zicato.storage._atomic import (
    atomic_claim,
    atomic_write_json,
    atomic_write_text,
    read_json,
)

#: ``F_GETPATH`` — the Darwin fcntl that fills a buffer with an open fd's
#: path. Only macOS builds of CPython define ``fcntl.F_GETPATH``, so the
#: literal is the fallback for a build that omits it; ``50`` is the value
#: from Darwin's ``sys/fcntl.h`` and has been stable across releases.
_F_GETPATH = 50
#: ``MAXPATHLEN`` on Darwin — the buffer ``F_GETPATH`` writes into.
_MAXPATHLEN = 1024


def _fd_path(fd: int) -> Path:
    """Best-effort path for an open fd, used only by fsync spy tests.

    Linux resolves it through ``/proc``; macOS has no ``/proc``, so the
    Darwin branch asks the kernel directly. The branch is unreachable on
    the CI platform, which is why it is kept to one syscall.
    """
    proc_path = Path(f"/proc/self/fd/{fd}")
    if proc_path.exists():
        return Path(os.readlink(proc_path))

    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        import fcntl

        raw = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", _F_GETPATH), bytes(_MAXPATHLEN))
        # The kernel writes a NUL-terminated path into the buffer and hands
        # back the whole buffer, trailing padding included.
        path = raw.split(b"\0", 1)[0].decode()
        if path:
            return Path(path)

    raise OSError(f"cannot resolve path for fd {fd}")


def test_atomic_write_text_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "state.json"
    atomic_write_text(target, '{"v": 1}')
    assert target.read_text(encoding="utf-8") == '{"v": 1}'
    # No .tmp residue.
    assert list(target.parent.iterdir()) == [target]


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"b": 2, "a": 1})
    assert read_json(target) == {"a": 1, "b": 2}


def test_atomic_write_fsyncs_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the rename, the PARENT DIRECTORY fd is fsynced.

    The rename's durability depends on the directory entry reaching
    disk; this observes the fsync call against a directory fd (an fd
    whose ``os.fstat`` mode is a directory) landing after the write.
    """
    synced_dirs: list[Path] = []
    real_fsync = os.fsync

    def spying_fsync(fd: int) -> None:
        import stat

        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_dirs.append(_fd_path(fd))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spying_fsync)
    target = tmp_path / "sub" / "state.json"
    atomic_write_text(target, "payload")
    assert target.read_text(encoding="utf-8") == "payload"
    assert target.parent.resolve() in [p.resolve() for p in synced_dirs]


def test_atomic_write_survives_unfsyncable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that rejects fsync must not fail the write."""
    import stat

    real_fsync = os.fsync

    def failing_dir_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync unsupported here")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_dir_fsync)
    target = tmp_path / "state.json"
    atomic_write_text(target, "still lands")
    assert target.read_text(encoding="utf-8") == "still lands"


def test_atomic_claim_moves_exactly_once(tmp_path: Path) -> None:
    src = tmp_path / "queue" / "cmd.json"
    src.parent.mkdir()
    src.write_text("{}", encoding="utf-8")
    dst = tmp_path / "claimed" / "cmd.json"

    assert atomic_claim(src, dst) is True
    assert not src.exists()
    assert dst.is_file()
    # The second claimer observes the source already gone.
    assert atomic_claim(src, tmp_path / "claimed2" / "cmd.json") is False


def test_atomic_claim_fsyncs_both_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful claim fsyncs the destination AND source directories."""
    import stat

    synced_dirs: list[Path] = []
    real_fsync = os.fsync

    def spying_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_dirs.append(_fd_path(fd).resolve())
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spying_fsync)
    src = tmp_path / "queue" / "cmd.json"
    src.parent.mkdir()
    src.write_text("{}", encoding="utf-8")
    dst = tmp_path / "claimed" / "cmd.json"
    assert atomic_claim(src, dst) is True
    assert dst.parent.resolve() in synced_dirs
    assert src.parent.resolve() in synced_dirs


def test_atomic_claim_missing_source_fsyncs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost claim race is a pure no-op — no directory fsync fires."""
    import stat

    synced = []
    real_fsync = os.fsync

    def spying_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spying_fsync)
    assert atomic_claim(tmp_path / "ghost.json", tmp_path / "claimed" / "x.json") is False
    assert synced == []
