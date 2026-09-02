"""Tests for the structured logging stream (``zicato.logging_stream``).

Covers the handler round-trip (record → JSONL line → reader), the
contextvars binding, retention pruning, the worker-boundary shared-file
append, and the honest degrade on a workspace with no logs. See
docs/design/LOGGING.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from zicato import logging_stream as ls
from zicato.logging_stream import (
    ZICATO_LOGGER_NAME,
    JsonlStreamHandler,
    current_log_stream_path,
    install_log_stream,
    install_worker_log_stream,
    list_stream_files,
    prune_streams,
)
from zicato.query import WorkspacePaths, build_log_view


@pytest.fixture(autouse=True)
def _clean_zicato_handlers() -> Iterator[None]:
    """Guarantee no JSONL handler leaks onto the shared ``zicato`` logger."""
    yield
    logger = logging.getLogger(ZICATO_LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, JsonlStreamHandler):
            logger.removeHandler(h)
            h.close()
    ls.set_log_context(epoch_id="", generation_id="", run_id="")


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_handler_round_trip_record_to_jsonl_to_reader(tmp_path: Path) -> None:
    """A stdlib record becomes a well-formed JSONL line the reader parses."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    handle = install_log_stream(ws, pid=4242)
    log = logging.getLogger("zicato.orchestrator")
    ls.set_log_context(epoch_id="e1", generation_id="g2")
    log.info("plain %d", 7)
    log.warning("attend", extra={"fields": {"k": 1}})
    ls.set_log_context(epoch_id="", generation_id="")
    log.info("unbound")
    handle.close()

    lines = _read_lines(handle.path)
    assert [r["message"] for r in lines] == ["plain 7", "attend", "unbound"]
    # required keys always present.
    for r in lines:
        assert set(r) >= {"ts", "level", "component", "message"}
        assert r["component"] == "zicato.orchestrator"
        assert r["ts"].endswith("Z")
    # context bound onto the first two, absent on the unbound third.
    assert lines[0]["epoch_id"] == "e1" and lines[0]["generation_id"] == "g2"
    assert "epoch_id" not in lines[2]
    # structured fields ride through.
    assert lines[1]["fields"] == {"k": 1}

    # The SAME reader the CLI + dashboard use reads it back.
    view = build_log_view(WorkspacePaths(ws), limit=10)
    assert view["invocation"].endswith("-4242")
    assert [r["message"] for r in view["records"]] == ["plain 7", "attend", "unbound"]
    # The cursor is now the file's EOF byte offset (the resume point).
    assert view["cursor"] == handle.path.stat().st_size


def test_reader_level_filter_and_after_cursor(tmp_path: Path) -> None:
    """The reader filters by level and advances past a line cursor."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    handle = install_log_stream(ws, pid=1)
    log = logging.getLogger("zicato.orchestrator")
    log.info("i0")
    log.warning("w1")
    log.error("e2")
    handle.close()

    paths = WorkspacePaths(ws)
    warn_view = build_log_view(paths, limit=10, level="WARNING")
    assert [r["message"] for r in warn_view["records"]] == ["w1", "e2"]

    # after = the byte cursor PAST the first record → only records beyond it.
    full = build_log_view(paths, limit=10)
    assert [r["message"] for r in full["records"]] == ["i0", "w1", "e2"]
    first_cursor = full["records"][0]["cursor"]  # byte offset just past "i0"
    tail = build_log_view(paths, limit=10, after=first_cursor)
    assert [r["message"] for r in tail["records"]] == ["w1", "e2"]
    assert all(r["cursor"] > first_cursor for r in tail["records"])
    # The view-level cursor is the file's EOF byte offset.
    assert full["cursor"] == full["records"][-1]["cursor"]


def test_below_floor_records_are_not_captured(tmp_path: Path) -> None:
    """The default INFO floor drops DEBUG records at capture time."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    handle = install_log_stream(ws, pid=2)  # default level INFO
    log = logging.getLogger("zicato.orchestrator")
    log.debug("noise")
    log.info("kept")
    handle.close()
    assert [r["message"] for r in _read_lines(handle.path)] == ["kept"]


