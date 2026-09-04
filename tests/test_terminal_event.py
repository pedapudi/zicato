"""Tests for the terminal-event invariant.

A board-entry run's ``events.jsonl`` must always end with a lifecycle
frame — ``run_completed`` / ``run_aborted`` / ``conversation_ended`` —
regardless of how the run ended. Goldfive emits these on its own clean
and caught-exception paths, but cannot emit when its task is *cancelled*
from the outside (the worker's cooperative wall-clock budget, or a
parent / supervisor kill of the entire subprocess). The fix in
:mod:`zicato.telemetry.terminal_event` and the wiring in
:mod:`zicato._tournament_worker` + :mod:`zicato.tournament.runner` make
sure that invariant holds.

These tests cover:

* :class:`SequenceTrackingSink` records ``run_id`` and the max
  ``sequence`` of every event flowing through.
* :func:`ensure_run_aborted_event` appends a ``run_aborted`` frame when
  the file lacks a terminal, and is a no-op when one is already present.
* The worker, when its cooperative wall-clock budget fires after at
  least one event has flowed through, leaves an ``events.jsonl`` ending
  with a ``run_aborted`` frame on disk.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from tests._runtime_builders import make_generation
from tests._subprocess_worker_support import (  # noqa: F401 — used by spawned worker
    EmittingThenSleepingAdapter,
    evaluation_call_llm,
    target_call_llm,
)
from tests.test_subprocess_workers import (
    _entry,
    _spawn_worker_blocking,
    _worker_env,
    _write_args_file,
)
from zicato.core.workspace import events_jsonl_path
from zicato.telemetry.terminal_event import (
    SequenceTrackingSink,
    ensure_run_aborted_event,
)

# ---------------------------------------------------------------------------
# SequenceTrackingSink
# ---------------------------------------------------------------------------


class _RecordingSink:
    """Inner sink: records every event it sees so we can assert the wrapper
    forwards them unchanged."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.closed = False

    async def emit(self, event: object) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


def test_sequence_tracker_records_run_id_and_max_sequence_from_dicts() -> None:
    inner = _RecordingSink()
    tracker = SequenceTrackingSink(inner)

    async def go() -> None:
        await tracker.emit({"run_id": "r-1", "sequence": 1})
        await tracker.emit({"run_id": "r-1", "sequence": 5})
        await tracker.emit({"run_id": "r-1", "sequence": 3})

    asyncio.run(go())

    assert tracker.last_run_id == "r-1"
    assert tracker.max_sequence == 5
    # Forwarding is unchanged — the inner sink saw every event.
    assert len(inner.events) == 3


def test_sequence_tracker_records_from_proto_messages() -> None:
    from goldfive.events import run_started_event

    inner = _RecordingSink()
    tracker = SequenceTrackingSink(inner)

    evt = run_started_event(run_id="r-proto", sequence=7, goal_summary="x")

    async def go() -> None:
        await tracker.emit(evt)

    asyncio.run(go())

    assert tracker.last_run_id == "r-proto"
    assert tracker.max_sequence == 7


def test_sequence_tracker_close_forwards() -> None:
    inner = _RecordingSink()
    tracker = SequenceTrackingSink(inner)

    asyncio.run(tracker.close())
    assert inner.closed is True


