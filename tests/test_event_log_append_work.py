"""Bounded round-log append work and interrupted-write recovery."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from zicato.epoch.round_log import RoundClosed, RoundLog, RoundOpened
from zicato.evolve.round_reporting import _RoundLogEmitter


def test_round_emitter_retains_writer_for_all_events(tmp_path, monkeypatch):
    seed = RoundLog(tmp_path, "epoch", 1)
    for _ in range(3):
        seed.append(RoundOpened(contract_hash="contract"))
    original = RoundLog.read
    scanned = []

    def read(log):
        records = original(log)
        scanned.append(len(records))
        return records

    monkeypatch.setattr(RoundLog, "read", read)
    emitter = _RoundLogEmitter(tmp_path, "epoch", 1)
    for _ in range(16):
        emitter.emit("round_closed", {})
    assert scanned == [3]
    assert [event.seq for event in original(seed)] == list(range(1, 20))


def test_round_emitter_recovers_bytes_after_failed_append(tmp_path, monkeypatch):
    emitter = _RoundLogEmitter(tmp_path, "epoch", 1)
    emitter.emit("round_opened", {"contract_hash": "contract"})
    path = RoundLog(tmp_path, "epoch", 1).path
    original = Path.open

    @contextmanager
    def open_with_interruption(file, mode="r", *args, **kwargs):
        with original(file, mode, *args, **kwargs) as stream:
            if file == path and mode == "a+b":
                stream.write(b'{"seq":2')
                stream.flush()
                raise OSError("interrupted append")
            yield stream

    with monkeypatch.context() as failing:
        failing.setattr(Path, "open", open_with_interruption)
        emitter.emit("round_closed", {})
    assert path.read_bytes().endswith(b'{"seq":2')
    emitter.emit("round_closed", {})
    events = RoundLog(tmp_path, "epoch", 1).read()
    assert [event.seq for event in events] == [1, 2]
    assert events[-1].event == RoundClosed()


@pytest.mark.parametrize("suffix", [b"\n", b"\n   "])
def test_resumed_writer_refuses_complete_corrupt_tail(tmp_path, suffix):
    log = RoundLog(tmp_path, "epoch", 1)
    log.append(RoundOpened(contract_hash="contract"))
    with log.path.open("ab") as stream:
        stream.write(b"not-json" + suffix)
    before = log.path.read_bytes()
    resumed = RoundLog(tmp_path, "epoch", 1)
    with pytest.raises(ValueError, match="append-only invariant"):
        resumed.read()
    with pytest.raises(ValueError, match="append-only invariant"):
        resumed.append(RoundClosed())
    assert log.path.read_bytes() == before


@pytest.mark.parametrize(
    "suffix",
    [
        b'{"seq":99,"type":"round_closed","payload":{}}',
        b'{"seq":"invalid"}',
        b'{"note":"\xe2\x82',
    ],
)
def test_round_ignores_uncommitted_bytes_before_decoding(tmp_path, suffix):
    log = RoundLog(tmp_path, "epoch", 1)
    log.append(RoundOpened(contract_hash="contract"))
    with log.path.open("ab") as stream:
        stream.write(suffix)
    resumed = RoundLog(tmp_path, "epoch", 1)
    assert [event.seq for event in resumed.read()] == [1]
    assert resumed.append(RoundClosed()).seq == 2
    assert [event.seq for event in resumed.read()] == [1, 2]
