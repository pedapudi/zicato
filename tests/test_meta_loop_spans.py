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
# The REAL scheduler ``_bounded`` sites open the matchup span (all THREE
# variants). The tree test above drives synthetic spans; this closes the gap
# that let the budgeted variant ship WITHOUT the matchup tuple-CM — so a
# fourth board-unit runner can never regress the matchup lane uncovered.
# ---------------------------------------------------------------------------


class _Ent:
    """Minimal ``BoardEntry`` stand-in — the runner reads only ``.id``."""

    def __init__(self, entry_id: str) -> None:
        self.id = entry_id


class _Gen:
    """Minimal ``Generation`` stand-in — only ``.id`` is threaded to the scorer."""

    def __init__(self, gen_id: str) -> None:
        self.id = gen_id


class _FakeCfg:
    """Minimal ``RuntimeConfig`` stand-in — the runner reads parallelism + ledger."""

    parallelism = 4
    token_ledger = None


@pytest.mark.parametrize("variant", ["full", "fast", "budgeted"])
async def test_real_bounded_opens_matchup_span_with_worker_nested(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """Every board-unit runner's ``_bounded`` brackets its unit in a matchup span.

    Drives the REAL ``_run_board_units_{full,fast,full_budgeted}`` through a
    fake board unit that opens the SAME worker span the production unit does,
    then asserts a single ``zicato.matchup`` span exists and the worker nests
    under it (parent == matchup invocation id). Pre-fix, the budgeted variant
    lacked the matchup tuple-CM its siblings carried, so the worker nested
    directly on the round — this test fails on that regression for ``budgeted``
    while passing for ``full`` / ``fast``.
    """
    from pathlib import Path

    from zicato.telemetry.meta_loop import SPAN_MATCHUP, SPAN_WORKER, meta_span
    from zicato.tournament import scheduling as sched

    async def _fake_full_unit(*, entry: Any, **_kw: Any) -> tuple[Any, Any]:
        # Mirror the real worker span the true board unit opens, so its parent
        # linkage exercises the ambient contextvar set by the matchup span.
        async with meta_span(f"worker::{entry.id}", kind=SPAN_WORKER, meta={"entry_id": entry.id}):
            await asyncio.sleep(0)
        return object(), object()

    async def _fake_cache_first(*, entry: Any, **_kw: Any) -> Any:
        async with meta_span(f"worker::{entry.id}", kind=SPAN_WORKER, meta={"entry_id": entry.id}):
            await asyncio.sleep(0)
        return object()

    async def _noop_record(self: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(sched, "_run_full_board_unit", _fake_full_unit)
    monkeypatch.setattr(sched, "_run_unit_cache_first", _fake_cache_first)
    monkeypatch.setattr(sched._IncrementalScorer, "record", _noop_record)

    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    token = set_current_emitter(emitter)

    board = [_Ent("e1")]
    common: dict[str, Any] = {
        "adapter": None,
        "weights": object(),
        "config": _FakeCfg(),
        "workspace_root": Path("/tmp"),
        "epoch_id": "ep",
        "match_id": "m1",
    }
    try:
        if variant == "full":
            await sched._run_board_units_full(
                parent_gen=_Gen("v0"), child_gen=_Gen("v1"), board=board, **common
            )
        elif variant == "fast":
            await sched._run_board_units_fast(child_gen=_Gen("v1"), board=board, **common)
        else:
            await sched._run_board_units_full_budgeted(
                parent_gen=_Gen("v0"),
                child_gen=_Gen("v1"),
                board=board,
                replicate_index=0,
                force_fresh=False,
                provenance=None,
                matchup_deadline=time.monotonic() + 3600.0,
                **common,
            )
    finally:
        reset_current_emitter(token)

    starts = _starts(sink)
    matchups = [s for s in starts if s.agent_name == f"zicato.{SPAN_MATCHUP}"]
    workers = [s for s in starts if s.agent_name == f"zicato.{SPAN_WORKER}"]
    assert len(matchups) == 1, f"{variant}: expected one matchup span, got {len(matchups)}"
    assert len(workers) == 1, f"{variant}: expected one worker span, got {len(workers)}"
    assert matchups[0].task_id == "e1"
    assert (
        workers[0].parent == matchups[0].invocation_id
    ), f"{variant}: worker must nest under the matchup span, not the round"


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


# ---------------------------------------------------------------------------
# The proposer / judge lifelines nest into the span tree (HARMONOGRAF.md §7).
#
# The pre-existing proposer / judge emits used to stuff their payload JSON
# into ``parent_invocation_id``, rendering them as DETACHED orphan roots. The
# migration parents them on the ambient structural span (propose / slot for
# the proposer, gate for a judge) and moves the payload to the COMPLETED
# envelope's ``summary`` — the structural-span convention.
# ---------------------------------------------------------------------------


async def test_proposer_emit_nests_under_slot_span() -> None:
    """Each slate slot's proposer call parents on ITS slot span, not a blob."""
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    token = set_current_emitter(emitter)
    try:
        async with meta_span("propose", kind=SPAN_PHASE):

            async def _slot(i: int) -> None:
                async with meta_span(f"slot {i}", kind=SPAN_SLOT):
                    inv = await emitter.proposer_started(
                        model="aux-model",
                        epoch_id="ep-1",
                        parent_generation_id="v0",
                        new_generation_id=f"v1-{i}",
                    )
                    await emitter.proposer_completed(
                        invocation_id=inv,
                        latency_s=0.1,
                        response_chars=42,
                        outcome="completed",
                    )

            await asyncio.gather(*(_slot(i) for i in range(3)))
    finally:
        reset_current_emitter(token)

    starts = _starts(sink)
    slot_ids = {s.invocation_id for s in starts if s.agent_name == f"zicato.{SPAN_SLOT}"}
    proposer_starts = [s for s in starts if s.agent_name == "zicato.proposer"]
    assert len(slot_ids) == 3
    assert len(proposer_starts) == 3
    # Every proposer started nests under one of the slate slots (no orphans).
    for p in proposer_starts:
        assert p.parent in slot_ids, "proposer must nest under its slot span"

    # The started identity payload migrated to the COMPLETED envelope's summary.
    proposer_completes = [s for s in _completes(sink) if s.agent_name == "zicato.proposer"]
    assert len(proposer_completes) == 3
    for c in proposer_completes:
        assert c.meta["model"] == "aux-model"
        assert c.meta["epoch_id"] == "ep-1"
        assert c.meta["parent_generation_id"] == "v0"
        assert c.meta["new_generation_id"].startswith("v1-")
        # The completed half's own metrics ride alongside the migrated fields.
        assert c.meta["outcome"] == "completed"
        assert c.meta["response_chars"] == 42
        assert "latency_s" in c.meta


async def test_judge_emit_nests_under_gate_span() -> None:
    """A process judge's call parents on the enclosing gate (phase) span."""
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    token = set_current_emitter(emitter)
    try:
        async with meta_span("gate", kind=SPAN_PHASE) as _gate:
            inv = await emitter.judge_invoked(
                judge_name="decision_telemetry_analyzer", kind="process"
            )
            await emitter.judgment_emitted(
                invocation_id=inv,
                judge_name="decision_telemetry_analyzer",
                verdict_kind="rubric",
                detail="insight written",
                latency_s=0.2,
            )
    finally:
        reset_current_emitter(token)

    starts = _starts(sink)
    gate = next(s for s in starts if s.task_id == "gate")
    judge_starts = [s for s in starts if s.agent_name == "zicato.judge:decision_telemetry_analyzer"]
    assert len(judge_starts) == 1
    assert judge_starts[0].parent == gate.invocation_id

    # The judge payload lands in the completed summary (its new home).
    judge_completes = [
        s for s in _completes(sink) if s.agent_name == "zicato.judge:decision_telemetry_analyzer"
    ]
    assert len(judge_completes) == 1
    assert judge_completes[0].meta["judge_name"] == "decision_telemetry_analyzer"
    assert judge_completes[0].meta["verdict_kind"] == "rubric"
    assert judge_completes[0].meta["detail"] == "insight written"


async def test_proposer_emit_without_ambient_span_has_empty_parent() -> None:
    """A bare emit (no enclosing span — e.g. a unit test) keeps an empty parent.

    Today's detached-root behaviour is preserved when there is no ambient span
    to nest under, rather than resurrecting the payload-blob parent.
    """
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[sink])
    # No ``set_current_emitter`` and no open span: ``_current_span_id`` is "".
    inv = await emitter.proposer_started(
        model="m", epoch_id="e", parent_generation_id="p", new_generation_id="c"
    )
    await emitter.proposer_completed(invocation_id=inv, latency_s=0.0, outcome="completed")

    proposer_starts = [s for s in _starts(sink) if s.agent_name == "zicato.proposer"]
    assert len(proposer_starts) == 1
    assert proposer_starts[0].parent == ""


async def test_read_meta_loop_session_id_still_reads_migrated_jsonl(tmp_path: Any) -> None:
    """The one zicato-side consumer still recovers the session id post-migration.

    ``read_meta_loop_session_id`` reads only ``session_id`` off the first
    JSONL line (never the payload / parent) — the migration must not perturb
    it. Drives the real JSONL sink end-to-end.
    """
    from zicato.query.paths import WorkspacePaths
    from zicato.query.runtime_view import read_meta_loop_session_id
    from zicato.telemetry.meta_loop import build_meta_loop_emitter

    emitter = build_meta_loop_emitter(
        tmp_path,
        harmonograf_url="",
        evolve_started_at_iso="2026-05-28T05:04:00+00:00",
    )
    inv = await emitter.proposer_started(
        model="m", epoch_id="e", parent_generation_id="p", new_generation_id="c"
    )
    await emitter.proposer_completed(invocation_id=inv, latency_s=0.0, outcome="completed")
    await emitter.close()

    sid = read_meta_loop_session_id(WorkspacePaths(tmp_path))
    assert sid == emitter.session_id
    assert sid  # non-empty
