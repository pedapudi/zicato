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
    split_judge_attributed_kind,
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
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=3),),
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
    """A budget-exceeded run carries the full not-completed penalty.

    ``wall_clock_budget_exceeded`` is one non-success terminal state, so
    it triggers the same penalty as any other: the heavy fixed term
    ``not_completed_penalty`` PLUS the floored ``task_failure_ratio`` of
    1.0 contributing ``_TASK_FAILURE_RATIO_MULTIPLIER`` inside
    ``compute_drift_loss``. For the default weights that is
    ``5.0 * 10.0 + 10.0 * 1.0 == 60.0`` over the clean baseline.
    """
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
    # Heavy term 5.0 * max(severity_weights) = 50.0, plus the floored
    # task_failure_ratio of 1.0 * 10.0 = 10.0 → 60.0 total.
    assert busted.drift_loss - base.drift_loss == pytest.approx(60.0)
    assert busted.task_failure_ratio == pytest.approx(1.0)


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
    # A bool expectation leaves score=None on the expectation result but the
    # reducer derives the continuous score as 1.0 onto the profile.
    assert profile.score == 1.0
    assert profile.metrics is None


def test_reduce_loss_continuous_score_and_metrics_flow_through(tmp_path: Path) -> None:
    """A scorer's float score + precision/recall metrics land on the profile."""
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    exp = ExpectationResult(
        kind="predicate",
        passed=True,
        score=0.625,
        metrics={"precision": 0.5, "recall": 0.83},
    )
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
    assert profile.score == 0.625
    assert profile.metrics == {"precision": 0.5, "recall": 0.83}


def test_reduce_loss_clamps_rogue_score(tmp_path: Path) -> None:
    """An out-of-range / non-finite score is clamped before it reaches the profile."""
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    out_of_range = ExpectationResult(kind="predicate", passed=True, score=9.9)
    nan_score = ExpectationResult(kind="predicate", passed=True, score=float("nan"))
    weights = _default_weights()
    hi = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=out_of_range,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=weights,
    )
    nan = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=nan_score,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=weights,
    )
    assert hi.score == 1.0  # clamped from 9.9
    assert nan.score == 0.0  # NaN treated as a miss


def test_loss_profile_round_trip_with_score_and_metrics(tmp_path: Path) -> None:
    """score + metrics survive write_loss_profile / read_loss_profile unchanged."""
    profile = LossProfile(
        run_id="r-score",
        entry_id="ent-score",
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(
            kind="predicate",
            passed=True,
            score=0.4,
            metrics={"precision": 0.3, "recall": 0.6},
        ),
        drift_loss=0.0,
        pass_fail=True,
        score=0.4,
        metrics={"precision": 0.3, "recall": 0.6},
    )
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    loaded = read_loss_profile(p)
    assert loaded == profile


def test_loss_profile_round_trip_score_none_is_back_compat(tmp_path: Path) -> None:
    """A profile written without score/metrics reads back with score=None (back-compat)."""
    profile = LossProfile(
        run_id="r-old",
        entry_id="ent-old",
        generation_id="v0",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=0.0,
        pass_fail=True,
    )
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    # Simulate a pre-feature loss.json: drop the score / metrics keys.
    import json as _json

    data = _json.loads(p.read_text(encoding="utf-8"))
    data.pop("score", None)
    data.pop("metrics", None)
    data["expectation_result"].pop("score", None)
    data["expectation_result"].pop("metrics", None)
    p.write_text(_json.dumps(data), encoding="utf-8")
    loaded = read_loss_profile(p)
    assert loaded.score is None
    assert loaded.metrics is None
    assert loaded == profile


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
    user_statement = "My phone number is five five five one two three four five six seven."
    agent_question = "My phone number is five five five one two three four five six seven?"
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

    from goldfive.pb.goldfive.v1 import (
        events_pb2,  # type: ignore
        types_pb2,  # type: ignore
    )

    from zicato.telemetry import make_run_sink, make_run_sink_path

    sink = make_run_sink(tmp_path, "ep1", "v0", "ent-real")
    target = make_run_sink_path(tmp_path, "ep1", "v0", "ent-real")

    e = events_pb2.Event()
    e.event_id = "evt-1"
    e.run_id = "run-real"
    e.sequence = 0
    # Enum constants live in types_pb2, not events_pb2.
    e.drift_detected.kind = types_pb2.DRIFT_KIND_OFF_TOPIC
    e.drift_detected.severity = types_pb2.DRIFT_SEVERITY_WARNING

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


