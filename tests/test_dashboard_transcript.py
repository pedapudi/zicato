"""Tests for the dashboard conversation-reconstruction module.

Synthetic ``events.jsonl`` fixtures exercise the two goldfive envelope
shapes (camelCase top-level payload key vs. normalized ``{kind, payload}``),
both timestamp encodings (RFC-3339 string vs. proto ``{seconds, nanos}``),
turn grouping, tool call/result pairing, annotation anchoring, malformed /
truncated line tolerance, and the missing-file path. A final test runs the
parser against a real tournament telemetry file when one is present.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

from zicato.dashboard.transcript import (
    Annotation,
    Transcript,
    Turn,
    reconstruct_transcript,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, lines: list[str], name: str = "events.jsonl") -> Path:
    """Write raw JSONL ``lines`` to ``tmp_path/name`` and return the path."""

    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _camel(payload_key: str, payload: dict, **envelope) -> str:
    """One camelCase-shape event line (top-level payload key)."""

    obj = {payload_key: payload}
    obj.update(envelope)
    return json.dumps(obj)


def _normalized(kind: str, payload: dict, **envelope) -> str:
    """One normalized-shape event line (``{kind, payload, ...}``)."""

    obj = {"kind": kind, "payload": payload}
    obj.update(envelope)
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# Missing / empty file
# ---------------------------------------------------------------------------


def test_missing_file_yields_empty_transcript(tmp_path: Path) -> None:
    t = reconstruct_transcript(tmp_path / "does_not_exist.jsonl")
    assert isinstance(t, Transcript)
    assert t.turns == []
    assert t.annotations == []
    assert t.run_id is None
    assert t.event_count == 0
    assert t.complete is False


def test_empty_file_yields_empty_transcript(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("", encoding="utf-8")
    t = reconstruct_transcript(path)
    assert t.turns == []
    assert t.event_count == 0
    assert t.complete is False


# ---------------------------------------------------------------------------
# Basic agent messages + ordering
# ---------------------------------------------------------------------------


def test_agent_messages_and_turn_ordering(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "Build a thing"},
            runId="r1",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        # Two consecutive same-agent reasoning events -> one turn.
        _camel(
            "goldfiveLlmCallStart",
            {"name": "judge_reasoning", "inputPreview": "first thought", "targetAgentId": "alpha"},
            runId="r1",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "goldfiveLlmCallEnd",
            {
                "name": "judge_reasoning",
                "decisionSummary": "second thought",
                "targetAgentId": "alpha",
            },
            runId="r1",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        # Different agent -> new turn.
        _camel(
            "goldfiveLlmCallStart",
            {"name": "judge_reasoning", "inputPreview": "beta thought", "targetAgentId": "beta"},
            runId="r1",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "done"},
            runId="r1",
            sequence="4",
            emittedAt="2026-05-16T00:00:04Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert t.run_id == "r1"
    assert t.event_count == 5
    assert t.complete is True

    # user(run_started), agent(alpha merged), agent(beta), system(run_completed)
    assert [turn.role for turn in t.turns] == ["user", "agent", "agent", "system"]
    assert t.turns[0].text == "Build a thing"

    alpha = t.turns[1]
    assert alpha.agent == "alpha"
    assert "first thought" in alpha.text and "second thought" in alpha.text
    assert t.turns[2].agent == "beta"
    assert t.turns[3].text == "done"

    # Turns are seq-ordered.
    seqs = [turn.seq for turn in t.turns]
    assert seqs == sorted(seqs)


def test_ordering_by_sequence_not_file_order(tmp_path: Path) -> None:
    # Lines written out of sequence order; reconstruction reorders by seq.
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "runStarted",
            {"goalSummary": "first"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "x"},
            runId="r",
            sequence="9",
            emittedAt="2026-05-16T00:00:09Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    assert [turn.seq for turn in t.turns] == [0, 2, 9]
    assert t.turns[0].text == "first"


# ---------------------------------------------------------------------------
# Tool call + result pairing
# ---------------------------------------------------------------------------


def test_tool_call_and_result_pairing(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "delegate"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        # Coordinator delegates to a sub-agent: a tool call.
        _camel(
            "delegationObserved",
            {
                "fromAgent": "coordinator",
                "toAgent": "worker",
                "taskId": "t1",
                "toolArgsJson": json.dumps({"request": "do work"}),
            },
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        # Sub-agent completes: its result pairs back to the call.
        _camel(
            "agentInvocationCompleted",
            {"agentName": "worker", "summary": "work finished", "taskId": "t1"},
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "ok"},
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    # Find the coordinator turn that issued the delegation.
    coord = next(turn for turn in t.turns if turn.agent == "coordinator")
    assert len(coord.tool_calls) == 1
    call = coord.tool_calls[0]
    assert call["name"] == "worker"
    assert call["args"] == {"request": "do work"}

    assert len(coord.tool_results) == 1
    result = coord.tool_results[0]
    assert result["name"] == "worker"
    assert result["result"] == "work finished"


# ---------------------------------------------------------------------------
# Drift + steering annotations
# ---------------------------------------------------------------------------


def test_drift_detection_becomes_annotation(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "goldfiveLlmCallStart",
            {
                "name": "judge_reasoning",
                "inputPreview": "off topic stuff",
                "targetAgentId": "alpha",
            },
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "driftDetected",
            {
                "kind": "DRIFT_KIND_OFF_TOPIC",
                "severity": "DRIFT_SEVERITY_WARNING",
                "detail": "agent wandered off the assigned task",
                "currentAgentId": "alpha",
                "id": "drift-1",
            },
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "ok"},
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    drift = [a for a in t.annotations if a.kind == "drift"]
    assert len(drift) == 1
    ann = drift[0]
    assert "off_topic" in ann.summary
    assert "wandered off" in ann.summary
    # Anchored to the agent turn that preceded it (seq 1).
    assert ann.anchor_seq == 1
    assert ann.detail["event_kind"] == "drift_detected"
    assert ann.detail["id"] == "drift-1"


def test_steering_decision_becomes_annotation(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "goldfiveLlmCallStart",
            {"name": "judge_reasoning", "inputPreview": "thinking", "targetAgentId": "alpha"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "steeringDecisionMade",
            {
                "detectorName": "reasoning_judge",
                "outcome": "no_drift",
                "reason": "judge verdict: on_task",
                "agentName": "alpha",
            },
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    steering = [a for a in t.annotations if a.kind == "steering"]
    assert len(steering) == 1
    assert "reasoning_judge" in steering[0].summary
    assert "no_drift" in steering[0].summary
    assert steering[0].anchor_seq == 1


# ---------------------------------------------------------------------------
# Envelope-shape and timestamp variety
# ---------------------------------------------------------------------------


def test_camelcase_and_snakecase_keys_both_accepted(tmp_path: Path) -> None:
    lines = [
        # camelCase top-level payload key.
        _camel(
            "runStarted",
            {"goalSummary": "camel goal"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        # Normalized {kind, payload} shape with snake_case payload fields.
        _normalized(
            "goldfive_llm_call_start",
            {
                "name": "judge_reasoning",
                "input_preview": "snake thought",
                "target_agent_id": "alpha",
            },
            run_id="r",
            sequence=1,
            emitted_at="2026-05-16T00:00:01Z",
        ),
        # Normalized shape using snake_case envelope, camelCase payload
        # (defensive: producers are inconsistent).
        _normalized(
            "task_completed",
            {"taskId": "t1", "summary": "task output text"},
            run_id="r",
            sequence=2,
            emitted_at="2026-05-16T00:00:02Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    assert t.run_id == "r"
    assert t.turns[0].text == "camel goal"
    texts = "\n".join(turn.text for turn in t.turns)
    assert "snake thought" in texts
    assert "task output text" in texts


def test_proto_and_rfc3339_timestamps_both_parsed(tmp_path: Path) -> None:
    # 1778906336 seconds == 2026-05-16T04:38:56 UTC.
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T04:38:56.123456Z",
        ),
        _normalized(
            "task_completed",
            {"taskId": "t1", "summary": "done"},
            run_id="r",
            sequence=1,
            emitted_at={"seconds": 1778906336, "nanos": 500000000},
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    # RFC-3339 string passes through verbatim.
    assert t.turns[0].ts == "2026-05-16T04:38:56.123456Z"
    # Proto {seconds, nanos} is rendered to an RFC-3339 string.
    proto_turn = t.turns[1]
    assert proto_turn.ts is not None
    assert proto_turn.ts.startswith("2026-05-16T04:38:56")
    assert proto_turn.ts.endswith("Z")
    assert ".5" in proto_turn.ts  # 500_000_000 nanos -> .5


# ---------------------------------------------------------------------------
# Malformed / truncated tolerance
# ---------------------------------------------------------------------------


def test_truncated_final_line_tolerated(tmp_path: Path) -> None:
    good = _camel(
        "runStarted",
        {"goalSummary": "g"},
        runId="r",
        sequence="0",
        emittedAt="2026-05-16T00:00:00Z",
    )
    good2 = _camel(
        "goldfiveLlmCallStart",
        {"name": "judge_reasoning", "inputPreview": "thought", "targetAgentId": "alpha"},
        runId="r",
        sequence="1",
        emittedAt="2026-05-16T00:00:01Z",
    )
    # A writer caught mid-flush leaves a half-written final line.
    truncated = '{"runCompleted": {"outcomeSumm'
    path = tmp_path / "events.jsonl"
    path.write_text(good + "\n" + good2 + "\n" + truncated, encoding="utf-8")

    t = reconstruct_transcript(path, partial_ok=True)
    # The two good lines parsed; the bad final line was skipped.
    assert t.event_count == 2
    assert any(turn.text == "thought" for turn in t.turns)
    # No terminal event seen AND a truncated tail -> not complete.
    assert t.complete is False


def test_malformed_interior_line_skipped_not_fatal(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        "{ this is not json at all",
        "42",  # valid JSON but not a dict -> skipped
        _camel(
            "runCompleted",
            {"outcomeSummary": "ok"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    # Only the two dict lines counted.
    assert t.event_count == 2
    assert t.complete is True
    assert t.turns[-1].text == "ok"


def test_in_progress_run_without_terminal_is_incomplete(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "goldfiveLlmCallStart",
            {"name": "judge_reasoning", "inputPreview": "still going", "targetAgentId": "alpha"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    assert t.complete is False
    assert t.event_count == 2


# ---------------------------------------------------------------------------
# Annotation anchoring edge case
# ---------------------------------------------------------------------------


def test_annotation_before_any_turn_anchors_to_first_turn(tmp_path: Path) -> None:
    # A plan_submitted before any conversation content: its anchor must
    # not dangle as None once turns exist.
    lines = [
        _normalized(
            "plan_submitted",
            {"plan": {"revision_index": 0, "tasks": [{"id": "a", "title": "A"}]}},
            run_id="r",
            sequence=0,
            emitted_at="2026-05-16T00:00:00Z",
        ),
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    plan_anns = [a for a in t.annotations if a.kind == "plan"]
    assert len(plan_anns) == 1
    assert t.turns  # there is at least one turn
    assert plan_anns[0].anchor_seq == t.turns[0].seq


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serializable(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "delegationObserved",
            {
                "fromAgent": "c",
                "toAgent": "w",
                "taskId": "t",
                "toolArgsJson": json.dumps({"k": "v"}),
            },
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "driftDetected",
            {
                "kind": "DRIFT_KIND_OFF_TOPIC",
                "severity": "DRIFT_SEVERITY_INFO",
                "detail": "d",
                "id": "x",
            },
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "ok"},
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    d = t.to_dict()

    # Round-trips through JSON cleanly.
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["run_id"] == "r"
    assert decoded["complete"] is True
    assert decoded["event_count"] == 4
    assert isinstance(decoded["turns"], list)
    assert isinstance(decoded["annotations"], list)

    # Turn / Annotation shapes match the documented contract.
    turn_keys = {"seq", "ts", "agent", "role", "kind", "text", "tool_calls", "tool_results"}
    for turn in decoded["turns"]:
        assert set(turn.keys()) == turn_keys
    ann_keys = {"kind", "ts", "summary", "anchor_seq", "detail"}
    for ann in decoded["annotations"]:
        assert set(ann.keys()) == ann_keys

    # Nested to_dict() also works in isolation.
    assert Turn().to_dict()["tool_calls"] == []
    assert Annotation().to_dict()["detail"] == {}


# ---------------------------------------------------------------------------
# Real telemetry (skipped when no tournament file is present)
# ---------------------------------------------------------------------------


def _find_real_events_file() -> Path | None:
    matches = sorted(glob.glob("/tmp/zicato-tournament3/.zicato/**/events.jsonl", recursive=True))
    for candidate in matches:
        path = Path(candidate)
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def test_reconstructs_a_real_tournament_file() -> None:
    real = _find_real_events_file()
    if real is None:
        pytest.skip("no real /tmp/zicato-tournament3 events.jsonl present")

    t = reconstruct_transcript(real)
    assert isinstance(t, Transcript)
    # A real file always has a run_id and a positive event count.
    assert t.run_id
    assert t.event_count > 0
    # It produces at least some conversation content.
    assert t.turns
    # Turns are seq-ordered (None-seq turns, if any, sort last).
    seqs = [turn.seq for turn in t.turns if turn.seq is not None]
    assert seqs == sorted(seqs)
    # Every annotation anchors to a real turn seq or stays None.
    turn_seqs = {turn.seq for turn in t.turns}
    for ann in t.annotations:
        assert ann.anchor_seq is None or ann.anchor_seq in turn_seqs
    # The whole thing serializes.
    json.dumps(t.to_dict())