def test_deleted_log_level_environment_variable_has_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    monkeypatch.setenv("ZICATO_LOG_LEVEL", "DEBUG")
    handle = install_log_stream(ws, pid=31)
    log = logging.getLogger("zicato.orchestrator")
    log.debug("hidden")
    log.info("visible")
    handle.close()
    assert [row["message"] for row in _read_lines(handle.path)] == ["visible"]


def test_third_party_loggers_are_not_captured(tmp_path: Path) -> None:
    """Only ``zicato.*`` records reach the stream — not library chatter."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    handle = install_log_stream(ws, pid=3)
    logging.getLogger("httpx").warning("library chatter")
    logging.getLogger("zicato.orchestrator").warning("ours")
    handle.close()
    assert [r["message"] for r in _read_lines(handle.path)] == ["ours"]


def test_worker_boundary_shared_file_append(tmp_path: Path) -> None:
    """A worker install APPENDS to the parent's file with its own context.

    Simulates the subprocess boundary within one process: the parent
    installs, then a "worker" install points at the SAME path and appends
    fully-attributed records. All records live in the one invocation file.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    parent = install_log_stream(ws, pid=100)
    logging.getLogger("zicato.orchestrator").info("parent line")
    stream_path = current_log_stream_path()
    assert stream_path == parent.path
    parent.close()

    # The "worker" re-installs pointed at the same file (append) with the
    # full run context — as ``_tournament_worker.main`` does.
    worker = install_worker_log_stream(
        stream_path, epoch_id="e9", generation_id="g3", run_id="g3--faq"
    )
    assert worker is not None
    logging.getLogger("zicato.tournament.runner").warning("worker over budget")
    worker.close()

    lines = _read_lines(stream_path)
    assert [r["message"] for r in lines] == ["parent line", "worker over budget"]
    # the worker record is fully attributed; the parent one is not.
    assert "epoch_id" not in lines[0]
    assert lines[1]["epoch_id"] == "e9"
    assert lines[1]["generation_id"] == "g3"
    assert lines[1]["run_id"] == "g3--faq"


def test_worker_install_no_path_is_a_noop(tmp_path: Path) -> None:
    """A worker with no stream path installs nothing (stderr-only, as before)."""
    assert install_worker_log_stream(None, epoch_id="e") is None
    assert install_worker_log_stream("", epoch_id="e") is None


def test_retention_prunes_oldest_to_bound(tmp_path: Path) -> None:
    """Installing prunes so at most MAX_RETAINED_INVOCATIONS streams remain."""
    ws = tmp_path / ".zicato"
    logs = ls.logs_dir(ws)
    logs.mkdir(parents=True)
    # Seed 25 stale streams with lexically-ordered names (stamp leads).
    for i in range(25):
        (logs / f"2026010{i // 10}T00000{i % 10}Z-{i}.jsonl").write_text("{}\n", encoding="utf-8")
    assert len(list_stream_files(logs)) == 25

    handle = install_log_stream(ws, pid=999)
    # Emit once so the (lazily-opened) new stream file actually exists.
    logging.getLogger("zicato.orchestrator").warning("first line")
    handle.close()
    # 19 kept + the new one = 20 (MAX_RETAINED_INVOCATIONS).
    remaining = list_stream_files(logs)
    assert len(remaining) == ls.MAX_RETAINED_INVOCATIONS
    assert handle.path.name in {p.name for p in remaining}


def test_prune_streams_keeps_newest(tmp_path: Path) -> None:
    """``prune_streams`` deletes oldest-first and returns what it removed."""
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("a", "b", "c", "d"):
        (logs / f"2026010{name}-1.jsonl").write_text("{}\n", encoding="utf-8")
    removed = prune_streams(logs, keep=2)
    assert len(removed) == 2
    kept = {p.name for p in list_stream_files(logs)}
    assert kept == {"2026010c-1.jsonl", "2026010d-1.jsonl"}