# ---------------------------------------------------------------------------
# Custom-judge drift attribution
# ---------------------------------------------------------------------------
#
# A custom judge emits a ``DriftDetected`` of kind ``custom`` AND a paired
# ``JudgementEmitted`` carrying the stable ``judge_name``. The reducer must
# attribute the ``custom``-kind drift to that ``judge_name`` (folded into the
# drift kind as ``custom:<judge_name>``) so two distinct custom judges score
# independently via ``ScoringWeights.per_judge_weights``. A custom judge with
# no weight entry uses ``default_judge_weight``; boards with no custom judges
# are unaffected (back-compat).


def _judgement_emitted(
    judge_name: str,
    *,
    drift_kind: str = "custom",
    severity: str = "warning",
    run_id: str = "run-J",
    seq: int = 0,
) -> dict:
    """A ``JudgementEmitted`` event-dict for a drift-flavoured verdict.

    Mirrors the wire shape goldfive's steerer emits for a custom judge's
    drift verdict: ``verdict_kind == "drift"`` plus the bare lowercase
    ``drift_kind`` / ``severity`` that mirror the paired ``DriftDetected``.
    """
    return {
        "event_id": f"j{seq}",
        "run_id": run_id,
        "sequence": seq,
        "judgement_emitted": {
            "judge_name": judge_name,
            "verdict_kind": "drift",
            "drift_kind": drift_kind,
            "severity": severity,
        },
    }


def _drift_detected(
    kind: str = "DRIFT_KIND_CUSTOM",
    *,
    severity: str = "DRIFT_SEVERITY_WARNING",
    run_id: str = "run-J",
    seq: int = 0,
) -> dict:
    """A ``DriftDetected`` event-dict in the MessageToJson wire form."""
    return {
        "event_id": f"d{seq}",
        "run_id": run_id,
        "sequence": seq,
        "drift_detected": {"kind": kind, "severity": severity, "detail": ""},
    }


def test_split_judge_attributed_kind_round_trip() -> None:
    """``split_judge_attributed_kind`` inverts the ``custom:<name>`` encoding."""
    assert split_judge_attributed_kind("custom:slide_quality") == (True, "slide_quality")
    assert split_judge_attributed_kind("custom") == (True, "")
    assert split_judge_attributed_kind("off_topic") == (False, "")
    # A judge name that itself contains a colon survives (split is on the
    # first separator only).
    assert split_judge_attributed_kind("custom:team:judge") == (True, "team:judge")