# ---------------------------------------------------------------------------
# ensure_run_aborted_event
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_ensure_terminal_appends_when_none_present(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_jsonl(
        events,
        [
            {"runId": "r-x", "sequence": 1, "goldfiveLlmCallEnd": {}},
            {"runId": "r-x", "sequence": 2, "goldfiveLlmCallEnd": {}},
        ],
    )

    appended = ensure_run_aborted_event(events)
    assert appended is True

    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    last = json.loads(lines[-1])
    assert "runAborted" in last or "run_aborted" in last
    assert last.get("runId") == "r-x" or last.get("run_id") == "r-x"


def test_ensure_terminal_is_noop_when_terminal_present(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_jsonl(
        events,
        [
            {"runId": "r-y", "sequence": 1, "goldfiveLlmCallEnd": {}},
            {"runId": "r-y", "sequence": 2, "runCompleted": {}},
        ],
    )

    before = events.read_text(encoding="utf-8")
    appended = ensure_run_aborted_event(events)
    after = events.read_text(encoding="utf-8")

    assert appended is False
    assert before == after, "file must be unchanged when a terminal is present"


def test_ensure_terminal_noop_when_file_missing(tmp_path: Path) -> None:
    events = tmp_path / "missing.jsonl"
    appended = ensure_run_aborted_event(events)
    assert appended is False
    # No file is created when there were no prior events.
    assert not events.exists()


def test_ensure_terminal_appended_carries_reason(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_jsonl(
        events,
        [
            {"runId": "r-z", "sequence": 1, "goldfiveLlmCallEnd": {}},
        ],
    )

    ensure_run_aborted_event(events, reason="wall_clock_budget_exceeded")
    last = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    payload = last.get("runAborted") or last.get("run_aborted")
    assert payload is not None
    assert payload.get("reason") == "wall_clock_budget_exceeded"


def test_ensure_terminal_sequence_strictly_increases(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_jsonl(
        events,
        [
            {"runId": "r-q", "sequence": 7, "goldfiveLlmCallEnd": {}},
            {"runId": "r-q", "sequence": 12, "goldfiveLlmCallEnd": {}},
        ],
    )

    ensure_run_aborted_event(events)
    last = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    appended_seq = last.get("sequence")
    if isinstance(appended_seq, str):
        appended_seq = int(appended_seq)
    assert appended_seq is not None
    assert int(appended_seq) > 12, "appended terminal frame must extend the sequence"


# ---------------------------------------------------------------------------
# End-to-end: worker's cooperative budget fires after an emit
# ---------------------------------------------------------------------------


def test_worker_emits_run_aborted_when_cooperative_budget_cancels_mid_emit(
    tmp_path: Path,
) -> None:
    """The bug from run #7: when the worker's wait_for cancels the inner task
    mid-LLM-call, the events.jsonl must still end with a ``run_aborted``
    frame so the downstream transcript reconstructor can flip
    ``complete=True`` and the dashboard renders an honest "timed out"
    panel instead of a misleading "in progress" cue.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    # 1s budget; the adapter emits one frame then sleeps via asyncio.sleep
    # so the worker's own cooperative budget fires.
    entry = _entry(budget_s=1)

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory=("tests._subprocess_worker_support:make_emitting_then_sleeping_adapter"),
    )

    proc = _spawn_worker_blocking(args_path)
    assert proc.returncode == 0, "a self-aborted worker still exits cleanly"

    events_path = events_jsonl_path(workspace, "e0", generation.id, entry.id)
    assert events_path.exists(), "the worker must have written an events file"

    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # At minimum: one run_started + one run_aborted.
    assert len(lines) >= 2

    last = json.loads(lines[-1])
    assert "runAborted" in last or "run_aborted" in last, (
        "events.jsonl must end with a run_aborted lifecycle frame on a "
        "cooperative wall-clock cancellation, got: " + lines[-1]
    )

    # The downstream reconstructor must now flip ``complete=True``.
    from zicato.query.transcript_reconstruction import reconstruct_transcript

    transcript = reconstruct_transcript(events_path, partial_ok=True)
    assert transcript.complete is True, (
        "with a terminal frame on disk the transcript must reconstruct " "as complete=True"
    )


# ---------------------------------------------------------------------------
# Convenience: subprocess fan-in (smoke; the substantive assertions live
# in the spawned-worker test above)
# ---------------------------------------------------------------------------


def test_worker_spawns_with_pythonpath_for_emitting_adapter() -> None:
    """Smoke: the worker module is importable under PYTHONPATH and the
    emitting-then-sleeping adapter resolves cleanly. The main worker
    test does the substantive assertions; this one fails fast if a
    rename of the test-support module breaks the lookup."""
    env = _worker_env()
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from tests._subprocess_worker_support import "
            "make_emitting_then_sleeping_adapter; "
            "make_emitting_then_sleeping_adapter()",
        ],
        env=env,
        check=False,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")
