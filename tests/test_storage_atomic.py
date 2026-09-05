"""Record replacement preserves complete payloads across races and failures."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread
from uuid import UUID

import pytest

import zicato.storage._atomic as atomic_helpers
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


def test_atomic_write_completes_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write

    def short_write(fd: int, content: bytes) -> int:
        return real_write(fd, content[:3])

    monkeypatch.setattr(os, "write", short_write)
    target = tmp_path / "state.json"
    payload = '{"message": "complete résumé"}'
    atomic_write_text(target, payload)
    assert target.read_text(encoding="utf-8") == payload
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_retries_a_temporary_name_collision_without_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    identifiers = iter([UUID(int=0), UUID(int=1)])
    other_temporary = tmp_path / f"state.json.{UUID(int=0).hex}.tmp"
    other_temporary.write_text("another operation", encoding="utf-8")
    monkeypatch.setattr(atomic_helpers, "uuid4", lambda: next(identifiers))

    atomic_write_text(target, "replacement")

    assert target.read_text(encoding="utf-8") == "replacement"
    assert other_temporary.read_text(encoding="utf-8") == "another operation"
    assert set(tmp_path.iterdir()) == {target, other_temporary}


@pytest.mark.parametrize("failure", ["write", "zero_write", "fsync", "replace"])
def test_atomic_write_failure_preserves_committed_file_and_other_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / "state.json"
    target.write_text("committed", encoding="utf-8")
    other_temporary = tmp_path / "state.json.tmp"
    other_temporary.write_text("another operation", encoding="utf-8")
    real_write = os.write
    written = False

    def failing_write(fd: int, content: bytes) -> int:
        nonlocal written
        if failure == "zero_write":
            return 0
        if written:
            raise OSError("injected write failure")
        written = True
        return real_write(fd, content[:2])

    def fail(*args: object) -> None:
        raise OSError("injected publication failure")

    if failure in {"write", "zero_write"}:
        monkeypatch.setattr(os, "write", failing_write)
    else:
        monkeypatch.setattr(os, failure, fail)

    with pytest.raises(OSError):
        atomic_write_text(target, "replacement")

    assert target.read_text(encoding="utf-8") == "committed"
    assert other_temporary.read_text(encoding="utf-8") == "another operation"
    assert set(tmp_path.iterdir()) == {target, other_temporary}


def test_competing_atomic_writers_publish_complete_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read after each rename while the other writer still owns an open file."""
    target = tmp_path / "state.json"
    target.write_text("committed", encoding="utf-8")
    first_ready, second_open = Event(), Event()
    publish_first, write_second = Event(), Event()
    real_write, real_replace = os.write, os.replace

    def controlled_write(fd: int, content: bytes) -> int:
        if current_thread().name == "record-writer_1":
            second_open.set()
            assert write_second.wait(5), "second writer was not released"
        return real_write(fd, content)

    def controlled_replace(source: Path, destination: Path) -> None:
        if current_thread().name == "record-writer_0":
            first_ready.set()
            assert publish_first.wait(5), "first writer was not released"
        real_replace(source, destination)

    monkeypatch.setattr(os, "write", controlled_write)
    monkeypatch.setattr(os, "replace", controlled_replace)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="record-writer") as workers:
        first = workers.submit(atomic_write_text, target, '{"writer": "first"}')
        try:
            assert first_ready.wait(5), "first writer did not reach replacement"
            second = workers.submit(atomic_write_text, target, '{"writer": "second"}')
            assert second_open.wait(5), "second writer did not open its temporary file"
            assert target.read_text(encoding="utf-8") == "committed"
            publish_first.set()
            first.result(timeout=5)
            first_publication = target.read_text(encoding="utf-8")
            write_second.set()
            second_error = second.exception(timeout=5)
        finally:
            publish_first.set()
            write_second.set()

    assert first_publication == '{"writer": "first"}'
    assert second_error is None
    assert target.read_text(encoding="utf-8") == '{"writer": "second"}'
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_synchronization_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("committed", encoding="utf-8")
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def observed_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append("directory sync")
            assert _fd_path(fd).resolve() == target.parent.resolve()
            assert target.read_text(encoding="utf-8") == "replacement"
        else:
            calls.append("file sync")
            assert _fd_path(fd).read_text(encoding="utf-8") == "replacement"
            assert target.read_text(encoding="utf-8") == "committed"
        real_fsync(fd)

    def observed_replace(source: Path, destination: Path) -> None:
        calls.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    monkeypatch.setattr(os, "replace", observed_replace)
    atomic_write_text(target, "replacement")
    assert calls == ["file sync", "replace", "directory sync"]


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