def test_empty_workspace_degrades_honestly(tmp_path: Path) -> None:
    """No ``logs/`` dir → an empty view, never an error."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    view = build_log_view(WorkspacePaths(ws), limit=10)
    assert view == {
        "records": [],
        "cursor": None,
        "invocation": None,
        "invocations": [],
        "level": None,
    }


def test_malformed_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A torn / non-JSON line is skipped; surrounding records still read."""
    ws = tmp_path / ".zicato"
    logs = ls.logs_dir(ws)
    logs.mkdir(parents=True)
    stream = logs / "20260101T000000Z-1.jsonl"
    stream.write_text(
        '{"ts":"2026-01-01T00:00:00.000Z","level":"INFO","component":"z","message":"a"}\n'
        "{not valid json\n"
        '{"ts":"2026-01-01T00:00:01.000Z","level":"INFO","component":"z","message":"b"}\n',
        encoding="utf-8",
    )
    view = build_log_view(WorkspacePaths(ws), limit=10)
    assert [r["message"] for r in view["records"]] == ["a", "b"]
    # cursor is the file's EOF byte offset; the torn middle line is skipped
    # without disturbing it (append-only follow survives the skip).
    assert view["cursor"] == stream.stat().st_size


# ---------------------------------------------------------------------------
# Fix 1 — failure isolation: logging setup / write can NEVER fail a run.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_unwritable_logs_dir_does_not_crash_the_run(tmp_path: Path) -> None:
    """An unwritable ``logs/`` dir: ``log.warning`` must NOT raise; run continues."""
    ws = tmp_path / ".zicato"
    logs = ls.logs_dir(ws)
    logs.mkdir(parents=True)
    os.chmod(logs, 0o555)  # read+execute, NOT writable → the file open fails
    try:
        handle = install_log_stream(ws, pid=7)
        log = logging.getLogger("zicato.orchestrator")
        # The FIRST record forces the (delayed) open, which fails inside emit —
        # stdlib would raise this straight out of the caller. It must not.
        log.warning("this must not raise")
        log.info("still running")  # per-emit retry: also silent, still no raise
        handle.close()
        # No stream file was created (the dir was unwritable), reader degrades.
        assert not any(p.suffix == ".jsonl" for p in logs.iterdir())
    finally:
        os.chmod(logs, 0o755)


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_missing_logs_dir_on_readonly_root_does_not_crash(tmp_path: Path) -> None:
    """A read-only workspace root (``logs/`` cannot even be created): no crash."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    os.chmod(ws, 0o555)  # cannot mkdir logs/ under here
    try:
        # mkdir is suppressed; the handler still installs (opens lazily).
        handle = install_log_stream(ws, pid=8)
        log = logging.getLogger("zicato.orchestrator")
        log.warning("no dir, must not raise")
        handle.close()
        assert not ls.logs_dir(ws).exists()
    finally:
        os.chmod(ws, 0o755)


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_logs_dir_becoming_writable_later_flows_again(tmp_path: Path) -> None:
    """Per-emit retry: once the dir is writable, records flow with NO reinstall."""
    ws = tmp_path / ".zicato"
    logs = ls.logs_dir(ws)
    logs.mkdir(parents=True)
    os.chmod(logs, 0o555)
    try:
        handle = install_log_stream(ws, pid=9)
        log = logging.getLogger("zicato.orchestrator")
        log.warning("dropped while blocked")  # silently fails to open
        assert not any(p.suffix == ".jsonl" for p in logs.iterdir())
        # Directory becomes writable mid-run — the SAME handler retries on the
        # next emit (nothing is cached that would permanently disable it).
        os.chmod(logs, 0o755)
        log.warning("flows once writable")
        handle.close()
        lines = _read_lines(handle.path)
        assert [r["message"] for r in lines] == ["flows once writable"]
    finally:
        os.chmod(logs, 0o755)


# ---------------------------------------------------------------------------
# Fix 2 — bounded seek-based tail + byte-offset cursor.
# ---------------------------------------------------------------------------


def _write_records(stream: Path, msgs: list[str], *, pad: str = "") -> None:
    with stream.open("a", encoding="utf-8") as fh:
        for m in msgs:
            rec: dict[str, str] = {
                "ts": "2026-01-01T00:00:00.000Z",
                "level": "INFO",
                "component": "z",
                "message": m,
            }
            if pad:
                rec["pad"] = pad
            fh.write(json.dumps(rec) + "\n")


def test_large_stream_tail_is_bounded(tmp_path: Path) -> None:
    """A ~50 MB stream tails the last ``limit`` records WITHOUT reading it whole."""
    from zicato.query.log_stream import tail_records

    logs = ls.logs_dir(tmp_path / ".zicato")
    logs.mkdir(parents=True)
    stream = logs / "20260101T000000Z-1.jsonl"
    pad = "x" * 512
    n = 90_000  # ~90k * ~620B ≈ 54 MB
    with stream.open("w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-01-01T00:00:00.000Z",
                        "level": "INFO",
                        "component": "z",
                        "message": f"m{i}",
                        "pad": pad,
                    }
                )
                + "\n"
            )
    size = stream.stat().st_size
    assert size > 40 * 1024 * 1024  # genuinely large

    t0 = time.perf_counter()
    records, cursor = tail_records(stream, limit=50)
    bounded = time.perf_counter() - t0

    # Correct tail: the last 50 messages, in order, each with a byte cursor.
    assert [r["message"] for r in records] == [f"m{i}" for i in range(n - 50, n)]
    assert cursor == size
    assert all(r["cursor"] <= size for r in records)

    # Bounded: the seek-based tail does FAR less work than a whole-file read
    # (the old read_text + splitlines), which is the whole point of the fix.
    t0 = time.perf_counter()
    _ = stream.read_text(encoding="utf-8").splitlines()
    whole = time.perf_counter() - t0
    assert bounded < whole


def test_cursor_append_correctness_across_offsets(tmp_path: Path) -> None:
    """Byte-offset follow: each ``after`` resumes exactly past the last record."""
    from zicato.query.log_stream import tail_records

    logs = ls.logs_dir(tmp_path / ".zicato")
    logs.mkdir(parents=True)
    stream = logs / "20260101T000000Z-1.jsonl"

    _write_records(stream, ["a", "b", "c"])
    recs, cursor = tail_records(stream, limit=100)
    assert [r["message"] for r in recs] == ["a", "b", "c"]
    assert cursor == stream.stat().st_size

    # No new data → empty delta, cursor holds (no rewind).
    recs2, cursor2 = tail_records(stream, limit=100, after=cursor)
    assert recs2 == []
    assert cursor2 == cursor

    # Append more → the delta is EXACTLY the new records, all past the cursor.
    _write_records(stream, ["d", "e"])
    recs3, cursor3 = tail_records(stream, limit=100, after=cursor)
    assert [r["message"] for r in recs3] == ["d", "e"]
    assert cursor3 == stream.stat().st_size
    assert all(r["cursor"] > cursor for r in recs3)


def test_bounded_tail_tolerates_torn_trailing_line(tmp_path: Path) -> None:
    """A partial (no-newline) trailing write is not emitted; complete lines are."""
    from zicato.query.log_stream import tail_records

    logs = ls.logs_dir(tmp_path / ".zicato")
    logs.mkdir(parents=True)
    stream = logs / "20260101T000000Z-1.jsonl"
    good = json.dumps({"ts": "t", "level": "INFO", "component": "z", "message": "done"})
    # Two complete lines + a torn trailing record still being written (no \n).
    stream.write_text(good + "\n" + good + "\n" + '{"partial": ', encoding="utf-8")
    recs, cursor = tail_records(stream, limit=100)
    assert [r["message"] for r in recs] == ["done", "done"]
    # cursor stays the EOF byte offset; the follower re-reads from there once
    # the torn line completes.
    assert cursor == stream.stat().st_size