def test_compute_drift_loss_per_judge_weight_distinct_judges() -> None:
    """Two custom judges with distinct per_judge_weights score independently."""
    weights = ScoringWeights(
        per_judge_weights={"judge_a": 2.0, "judge_b": 5.0},
    )
    # judge_a: one warning-severity custom drift.
    loss_a = compute_drift_loss(
        drift_counts=(DriftCount(kind="custom:judge_a", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    # judge_b: one warning-severity custom drift.
    loss_b = compute_drift_loss(
        drift_counts=(DriftCount(kind="custom:judge_b", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    # severity_weights["warning"] == 3.0; per_judge multiplier stacks.
    assert loss_a == pytest.approx(3.0 * 2.0)
    assert loss_b == pytest.approx(3.0 * 5.0)
    # The two judges are independent — judge_b's heavier weight does not
    # bleed into judge_a's score.
    assert loss_a != pytest.approx(loss_b)


def test_compute_drift_loss_per_judge_weight_default_for_unknown_judge() -> None:
    """A custom judge with no per_judge_weights entry uses default_judge_weight."""
    weights = ScoringWeights(
        per_judge_weights={"judge_a": 9.0},
        default_judge_weight=4.0,
    )
    # judge_unknown is absent from per_judge_weights → default_judge_weight.
    loss = compute_drift_loss(
        drift_counts=(DriftCount(kind="custom:judge_unknown", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert loss == pytest.approx(3.0 * 4.0)


def test_compute_drift_loss_bare_custom_uses_default_judge_weight() -> None:
    """An unattributed bare ``custom`` drift also scores at default_judge_weight."""
    weights = ScoringWeights(default_judge_weight=2.5)
    loss = compute_drift_loss(
        drift_counts=(DriftCount(kind="custom", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert loss == pytest.approx(3.0 * 2.5)


def test_compute_drift_loss_default_judge_weight_defaults_to_one() -> None:
    """With no per_judge config, a custom judge weighs the same as an unknown kind."""
    weights = ScoringWeights()  # default_judge_weight == 1.0
    custom = compute_drift_loss(
        drift_counts=(DriftCount(kind="custom:some_judge", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    first_class = compute_drift_loss(
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert custom == pytest.approx(first_class)


def test_compute_drift_loss_per_kind_and_per_judge_coexist() -> None:
    """per_kind_weights and per_judge_weights apply to their own kinds only."""
    weights = ScoringWeights(
        per_kind_weights={"off_topic": 2.0},
        per_judge_weights={"judge_a": 7.0},
    )
    loss = compute_drift_loss(
        drift_counts=(
            DriftCount(kind="off_topic", severity="warning", count=1),
            DriftCount(kind="custom:judge_a", severity="warning", count=1),
        ),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    # off_topic: 3.0 * 2.0 ; custom:judge_a: 3.0 * 7.0
    assert loss == pytest.approx(3.0 * 2.0 + 3.0 * 7.0)


def test_reduce_loss_attributes_custom_drift_to_paired_judge(tmp_path: Path) -> None:
    """A custom DriftDetected is attributed to the judge_name of its paired
    JudgementEmitted."""
    events = [
        _judgement_emitted("slide_quality", severity="warning", seq=0),
        _drift_detected("DRIFT_KIND_CUSTOM", severity="DRIFT_SEVERITY_WARNING", seq=1),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    # The drift is bucketed under the namespaced custom kind, not bare "custom".
    assert by_key == {("custom:slide_quality", "warning"): 1}


def test_reduce_loss_two_custom_judges_score_independently(tmp_path: Path) -> None:
    """Two custom judges with distinct judge_names + per_judge_weights produce
    independent drift_loss contributions."""
    events = [
        _judgement_emitted("judge_a", severity="warning", seq=0),
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=1),
        _judgement_emitted("judge_b", severity="critical", seq=2),
        _drift_detected(severity="DRIFT_SEVERITY_CRITICAL", seq=3),
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    weights = ScoringWeights(per_judge_weights={"judge_a": 2.0, "judge_b": 5.0})
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {
        ("custom:judge_a", "warning"): 1,
        ("custom:judge_b", "critical"): 1,
    }
    # drift_loss: judge_a -> 3.0(warning) * 2.0 ; judge_b -> 10.0(critical) * 5.0
    assert profile.drift_loss == pytest.approx(3.0 * 2.0 + 10.0 * 5.0)


def test_reduce_loss_custom_drift_without_judgement_uses_default(tmp_path: Path) -> None:
    """A custom DriftDetected with no paired JudgementEmitted stays bare
    "custom" and scores at default_judge_weight."""
    events = [_drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=0)]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    weights = ScoringWeights(
        per_judge_weights={"judge_a": 9.0},
        default_judge_weight=3.0,
    )
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {("custom", "warning"): 1}
    # drift_loss: 3.0 (warning severity) * 3.0 (default_judge_weight) = 9.0
    assert profile.drift_loss == pytest.approx(9.0)


def test_reduce_loss_non_drift_judgement_does_not_attribute(tmp_path: Path) -> None:
    """A rubric/boolean/numeric JudgementEmitted does not pair with a later
    custom drift — only drift-flavoured judgements do."""
    events = [
        # A rubric verdict: verdict_kind != "drift". Mints no DriftDetected.
        {
            "event_id": "j0",
            "run_id": "run-J",
            "sequence": 0,
            "judgement_emitted": {
                "judge_name": "rubric_judge",
                "verdict_kind": "rubric",
                "rubric_score": 0.8,
            },
        },
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=1),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    # The rubric judgement must NOT have been mis-attributed: the custom
    # drift stays bare "custom".
    assert by_key == {("custom", "warning"): 1}


def test_reduce_loss_judgement_pairs_only_with_next_drift(tmp_path: Path) -> None:
    """One judgement pairs with exactly one (the next) DriftDetected — a second
    custom drift does not inherit a stale judge_name."""
    events = [
        _judgement_emitted("judge_a", severity="warning", seq=0),
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=1),
        # No judgement before this second custom drift.
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=2),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    # First custom drift -> judge_a; second -> unattributed bare "custom".
    assert by_key == {
        ("custom:judge_a", "warning"): 1,
        ("custom", "warning"): 1,
    }


def test_reduce_loss_first_class_drift_consumes_pending_judgement(tmp_path: Path) -> None:
    """A custom judge emitting a FIRST-CLASS drift kind pairs its judgement with
    that drift; a later bare custom drift is not mis-attributed to it."""
    events = [
        # Judge emits a drift-flavoured verdict for a first-class kind.
        _judgement_emitted("judge_a", drift_kind="off_topic", severity="warning", seq=0),
        _drift_detected("DRIFT_KIND_OFF_TOPIC", severity="DRIFT_SEVERITY_WARNING", seq=1),
        # Later, an unrelated custom drift with no judgement of its own.
        _drift_detected("DRIFT_KIND_CUSTOM", severity="DRIFT_SEVERITY_INFO", seq=2),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    # off_topic stays first-class; the custom drift is NOT attributed to
    # judge_a (whose judgement was consumed by the off_topic drift).
    assert by_key == {
        ("off_topic", "warning"): 1,
        ("custom", "info"): 1,
    }


def test_reduce_loss_no_custom_judges_back_compat(tmp_path: Path) -> None:
    """Boards with no custom judges reduce exactly as before — the custom-judge
    attribution path is inert when no custom drift / judgement is present."""
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
                "kind": "DRIFT_KIND_LOOPING_REASONING",
                "severity": "DRIFT_SEVERITY_CRITICAL",
                "detail": "loop",
            },
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {
        ("off_topic", "warning"): 1,
        ("looping_reasoning", "critical"): 1,
    }
    # Loss = 3*1 (warning) + 10*1 (critical) = 13 — unchanged from the
    # pre-custom-judge formula.
    assert profile.drift_loss == pytest.approx(13.0)


def test_reduce_loss_custom_drift_appears_in_metric_counts(tmp_path: Path) -> None:
    """The attributed custom drift shows up in metric_counts under the
    ``drift:`` namespace, carrying the judge name."""
    events = [
        _judgement_emitted("slide_quality", severity="warning", seq=0),
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=1),
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
    names = {m.name for m in profile.metric_counts}
    # MetricCount.from_drift_count prefixes "drift:" — the judge identity
    # rides inside the kind segment.
    assert "drift:custom:slide_quality" in names


# ---------------------------------------------------------------------------
# camelCase payload keys — goldfive's plain-JSON wire form
# ---------------------------------------------------------------------------
#
# Goldfive's persistence sink serialises event payloads via MessageToJson,
# which renders proto field names in camelCase (``driftDetected``). When the
# reducer falls back to plain-JSON reading (the strict proto-replay path is
# not always usable — e.g. a JSONL that mixes camelCase and snake_case
# envelope shapes), the dispatch in ``reduce_loss`` must still recognise
# those keys. These tests exercise the camelCase wire form directly.


def _camel_drift_event(kind: str, severity: str, seq: int) -> dict:
    """A ``DriftDetected`` event in goldfive's camelCase MessageToJson form."""
    return {
        "eventId": f"e{seq}",
        "runId": "run-camel",
        "sequence": seq,
        "driftDetected": {"kind": kind, "severity": severity, "detail": ""},
    }


def test_reduce_loss_folds_camelcase_drift_verdicts(tmp_path: Path) -> None:
    """Drift events written in goldfive's camelCase wire form fold into drift_loss.

    Regression for F1: goldfive's persistence sink emits ``driftDetected``
    (camelCase), but the reducer dispatch keyed on the snake_case
    ``drift_detected`` — so every in-run judge verdict was silently
    dropped and the run scored ``drift_loss == 0.0``.
    """
    events = [
        _camel_drift_event("DRIFT_KIND_CAPABILITY_MISMATCH", "DRIFT_SEVERITY_CRITICAL", 0),
        _camel_drift_event("DRIFT_KIND_OFF_TOPIC", "DRIFT_SEVERITY_WARNING", 1),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {
        ("capability_mismatch", "critical"): 1,
        ("off_topic", "warning"): 1,
    }
    # Loss = 10*1 (critical) + 3*1 (warning) = 13, strictly > 0.
    assert profile.drift_loss == pytest.approx(13.0)
    assert profile.drift_loss > 0.0
    # The run_id is read from the camelCase ``runId`` envelope key.
    assert profile.run_id == "run-camel"


def test_reduce_loss_camelcase_judgement_attributes_custom_drift(tmp_path: Path) -> None:
    """A camelCase ``judgementEmitted`` pairs with the next camelCase drift.

    Custom-judge attribution must work in the camelCase wire form too:
    the paired ``judgeName`` is folded into the ``custom:<judge_name>``
    drift kind exactly as in the snake_case path.
    """
    events = [
        {
            "eventId": "j0",
            "runId": "run-camel",
            "sequence": 0,
            "judgementEmitted": {
                "judgeName": "slide_quality",
                "verdictKind": "drift",
                "driftKind": "custom",
                "severity": "warning",
            },
        },
        _camel_drift_event("DRIFT_KIND_CUSTOM", "DRIFT_SEVERITY_WARNING", 1),
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
    by_key = {(c.kind, c.severity): c.count for c in profile.drift_counts}
    assert by_key == {("custom:slide_quality", "warning"): 1}
    assert profile.drift_loss > 0.0


def test_reduce_loss_camelcase_plan_revisions_and_llm_calls(tmp_path: Path) -> None:
    """camelCase ``planRevised`` / ``goldfiveLlmCallEnd`` events are counted.

    The camelCase-vs-snake_case bug dropped *every* event kind in
    plain-JSON mode, not just drift — so plan-revision and llm-call
    counts also silently zeroed. This pins the broader fix.
    """
    events = [
        {"eventId": "r0", "runId": "run-camel", "sequence": 0, "planRevised": {"reason": "x"}},
        {
            "eventId": "l0",
            "runId": "run-camel",
            "sequence": 1,
            "goldfiveLlmCallEnd": {"name": "step", "spanId": "s1"},
        },
        {
            "eventId": "l1",
            "runId": "run-camel",
            "sequence": 2,
            "goldfiveLlmCallEnd": {"name": "step", "spanId": "s2"},
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
    assert profile.plan_revisions == 1
    llm = {m.name: m.count for m in profile.metric_counts}
    assert llm["cost:llm_calls"] == pytest.approx(2.0)
    # plan_revision_weight default is 0.5 → loss reflects the revision.
    assert profile.drift_loss == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Not-completed penalty — F3: failure modes scored uniformly, never 0.0
# ---------------------------------------------------------------------------


def test_not_completed_penalty_keyed_off_severity_weights() -> None:
    """``not_completed_penalty`` is ``5.0 * max(severity_weights)``."""
    from zicato.telemetry.reducer import not_completed_penalty

    w = ScoringWeights(severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0})
    assert not_completed_penalty(w) == pytest.approx(50.0)
    # An epoch with no severity weights still gets a non-trivial penalty.
    w_empty = ScoringWeights(severity_weights={})
    assert not_completed_penalty(w_empty) == pytest.approx(5.0)


def test_reduce_loss_run_not_completed_penalises_crashed_run(tmp_path: Path) -> None:
    """A crashed run (empty events, run_not_completed=True) is scored worst-case.

    Regression for F3: a run that fails instantly by exception leaves an
    empty events file. Without the not-completed penalty it would score
    ``drift_loss == 0.0`` — the BEST possible score — letting a
    challenger generation win a tournament by crashing fast.
    """
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    weights = _default_weights()
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=1,
        wall_clock_budget_exceeded=False,
        weights=weights,
        run_not_completed=True,
    )
    # Heavy term 50.0 + floored task_failure_ratio 1.0 * 10.0 = 60.0.
    assert profile.drift_loss == pytest.approx(60.0)
    assert profile.drift_loss > 0.0
    assert profile.task_failure_ratio == pytest.approx(1.0)


def test_reduce_loss_killed_crashed_aborted_all_penalised(tmp_path: Path) -> None:
    """Every non-success terminal state lands the SAME penalty — none 0.0.

    Killed (wall_clock_budget_exceeded), crashed / aborted / errored
    (run_not_completed) all score identically, so no failure mode is
    cheaper than another and a fast crash cannot beat a slow kill.
    """
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    weights = _default_weights()

    def _loss(*, budget: bool, not_completed: bool) -> LossProfile:
        return reduce_loss(
            events_jsonl_path=p,
            entry=_single_turn_entry(),
            generation_id="v0",
            epoch_id="ep1",
            expectation_result=None,
            runtime_ms=1,
            wall_clock_budget_exceeded=budget,
            weights=weights,
            run_not_completed=not_completed,
        )

    killed = _loss(budget=True, not_completed=False)
    crashed = _loss(budget=False, not_completed=True)
    both = _loss(budget=True, not_completed=True)
    completed = _loss(budget=False, not_completed=False)

    # None of the non-success states score the best-possible 0.0.
    for label, prof in (("killed", killed), ("crashed", crashed), ("both", both)):
        assert prof.drift_loss > 0.0, f"{label} run scored 0.0 drift_loss"
    # All non-success states score IDENTICALLY — the penalty is applied
    # exactly once even when both flags are set.
    assert killed.drift_loss == pytest.approx(crashed.drift_loss)
    assert both.drift_loss == pytest.approx(killed.drift_loss)
    # The completed run is the only one that may score 0.0.
    assert completed.drift_loss == pytest.approx(0.0)


def test_reduce_loss_run_not_completed_with_drift_stacks(tmp_path: Path) -> None:
    """A not-completed run that ALSO emitted drift stacks both contributions.

    The not-completed penalty is additive on top of any drift the run
    managed to emit before failing — it does not replace the drift loss.
    """
    events = [
        _camel_drift_event("DRIFT_KIND_OFF_TOPIC", "DRIFT_SEVERITY_WARNING", 0),
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
        runtime_ms=1,
        wall_clock_budget_exceeded=False,
        weights=weights,
        run_not_completed=True,
    )
    # drift 3.0 (warning) + heavy term 50.0 + task_failure 10.0 = 63.0.
    assert profile.drift_loss == pytest.approx(63.0)


def test_reduce_loss_completed_run_unaffected_by_default(tmp_path: Path) -> None:
    """A run that completed cleanly is scored exactly as before.

    ``run_not_completed`` defaults to False; a completed run with no
    drift still scores ``drift_loss == 0.0`` — back-compat for every
    existing caller.
    """
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, [])
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_default_weights(),
    )
    assert profile.drift_loss == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ADK session id — captured from the events.jsonl envelope
# ---------------------------------------------------------------------------
#
# goldfive carries ``sessionId`` (camelCase) and ``session_id`` (snake_case)
# on every event envelope. The reducer must capture the first non-empty
# value it sees and persist it on LossProfile.adk_session_id so the
# dashboard can build harmonograf deep-links.


def test_reduce_loss_captures_adk_session_id_camel(tmp_path: Path) -> None:
    """``sessionId`` (camelCase) on the first event is stored as ``adk_session_id``."""
    events = [
        {
            "eventId": "e0",
            "runId": "run-A",
            "sessionId": "session-camel-123",
            "sequence": 0,
            "runStarted": {"goalSummary": "test"},
        }
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
    assert profile.adk_session_id == "session-camel-123"


def test_reduce_loss_captures_adk_session_id_snake(tmp_path: Path) -> None:
    """``session_id`` (snake_case) is accepted as a fallback when ``sessionId`` is absent."""
    events = [
        {
            "event_id": "e0",
            "run_id": "run-B",
            "session_id": "session-snake-456",
            "sequence": 0,
            "run_started": {"goal_summary": "test"},
        }
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
    assert profile.adk_session_id == "session-snake-456"


def test_reduce_loss_adk_session_id_empty_when_absent(tmp_path: Path) -> None:
    """No ``sessionId`` / ``session_id`` envelope key → ``adk_session_id`` defaults to ``""``."""
    events = [
        {
            "event_id": "e0",
            "run_id": "run-C",
            "sequence": 0,
            "run_started": {"goal_summary": "no session"},
        }
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
    assert profile.adk_session_id == ""


def test_loss_profile_round_trip_with_adk_session_id(tmp_path: Path) -> None:
    """``adk_session_id`` survives ``write_loss_profile`` / ``read_loss_profile``."""
    profile = LossProfile(
        run_id="r-adk",
        entry_id="ent-adk",
        generation_id="v0",
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
        adk_session_id="abc123def456",
    )
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    loaded = read_loss_profile(p)
    assert loaded == profile
    assert loaded.adk_session_id == "abc123def456"


def test_read_loss_profile_back_compat_missing_adk_session_id(tmp_path: Path) -> None:
    """An old ``loss.json`` without ``adk_session_id`` loads with the default ``""``."""
    # Write a loss.json that predates the adk_session_id field.
    import json as _json

    old_loss = {
        "run_id": "old-run",
        "entry_id": "ent-old",
        "generation_id": "v0",
        "epoch_id": "ep1",
        "drift_counts": [],
        "plan_revisions": 0,
        "task_failure_ratio": 0.0,
        "runtime_ms": 100,
        "wall_clock_budget_exceeded": False,
        "expectation_result": None,
        "drift_loss": 0.0,
        "pass_fail": None,
        "turns_completed": None,
        "memory_failure_count": None,
        "context_loss_count": None,
        # intentionally omits adk_session_id
    }
    p = tmp_path / "loss.json"
    p.write_text(_json.dumps(old_loss), encoding="utf-8")
    loaded = read_loss_profile(p)
    assert loaded.adk_session_id == ""
    assert loaded.task_failure_ratio == pytest.approx(0.0)
