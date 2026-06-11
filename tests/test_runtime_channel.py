"""Tests for ``zicato.runtime.channel`` — the Channel abstraction (Phase 1).

Covers the two channel shapes built in RUNTIME-V2 Phase 1:

* :class:`EventLog` — append-only, single-writer, monotonic gap-free ``seq``,
  cursor reads, tail, atomicity of each append. Exercised against both the
  file and the in-memory storage backend (the ``seq``/cursor contract is
  backend-independent).
* :class:`CommandQueue` — many-writer enqueue, single/concurrent-consumer
  **claim-once** via atomic move. File-backed (the claim-once guarantee
  rides on filesystem rename atomicity), so exercised against the file
  backend + a real workspace root.

Nothing here touches an existing channel — Phase 1 is purely additive.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zicato.runtime.channel import Command, CommandQueue, Event, EventLog
from zicato.storage import (
    FileStorageBackend,
    InMemoryStorageBackend,
    StorageBackend,
)

# ---------------------------------------------------------------------------
# EventLog — run against both backends (the seq/cursor contract is shared)
# ---------------------------------------------------------------------------


def _make_file_backend(tmp_path: Path) -> StorageBackend:
    backend = FileStorageBackend(tmp_path / "ws")
    backend.start()
    return backend


def _make_memory_backend(_tmp_path: Path) -> StorageBackend:
    return InMemoryStorageBackend()


BACKEND_BUILDERS: dict[str, Callable[[Path], StorageBackend]] = {
    "files": _make_file_backend,
    "memory": _make_memory_backend,
}


@pytest.fixture(params=list(BACKEND_BUILDERS), ids=list(BACKEND_BUILDERS))
def backend(request, tmp_path: Path) -> StorageBackend:
    return BACKEND_BUILDERS[request.param](tmp_path)


def test_eventlog_empty_reads(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    assert log.tail() is None
    assert log.read() == []
    assert log.read(from_seq=5) == []


def test_eventlog_append_returns_event_with_seq_and_ts(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    event = log.append("Started", {"structure": "gauntlet"})
    assert isinstance(event, Event)
    assert event.seq == 1
    assert event.type == "Started"
    assert event.payload == {"structure": "gauntlet"}
    assert event.ts.endswith("Z")


def test_eventlog_seq_is_monotonic_and_gap_free(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    seqs = [log.append("E", {"i": i}).seq for i in range(10)]
    assert seqs == list(range(1, 11))


def test_eventlog_seq_continues_across_fresh_handles(backend: StorageBackend) -> None:
    # The seq is derived from the on-disk tail, not in-memory state, so a
    # brand-new handle onto the same key keeps counting where the log left
    # off (a new process attaching to an existing log).
    key = "runtime/test_log.jsonl"
    EventLog(backend, key).append("A")
    EventLog(backend, key).append("B")
    third = EventLog(backend, key).append("C")
    assert third.seq == 3


def test_eventlog_read_returns_events_in_append_order(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    log.append("A", 1)
    log.append("B", 2)
    log.append("C", 3)
    events = log.read()
    assert [(e.seq, e.type, e.payload) for e in events] == [
        (1, "A", 1),
        (2, "B", 2),
        (3, "C", 3),
    ]


def test_eventlog_read_from_seq_is_a_cursor(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    for i in range(5):
        log.append("E", i)
    # A consumer that has folded up to seq=2 reads only the tail.
    fresh = log.read(from_seq=2)
    assert [e.seq for e in fresh] == [3, 4, 5]
    # Cursor at the head of the log returns nothing.
    assert log.read(from_seq=5) == []


def test_eventlog_tail_is_last_event(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    log.append("A")
    log.append("B")
    last = log.append("Settled", {"decision": "promote"})
    tail = log.tail()
    assert tail is not None
    assert tail.seq == last.seq
    assert tail.type == "Settled"
    assert tail.payload == {"decision": "promote"}


def test_eventlog_payload_roundtrips_nested_json(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    payload = {"sides": ["a", "b"], "meta": {"n": 3, "flags": [True, None]}}
    log.append("MatchupStarted", payload)
    [event] = log.read()
    assert event.payload == payload


def test_eventlog_append_none_payload(backend: StorageBackend) -> None:
    log = EventLog(backend, "runtime/test_log.jsonl")
    event = log.append("Tick")
    assert event.payload is None
    [read_back] = log.read()
    assert read_back.payload is None


def test_eventlog_separate_keys_are_independent(backend: StorageBackend) -> None:
    a = EventLog(backend, "runtime/log_a.jsonl")
    b = EventLog(backend, "runtime/log_b.jsonl")
    a.append("X")
    a.append("X")
    b.append("Y")
    assert [e.seq for e in a.read()] == [1, 2]
    assert [e.seq for e in b.read()] == [1]  # independent seq space


# ---------------------------------------------------------------------------
# EventLog — on-disk shape + atomicity (file backend specifically)
# ---------------------------------------------------------------------------


def test_eventlog_on_disk_is_one_complete_line_per_event(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    backend = FileStorageBackend(root)
    backend.start()
    log = EventLog(backend, "runtime/test_log.jsonl")
    log.append("A", {"k": 1})
    log.append("B", {"k": 2})

    raw = (root / "runtime" / "test_log.jsonl").read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 2
    # Every line is a complete, parseable JSON record — never a torn mix.
    records = [json.loads(ln) for ln in lines]
    assert [r["seq"] for r in records] == [1, 2]
    assert all(set(r) == {"seq", "ts", "type", "payload"} for r in records)


def test_eventlog_reader_sees_a_prefix_during_concurrent_append(tmp_path: Path) -> None:
    # An interleaved reader observes a clean prefix of the log — every event
    # it sees is whole (one complete line per append), never a half-record.
    backend = FileStorageBackend(tmp_path / "ws")
    backend.start()
    log = EventLog(backend, "runtime/test_log.jsonl")
    seen_max = 0
    for i in range(50):
        log.append("E", i)
        events = log.read()
        # Whatever the reader sees is a contiguous gap-free prefix.
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        seen_max = max(seen_max, len(events))
    assert seen_max == 50


# ---------------------------------------------------------------------------
# CommandQueue — file-backed, claim-once
# ---------------------------------------------------------------------------


def _file_queue(tmp_path: Path, prefix: str = "runtime/control_q") -> CommandQueue:
    root = tmp_path / "ws"
    backend = FileStorageBackend(root)
    backend.start()
    return CommandQueue(backend, root, prefix=prefix)


def test_queue_empty_claim_is_none(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    assert q.claim() is None
    assert q.pending() == []
    assert q.archived() == []


def test_queue_enqueue_then_claim_roundtrips(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    enq = q.enqueue("pause_epoch")
    assert isinstance(enq, Command)
    assert enq.type == "pause_epoch"
    assert enq.ts.endswith("Z")

    claimed = q.claim()
    assert claimed is not None
    assert claimed.id == enq.id
    assert claimed.type == "pause_epoch"


def test_queue_claim_is_fifo_for_single_enqueuer(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    q.enqueue("a")
    q.enqueue("b")
    q.enqueue("c")
    drained = [q.claim(), q.claim(), q.claim()]
    assert [c.type for c in drained if c is not None] == ["a", "b", "c"]
    assert q.claim() is None


def test_queue_payload_roundtrips(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    q.enqueue("rubric_replacement", {"text": "new rubric body", "epoch": 4})
    claimed = q.claim()
    assert claimed is not None
    assert claimed.payload == {"text": "new rubric body", "epoch": 4}


def test_queue_claim_once_each_command_fires_exactly_once(tmp_path: Path) -> None:
    # Enqueue N, claim until drained — each command surfaces exactly once.
    q = _file_queue(tmp_path)
    for i in range(20):
        q.enqueue("cmd", i)
    seen = []
    while (c := q.claim()) is not None:
        seen.append(c.payload)
    assert sorted(seen) == list(range(20))
    assert len(seen) == 20  # no duplicates


def test_queue_claim_archives_the_command(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    enq = q.enqueue("promote", {"gen": "v7"})
    assert [c.id for c in q.pending()] == [enq.id]
    assert q.archived() == []

    q.claim()
    # Claimed command leaves pending and lands in the archive (audit trail).
    assert q.pending() == []
    archived = q.archived()
    assert [c.id for c in archived] == [enq.id]
    assert archived[0].payload == {"gen": "v7"}


def test_queue_pending_is_read_only_peek(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    q.enqueue("a")
    q.enqueue("b")
    # Peeking twice does not consume.
    assert [c.type for c in q.pending()] == ["a", "b"]
    assert [c.type for c in q.pending()] == ["a", "b"]
    # And a claim still sees both.
    assert q.claim() is not None
    assert q.claim() is not None
    assert q.claim() is None


def test_queue_distinct_ids_under_burst_enqueue(tmp_path: Path) -> None:
    q = _file_queue(tmp_path)
    ids = {q.enqueue("cmd", i).id for i in range(100)}
    assert len(ids) == 100  # ordinal + uuid keeps every id unique
    assert len(q.pending()) == 100


def test_queue_concurrent_claim_once_no_command_fires_twice(tmp_path: Path) -> None:
    # Several consumers race to drain one queue. Claim-once must hold: the
    # union of what every consumer claimed is the full set, with no overlap.
    root = tmp_path / "ws"
    backend = FileStorageBackend(root)
    backend.start()
    producer = CommandQueue(backend, root, prefix="runtime/control_q")

    n = 200
    for i in range(n):
        producer.enqueue("cmd", i)

    def drain() -> list[int]:
        # Each consumer is its own handle on the same queue files.
        consumer = CommandQueue(backend, root, prefix="runtime/control_q")
        got: list[int] = []
        while (c := consumer.claim()) is not None:
            got.append(c.payload)
        return got

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: drain(), range(8)))

    all_claimed = [payload for batch in results for payload in batch]
    # Exactly once: every command claimed, none twice.
    assert sorted(all_claimed) == list(range(n))
    assert len(all_claimed) == n


def test_queue_concurrent_enqueue_all_land(tmp_path: Path) -> None:
    # Many writers enqueue concurrently; every command lands with a distinct
    # id (no overwrite, no lost enqueue).
    root = tmp_path / "ws"
    backend = FileStorageBackend(root)
    backend.start()

    def enqueue_batch(writer: int) -> list[str]:
        q = CommandQueue(backend, root, prefix="runtime/control_q")
        return [q.enqueue("cmd", {"w": writer, "i": i}).id for i in range(25)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(enqueue_batch, range(8)))

    enqueued_ids = {cid for batch in batches for cid in batch}
    landed = CommandQueue(backend, root, prefix="runtime/control_q").pending()
    assert len(landed) == 200
    assert {c.id for c in landed} == enqueued_ids  # none lost, none duplicated


def test_queue_separate_prefixes_are_isolated(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    backend = FileStorageBackend(root)
    backend.start()
    kills = CommandQueue(backend, root, prefix="runtime/kill_q")
    control = CommandQueue(backend, root, prefix="runtime/control_q")
    kills.enqueue("kill", "run_a")
    control.enqueue("pause")
    # Each queue only sees its own commands.
    assert [c.type for c in kills.pending()] == ["kill"]
    assert [c.type for c in control.pending()] == ["pause"]
    claimed = kills.claim()
    assert claimed is not None and claimed.type == "kill"
    assert control.claim() is not None  # control's command is untouched
