"""Tests for ``zicato.runtime.progress_log`` — the orchestrator progress log.

RUNTIME-V2 Phase 4. The progress event log is the TRUE liveness signal:
its monotonic ``seq`` advances only on a genuine orchestrator transition
(round start, propose, apply, tournament start/settle, gate,
promote/reject), never on the heartbeat timer. A terminal marker
(SETTLED / STOPPED) lets a reader tell a settled run from a stalled one.

These tests pin:

* ``seq`` monotonicity + gap-freedom across appended transitions;
* ``tail_seq`` reads ``0`` for an absent / empty log (the back-compat
  default a pre-Phase-4 heartbeat reads back as);
* terminal detection (``is_terminal`` / ``tail_is_terminal``);
* ``clear_log`` resets the log so a fresh invocation starts from ``seq == 1``.
"""

from __future__ import annotations

from pathlib import Path

from zicato.runtime import progress_log
from zicato.runtime.paths import progress_log_path


def test_tail_seq_zero_for_absent_log(tmp_path: Path) -> None:
    """A workspace whose orchestrator never wrote a progress log reads seq 0."""
    assert progress_log.tail_seq(tmp_path) == 0
    assert progress_log.tail(tmp_path) is None
    assert progress_log.tail_is_terminal(tmp_path) is False


def test_append_returns_monotonic_gapfree_seq(tmp_path: Path) -> None:
    """Each append advances ``seq`` by exactly one, starting at 1."""
    s1 = progress_log.append_progress(tmp_path, progress_log.LOOP_START)
    s2 = progress_log.append_progress(tmp_path, progress_log.ROUND_START)
    s3 = progress_log.append_progress(tmp_path, progress_log.PROPOSE)
    s4 = progress_log.append_progress(tmp_path, progress_log.TOURNAMENT_START)
    assert [s1, s2, s3, s4] == [1, 2, 3, 4]
    # The tail tracks the latest append.
    assert progress_log.tail_seq(tmp_path) == 4


def test_seq_advances_only_on_append_not_on_reread(tmp_path: Path) -> None:
    """Re-reading the tail does NOT advance ``seq`` — only an append does.

    This is the whole point of the signal: a reader (watchdog / dashboard)
    polling ``tail_seq`` repeatedly sees a FROZEN ``seq`` while no genuine
    transition happens, so a wedged loop no longer reads as alive.
    """
    progress_log.append_progress(tmp_path, progress_log.ROUND_START)
    first = progress_log.tail_seq(tmp_path)
    # Many re-reads with no append in between.
    for _ in range(5):
        assert progress_log.tail_seq(tmp_path) == first
    # A real transition advances it.
    progress_log.append_progress(tmp_path, progress_log.PROPOSE)
    assert progress_log.tail_seq(tmp_path) == first + 1


def test_terminal_detection(tmp_path: Path) -> None:
    """A terminal marker is distinguishable from a mid-flight transition."""
    # is_terminal is a pure predicate over the type token.
    assert progress_log.is_terminal(progress_log.SETTLED) is True
    assert progress_log.is_terminal(progress_log.STOPPED) is True
    assert progress_log.is_terminal(progress_log.PROPOSE) is False
    assert progress_log.is_terminal(progress_log.TOURNAMENT_START) is False

    # A log whose tail is a progress event is NOT terminal (stalled-shaped).
    progress_log.append_progress(tmp_path, progress_log.TOURNAMENT_START)
    assert progress_log.tail_is_terminal(tmp_path) is False

    # Appending a terminal marker flips the tail terminal (settled-shaped).
    progress_log.append_progress(tmp_path, progress_log.SETTLED)
    assert progress_log.tail_is_terminal(tmp_path) is True
    last = progress_log.tail(tmp_path)
    assert last is not None
    assert last.type == progress_log.SETTLED


def test_stopped_is_terminal_but_distinct_from_settled(tmp_path: Path) -> None:
    """A budget/circuit-breaker cut marks STOPPED — terminal, but not SETTLED."""
    progress_log.append_progress(tmp_path, progress_log.STOPPED)
    assert progress_log.tail_is_terminal(tmp_path) is True
    last = progress_log.tail(tmp_path)
    assert last is not None
    assert last.type == progress_log.STOPPED
    assert last.type != progress_log.SETTLED


def test_clear_log_resets_seq(tmp_path: Path) -> None:
    """Clearing the log resets the cursor so a fresh invocation starts at 1."""
    progress_log.append_progress(tmp_path, progress_log.LOOP_START)
    progress_log.append_progress(tmp_path, progress_log.ROUND_START)
    assert progress_log.tail_seq(tmp_path) == 2

    progress_log.clear_log(tmp_path)
    assert progress_log.tail_seq(tmp_path) == 0
    assert not progress_log_path(tmp_path).exists()

    # A new invocation starts its seq from 1 again — a prior tail can never
    # leak in as live progress.
    assert progress_log.append_progress(tmp_path, progress_log.LOOP_START) == 1


def test_clear_log_idempotent_on_absent(tmp_path: Path) -> None:
    """Clearing a never-written log is a no-op, not an error."""
    progress_log.clear_log(tmp_path)  # must not raise
    assert progress_log.tail_seq(tmp_path) == 0
