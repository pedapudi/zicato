"""Structural-span coverage for the meta-loop emitter (HARMONOGRAF.md §7).

These pin the lifelines surface added on top of the proposer / judge emits:
the generic :meth:`MetaLoopEmitter.span` context manager, the ambient
``meta_span`` helper, the contextvar-inferred span tree (round ⊃ phase ⊃
matchup ⊙ worker, slate slots under propose), and the non-negotiable
disciplines — pairing on exception / cancellation, sink-failure isolation,
bounded overhead, and clean teardown.

Every assertion reads the emitted ENVELOPES off a capturing fake sink; no
live harmonograf server is ever contacted (conftest keeps the suite
server-free).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from zicato.telemetry.meta_loop import (
    SPAN_MATCHUP,
    SPAN_PHASE,
    SPAN_ROUND,
    SPAN_SLOT,
    SPAN_WORKER,
    MetaLoopEmitter,
    current_meta_emitter,
    meta_span,
    reset_current_emitter,
    set_current_emitter,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake sinks + envelope projection
# ---------------------------------------------------------------------------


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.closed = False

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


class _ExplodingSink:
    def __init__(self) -> None:
        self.calls = 0

    async def emit(self, event: Any) -> None:
        self.calls += 1
        raise RuntimeError("boom")

    async def close(self) -> None:
        return None


class _Span:
    """Projection of one captured span envelope (proto or dict fallback)."""

    __slots__ = ("started", "agent_name", "task_id", "invocation_id", "parent", "meta")

    def __init__(self, event: Any) -> None:
        if isinstance(event, dict):
            self.started = event.get("kind") == "span_started"
            self.agent_name = event.get("agent_name", "")
            self.task_id = event.get("task_id", "")
            self.invocation_id = event.get("invocation_id", "")
            self.parent = event.get("parent_invocation_id", "")
            self.meta = dict(event.get("payload", {}))
            return
        started = getattr(event, "agent_invocation_started", None)
        if started is not None and event.WhichOneof("payload") == "agent_invocation_started":
            self.started = True
            self.agent_name = started.agent_name
            self.task_id = started.task_id
            self.invocation_id = started.invocation_id
            self.parent = started.parent_invocation_id
            self.meta = {}
            return
        completed = event.agent_invocation_completed
        self.started = False
        self.agent_name = completed.agent_name
        self.task_id = completed.task_id
        self.invocation_id = completed.invocation_id
        self.parent = ""
        self.meta = json.loads(completed.summary) if completed.summary else {}


def _spans(sink: _CapturingSink) -> list[_Span]:
    return [_Span(e) for e in sink.events]


def _starts(sink: _CapturingSink) -> list[_Span]:
    return [s for s in _spans(sink) if s.started]


def _completes(sink: _CapturingSink) -> list[_Span]:
    return [s for s in _spans(sink) if not s.started]


def _by_invocation(sink: _CapturingSink) -> dict[str, tuple[_Span, _Span]]:
    """Map invocation_id -> (started, completed); asserts every span is paired."""
    starts = {s.invocation_id: s for s in _starts(sink)}
    completes = {s.invocation_id: s for s in _completes(sink)}
    assert set(starts) == set(completes), "every started span must have a completed pair"
    return {inv: (starts[inv], completes[inv]) for inv in starts}


# ---------------------------------------------------------------------------
# The span tree — round ⊃ phase ⊃ matchup ⊃ worker, slots under propose.
# ---------------------------------------------------------------------------


async def _mock_evolve_round(emitter: MetaLoopEmitter) -> None:
    """A synthetic evolve round exercising the full structural taxonomy.

    Drives the SAME ambient ``meta_span`` + ``asyncio.gather`` shapes the real
    call sites use (loop → propose slate → tournament fan-out → worker), so the
    contextvar-inferred parent linkage is exercised exactly as in production.
    """
    async with meta_span("round 0", kind=SPAN_ROUND, meta={"round_index": 0}):
        # Propose phase with a 3-slot slate gathered concurrently.
        async with meta_span("propose", kind=SPAN_PHASE):

            async def _slot(i: int) -> None:
                async with meta_span(f"slot {i}", kind=SPAN_SLOT, meta={"sample": i}):
                    await asyncio.sleep(0)

            await asyncio.gather(*(_slot(i) for i in range(3)))

        # Tournament: two matchups fanned out; each runs two workers.
        async def _matchup(entry: str) -> None:
            async with meta_span(entry, kind=SPAN_MATCHUP, meta={"entry_id": entry}):

                async def _worker(side: str) -> None:
                    async with meta_span(
                        f"{entry}::{side}", kind=SPAN_WORKER, meta={"side": side}
                    ) as sp:
                        await asyncio.sleep(0)
                        sp.set(adk_session_id=f"sess-{entry}-{side}")

                await asyncio.gather(_worker("parent"), _worker("child"))

        await asyncio.gather(_matchup("e1"), _matchup("e2"))


async def test_span_tree_nesting_and_pairing() -> None:
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="run-tree", session_id="sess-tree", sinks=[sink])
    token = set_current_emitter(emitter)
    try:
        await _mock_evolve_round(emitter)
    finally:
        reset_current_emitter(token)

    paired = _by_invocation(sink)
    starts = _starts(sink)
    # id -> its started span, for parent lookups.
    by_id = {s.invocation_id: s for s in starts}

    def _find(agent: str, task: str) -> _Span:
        matches = [s for s in starts if s.agent_name == agent and s.task_id == task]
        assert len(matches) == 1, f"expected one {agent}/{task}, got {len(matches)}"
        return matches[0]

    round_span = _find("zicato.round", "round 0")
    assert round_span.parent == "", "the round is the tree root"

    propose = _find("zicato.phase", "propose")
    assert propose.parent == round_span.invocation_id

    # Three slate slots, all parented on the propose phase (overlapping siblings).
    slots = [s for s in starts if s.agent_name == f"zicato.{SPAN_SLOT}"]
    assert len(slots) == 3
    assert all(s.parent == propose.invocation_id for s in slots)

    # Two matchups under the round (no tournament wrapper — they nest on round).
    matchups = [s for s in starts if s.agent_name == f"zicato.{SPAN_MATCHUP}"]
    assert {m.task_id for m in matchups} == {"e1", "e2"}
    assert all(m.parent == round_span.invocation_id for m in matchups)

    # Each worker nests under ITS matchup and carries the goldfive session id.
    workers = [s for s in starts if s.agent_name == f"zicato.{SPAN_WORKER}"]
    assert len(workers) == 4
    for w in workers:
        entry = w.task_id.split("::", 1)[0]
        parent_matchup = by_id[w.parent]
        assert parent_matchup.agent_name == f"zicato.{SPAN_MATCHUP}"
        assert parent_matchup.task_id == entry
        _started, completed = paired[w.invocation_id]
        assert completed.meta["adk_session_id"] == f"sess-{entry}-{w.task_id.split('::')[1]}"
        assert completed.meta["outcome"] == "completed"


# ---------------------------------------------------------------------------
# Pairing on exception / cancellation — for each span kind.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [SPAN_ROUND, SPAN_PHASE, SPAN_MATCHUP, SPAN_WORKER, SPAN_SLOT],
)
async def test_span_closes_on_exception(kind: str) -> None:
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async with emitter.span("x", kind=kind):
            raise Boom("body failed")

    paired = _by_invocation(sink)
    ((_started, completed),) = paired.values()
    assert completed.meta["outcome"] == "error:Boom"
    assert "latency_s" in completed.meta


@pytest.mark.parametrize(
    "kind",
    [SPAN_ROUND, SPAN_PHASE, SPAN_MATCHUP, SPAN_WORKER, SPAN_SLOT],
)
async def test_span_closes_on_cancellation(kind: str) -> None:
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])

    with pytest.raises(asyncio.CancelledError):
        async with emitter.span("x", kind=kind):
            raise asyncio.CancelledError

    paired = _by_invocation(sink)
    ((_started, completed),) = paired.values()
    assert completed.meta["outcome"] == "cancelled"


async def test_real_task_cancellation_still_closes_span() -> None:
    """An externally cancelled task's span still emits its completed half.

    The completed emit is shielded, so a cancellation propagating through the
    span's ``finally`` cannot drop the closing envelope.
    """
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    entered = asyncio.Event()

    async def _body() -> None:
        async with emitter.span("worker", kind=SPAN_WORKER):
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    paired = _by_invocation(sink)
    ((_started, completed),) = paired.values()
    assert completed.meta["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# Sink-failure isolation + no-op paths.
# ---------------------------------------------------------------------------


async def test_raising_sink_never_fails_the_span() -> None:
    good = _CapturingSink()
    bad = _ExplodingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[bad, good])

    async with emitter.span("phase", kind=SPAN_PHASE):
        pass

    # The body ran, the good sink saw the pair, and the raising sink was
    # offered both halves without ever surfacing an exception.
    assert len(good.events) == 2
    assert bad.calls == 2


async def test_meta_span_is_no_op_without_ambient_emitter() -> None:
    # No emitter bound: meta_span yields a no-op handle and emits nothing.
    assert current_meta_emitter() is None
    async with meta_span("phase", kind=SPAN_PHASE) as handle:
        handle.set(anything=1)  # tolerated, discarded
    assert current_meta_emitter() is None


async def test_ambient_emitter_binding_round_trips() -> None:
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    token = set_current_emitter(emitter)
    try:
        assert current_meta_emitter() is emitter
        async with meta_span("round 0", kind=SPAN_ROUND):
            pass
    finally:
        reset_current_emitter(token)
    assert current_meta_emitter() is None
    assert len(sink.events) == 2


# ---------------------------------------------------------------------------
# Teardown cleanliness.
# ---------------------------------------------------------------------------


async def test_close_flushes_every_sink() -> None:
    a, b = _CapturingSink(), _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[a, b])
    async with emitter.span("round 0", kind=SPAN_ROUND):
        pass
    await emitter.close()
    assert a.closed and b.closed


# ---------------------------------------------------------------------------
# Overhead — the emitter must be cheap enough to never gate a round.
# ---------------------------------------------------------------------------


async def test_span_overhead_is_bounded() -> None:
    """Time a span-dense mock round enabled (capturing sink) vs disabled.

    Absolute per-span ceiling rather than a ratio: the disabled baseline is
    near-zero, so a ratio is unstable under xdist. 5 ms/span is ~1000x the
    observed cost (a proto build + two list appends), so it never flakes while
    still catching a genuine regression (e.g. accidental network I/O on the
    hot path).
    """
    n_spans = 400

    async def _dense(emitter: MetaLoopEmitter | None) -> None:
        token = set_current_emitter(emitter)
        try:
            for i in range(n_spans):
                async with meta_span(f"s{i}", kind=SPAN_MATCHUP, meta={"i": i}):
                    pass
        finally:
            reset_current_emitter(token)

    # Disabled baseline (no ambient emitter → the no-op path).
    t0 = time.perf_counter()
    await _dense(None)
    disabled_s = time.perf_counter() - t0

    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    t0 = time.perf_counter()
    await _dense(emitter)
    enabled_s = time.perf_counter() - t0

    assert len(sink.events) == n_spans * 2
    per_span_ms = (enabled_s / n_spans) * 1000.0
    assert per_span_ms < 5.0, (
        f"enabled span overhead {per_span_ms:.3f} ms/span (disabled baseline "
        f"{disabled_s * 1000:.1f} ms total) exceeds the 5 ms/span ceiling"
    )
