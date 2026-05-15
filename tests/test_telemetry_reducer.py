"""Tests for :mod:`zicato.telemetry.reducer`.

The reducer is a pure function over a JSONL path. Most of these tests
write hand-crafted JSONL files containing one MessageToJson-shaped
event dict per line — matching the wire form goldfive's
``JSONLPersistenceSink`` produces — and then call
:func:`reduce_loss` against them. That decouples the tests from
goldfive being installed; the reducer's JSON-fallback path reads them
without needing the proto module.

A separate goldfive-gated test exercises the real proto-parsing path
via :func:`goldfive.sinks.persistence.replay_from_jsonl`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
    ScoringWeights,
    UserPersona,
)
from zicato.telemetry.reducer import (
    compute_drift_loss,
    read_loss_profile,
    reduce_loss,
    write_loss_profile,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_events_jsonl(path: Path, events: list[dict]) -> None:
    """Write a list of event-dicts as JSONL.

    Each event must be a dict matching the wire form goldfive's
    ``MessageToJson(sort_keys=True)`` produces. The reducer's
    JSON-fallback path consumes these directly — we deliberately do
    NOT round-trip through proto so the test does not depend on the
    goldfive package being installed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, sort_keys=True) + "\n")


def _single_turn_entry(entry_id: str = "ent-1") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _multi_turn_emulated_entry(entry_id: str = "ent-mt") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=120,
        user_persona=UserPersona(
            goal="book a flight",
            constraints="be polite",
            stop_when="ticket booked or user gives up",
        ),
        max_turns=10,
    )


def _default_weights() -> ScoringWeights:
    return ScoringWeights()


# ---------------------------------------------------------------------------
# compute_drift_loss
# ---------------------------------------------------------------------------


def test_compute_drift_loss_zero_when_empty() -> None:
    """Zero counts + zero ratio + zero runtime → zero loss."""
    loss = compute_drift_loss(
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=_default_weights(),
    )
    assert loss == 0.0


def test_compute_drift_loss_severity_weights() -> None:
    """A CRITICAL drift weighs 10x an INFO under default severity weights."""
    weights = _default_weights()
    info = compute_drift_loss(
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    critical = compute_drift_loss(
        drift_counts=(DriftCount(kind="off_topic", severity="critical", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert critical == pytest.approx(10.0 * info)


def test_compute_drift_loss_per_kind_multiplier() -> None:
    """per_kind_weights stacks multiplicatively with severity_weights."""
    weights = ScoringWeights(
        per_kind_weights={"off_topic": 2.0},
    )
    loss = compute_drift_loss(
        drift_counts=(
            DriftCount(kind="off_topic", severity="warning", count=3),
        ),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    # severity_weights["warning"]=3.0, per_kind=2.0, count=3 → 18.0
    assert loss == pytest.approx(18.0)


def test_compute_drift_loss_task_failure_term() -> None:
    """task_failure_ratio is multiplied by the fixed 10.0 constant."""
    weights = _default_weights()
    loss = compute_drift_loss(
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.5,
        runtime_ms=0,
        weights=weights,
    )
    assert loss == pytest.approx(5.0)


def test_compute_drift_loss_plan_revision_term() -> None:
    """plan_revisions * plan_revision_weight contributes additively."""
    weights = ScoringWeights(plan_revision_weight=2.0)
    loss = compute_drift_loss(
        drift_counts=(),
        plan_revisions=4,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert loss == pytest.approx(8.0)


def test_compute_drift_loss_runtime_term() -> None:
    """runtime contributes runtime_weight * runtime_seconds."""
    weights = ScoringWeights(runtime_weight=0.25)
    loss = compute_drift_loss(
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=4000,
        weights=weights,
    )
    assert loss == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# reduce_loss
# ---------------------------------------------------------------------------


def test_reduce_loss_no_events(tmp_path: Path) -> None:
    """An empty events file produces zero loss and empty drift buckets."""
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.drift_counts == ()
    assert profile.plan_revisions == 0
    assert profile.task_failure_ratio == 0.0
    assert profile.drift_loss == 0.0
    assert profile.pass_fail is None
    assert profile.expectation_result is None
    assert profile.turns_completed is None  # single-turn → None
    assert profile.memory_failure_count is None
    assert profile.context_loss_count is None


def test_reduce_loss_missing_file(tmp_path: Path) -> None:
    """A missing events file is treated as 'no events' rather than an error."""
    p = tmp_path / "does-not-exist.jsonl"
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.drift_loss == 0.0
    assert profile.run_id == "v0:ent-1"  # synthetic fallback id


def test_reduce_loss_three_drifts_mixed_kind_severity(tmp_path: Path) -> None:
    """Three DriftDetected events with mixed (kind, severity) → expected buckets + loss."""
    events = [
        {
            "event_id": "e1",
            "run_id": "run-A",
            "sequence": 0,
            "drift_detected": {
                "kind": "DRIFT_KIND_OFF_TOPIC",
                "severity": "DRIFT_SEVERITY_WARNING",
                "detail": "topic drift",
            },
        },
        {
            "event_id": "e2",
            "run_id": "run-A",
            "sequence": 1,
            "drift_detected": {
                "kind": "DRIFT_KIND_OFF_TOPIC",
                "severity": "DRIFT_SEVERITY_INFO",
                "detail": "another",
            },
        },
        {
            "event_id": "e3",
            "run_id": "run-A",
            "sequence": 2,
            "drift_detected": {
                "kind": "DRIFT_KIND_LOOPING_REASONING",
                "severity": "DRIFT_SEVERITY_CRITICAL",
                "detail": "loop",
            },
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    weights = _default_weights()
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=weights,
    )
    # Buckets: looping_reasoning|critical=1, off_topic|info=1, off_topic|warning=1
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {
        ("looping_reasoning", "critical"): 1,
        ("off_topic", "info"): 1,
        ("off_topic", "warning"): 1,
    }
    # Loss = 10*1 (critical) + 1*1 (info) + 3*1 (warning) = 14
    assert profile.drift_loss == pytest.approx(14.0)
    assert profile.run_id == "run-A"


def test_reduce_loss_plan_revisions_and_task_ratio(tmp_path: Path) -> None:
    """PlanRevised events bump revisions; TaskFailed / TaskStarted drive the ratio."""
    events = [
        {"event_id": "e1", "run_id": "r", "sequence": 0, "task_started": {"task_id": "t1"}},
        {"event_id": "e2", "run_id": "r", "sequence": 1, "task_started": {"task_id": "t2"}},
        {"event_id": "e3", "run_id": "r", "sequence": 2, "task_failed": {"task_id": "t1"}},
        {
            "event_id": "e4",
            "run_id": "r",
            "sequence": 3,
            "plan_revised": {"plan": {}, "drift_kind": "DRIFT_KIND_OFF_TOPIC"},
        },
        {
            "event_id": "e5",
            "run_id": "r",
            "sequence": 4,
            "plan_revised": {"plan": {}},
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.plan_revisions == 2
    # 1 failed / 2 started = 0.5
    assert profile.task_failure_ratio == pytest.approx(0.5)
    # drift_loss: 0 drifts + 0.5*2 (plan_revision_weight) + 10.0*0.5 (failure)
    assert profile.drift_loss == pytest.approx(1.0 + 5.0)


def test_reduce_loss_wall_clock_budget_exceeded_heavy_term(tmp_path: Path) -> None:
    """When wall_clock_budget_exceeded is True, the loss carries a heavy term."""
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    weights = _default_weights()
    base = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=weights,
    )
    busted = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=True,
        weights=weights,
    )
    assert busted.wall_clock_budget_exceeded is True
    # Heavy term = 5.0 * max(severity_weights.values()) = 5.0 * 10.0 = 50.0
    assert busted.drift_loss - base.drift_loss == pytest.approx(50.0)


def test_reduce_loss_pass_fail_from_expectation_result(tmp_path: Path) -> None:
    """expectation_result.passed flows through to LossProfile.pass_fail."""
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    exp = ExpectationResult(kind="predicate", passed=True, detail="ok")
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=exp,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.pass_fail is True
    assert profile.expectation_result == exp


# ---------------------------------------------------------------------------
# Multi-turn heuristics
# ---------------------------------------------------------------------------


def _repeated_paragraph() -> str:
    """A long-enough paragraph that two turns sharing it count as memory-failure."""
    return (
        "Let me restate the plan: first we will reserve the room, "
        "then we will send invitations to all attendees by email."
    )


def test_reduce_loss_memory_failure_heuristic(tmp_path: Path) -> None:
    """Agent that repeats a paragraph across two turns produces memory_failure_count >= 1."""
    paragraph = _repeated_paragraph()
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "sequence": 0,
            "agent_invocation_completed": {
                "agent_name": "a",
                "task_id": "t1",
                "invocation_id": "i1",
                "summary": paragraph,
            },
        },
        {
            "event_id": "e2",
            "run_id": "r",
            "sequence": 1,
            "agent_invocation_completed": {
                "agent_name": "a",
                "task_id": "t2",
                "invocation_id": "i2",
                "summary": "Different intro. " + paragraph,
            },
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_multi_turn_emulated_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.turns_completed == 2
    assert profile.memory_failure_count is not None
    assert profile.memory_failure_count >= 1


def test_reduce_loss_memory_failure_zero_when_distinct(tmp_path: Path) -> None:
    """Distinct agent turns produce a zero memory_failure_count."""
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "sequence": 0,
            "agent_invocation_completed": {"summary": "Hello, how can I help today?"},
        },
        {
            "event_id": "e2",
            "run_id": "r",
            "sequence": 1,
            "agent_invocation_completed": {"summary": "I have booked the flight for you."},
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_multi_turn_emulated_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.memory_failure_count == 0


def test_reduce_loss_context_loss_heuristic(tmp_path: Path) -> None:
    """Agent re-asking near-verbatim what the user already said produces context_loss_count >= 1.

    The heuristic is character-trigram cosine with a 0.7 threshold —
    it catches the *obvious* "the agent quoted the user's words back
    as a question" case. Genuine paraphrase detection would need an
    embedding-based test; this heuristic deliberately stays cheap and
    deterministic. The fixture below tracks that contract: the agent's
    question text near-duplicates the user's earlier statement, so
    the trigram overlap is high.
    """
    user_statement = (
        "My phone number is five five five one two three four five six seven."
    )
    agent_question = (
        "My phone number is five five five one two three four five six seven?"
    )
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "sequence": 0,
            "run_started": {
                "run_id": "r",
                "goal_summary": user_statement,
            },
        },
        {
            "event_id": "e2",
            "run_id": "r",
            "sequence": 1,
            "agent_invocation_completed": {"summary": agent_question},
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_multi_turn_emulated_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.context_loss_count is not None
    assert profile.context_loss_count >= 1


def test_reduce_loss_single_turn_heuristic_extras_are_none(tmp_path: Path) -> None:
    """Single-turn entries always leave the multi-turn extras as None."""
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "sequence": 0,
            "agent_invocation_completed": {"summary": _repeated_paragraph()},
        },
        {
            "event_id": "e2",
            "run_id": "r",
            "sequence": 1,
            "agent_invocation_completed": {"summary": _repeated_paragraph()},
        },
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.turns_completed is None
    assert profile.memory_failure_count is None
    assert profile.context_loss_count is None


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


def test_loss_profile_round_trip(tmp_path: Path) -> None:
    """write_loss_profile + read_loss_profile is a fixed point."""
    profile = LossProfile(
        run_id="r1",
        entry_id="ent",
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(
            DriftCount(kind="off_topic", severity="warning", count=2),
            DriftCount(kind="looping_reasoning", severity="critical", count=1),
        ),
        plan_revisions=3,
        task_failure_ratio=0.25,
        runtime_ms=12345,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True, detail="ok"),
        drift_loss=17.5,
        pass_fail=True,
        turns_completed=4,
        memory_failure_count=1,
        context_loss_count=0,
    )
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    loaded = read_loss_profile(p)
    assert loaded == profile


def test_loss_profile_round_trip_no_expectation(tmp_path: Path) -> None:
    """Round-trip handles None expectation_result and None multi-turn extras."""
    profile = LossProfile(
        run_id="r2",
        entry_id="ent2",
        generation_id="v1",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=None,
        turns_completed=None,
        memory_failure_count=None,
        context_loss_count=None,
    )
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    loaded = read_loss_profile(p)
    assert loaded == profile


# ---------------------------------------------------------------------------
# Goldfive-gated proto roundtrip
# ---------------------------------------------------------------------------


def test_reduce_loss_via_real_goldfive_replay(tmp_path: Path) -> None:
    """End-to-end: write events with the real sink, read via the reducer.

    Exercises the strict proto-parsed path. Skipped without goldfive.
    """
    pytest.importorskip("goldfive")
    pytest.importorskip("google.protobuf")
    import asyncio

    from goldfive.pb.goldfive.v1 import events_pb2  # type: ignore

    from zicato.telemetry import make_run_sink, make_run_sink_path

    sink = make_run_sink(tmp_path, "ep1", "v0", "ent-real")
    target = make_run_sink_path(tmp_path, "ep1", "v0", "ent-real")

    e = events_pb2.Event()
    e.event_id = "evt-1"
    e.run_id = "run-real"
    e.sequence = 0
    e.drift_detected.kind = events_pb2.DRIFT_KIND_OFF_TOPIC
    e.drift_detected.severity = events_pb2.DRIFT_SEVERITY_WARNING

    asyncio.run(sink.emit(e))
    asyncio.run(sink.close())

    profile = reduce_loss(
        events_jsonl_path=target,
        entry=_single_turn_entry(entry_id="ent-real"),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {("off_topic", "warning"): 1}
    assert profile.run_id == "run-real"
