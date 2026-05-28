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


def test_conversation_started_without_sequence_sorts_first(tmp_path: Path) -> None:
    # goldfive's persistence sink emits ``conversation_started`` OUTSIDE
    # the per-run sequence stream: the event carries only an
    # ``emittedAt`` timestamp and no ``sequence`` field. The reconstructed
    # transcript must surface that synthetic "conversation started" turn
    # at the START of the rendered order (chronological), not the END
    # (which is what a naïve sequence-first / sequence-less-sorts-last
    # ordering produces). The downstream conversation view renders
    # turns in the order this module yields them, so this is the
    # authoritative test for the invariant.
    lines = [
        # The real sink writes conversation_started with no `sequence`
        # field — modelled exactly here.
        _camel(
            "conversationStarted",
            {"conversationId": "c1"},
            runId="r",
            emittedAt="2026-05-16T00:00:00.000Z",
        ),
        _camel(
            "runStarted",
            {"goalSummary": "build a thing"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:00.100Z",
        ),
        _camel(
            "goldfiveLlmCallStart",
            {"name": "judge_reasoning", "inputPreview": "first thought", "targetAgentId": "alpha"},
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "goldfiveLlmCallEnd",
            {
                "name": "judge_reasoning",
                "decisionSummary": "final thought",
                "targetAgentId": "alpha",
            },
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "done"},
            runId="r",
            sequence="4",
            emittedAt="2026-05-16T00:00:03Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    # The synthetic "conversation started" turn sits FIRST in the
    # transcript — not at the tail. The rest of the events follow in
    # their natural chronological order.
    assert t.turns, "transcript must produce at least one turn"
    first = t.turns[0]
    assert first.role == "system"
    assert first.text == "conversation started", (
        f"expected the synthetic conversation_started turn first, "
        f"got role={first.role!r} kind={first.kind!r} text={first.text!r}"
    )

    # Real-event order is preserved after the synthetic frame.
    roles = [turn.role for turn in t.turns]
    assert roles == [
        "system",
        "user",
        "agent",
        "system",
    ], f"expected [system, user, agent, system] but got {roles}"
    # The agent turn carries content from the two consecutive llm-call
    # events, in their emitted order.
    agent_text = t.turns[2].text
    assert "first thought" in agent_text and "final thought" in agent_text
    assert agent_text.index("first thought") < agent_text.index("final thought")
    # The terminal run_completed turn sits LAST, not the conversation
    # frame.
    assert t.turns[-1].text == "done"


def test_conversation_started_first_even_when_listed_late(tmp_path: Path) -> None:
    # Same as above but with the conversation_started line appearing in
    # the MIDDLE of the file (an unlikely append order, but a robust
    # test). The reconstructor must still float it to the top because
    # its timestamp precedes every numbered event.
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "g"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:00.500Z",
        ),
        _camel(
            "conversationStarted",
            {"conversationId": "c1"},
            runId="r",
            emittedAt="2026-05-16T00:00:00.100Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "ok"},
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:01Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))
    assert t.turns[0].text == "conversation started"
    assert t.turns[-1].text == "ok"


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
# Multi-run grouping (multi_turn_emulated board entries)
# ---------------------------------------------------------------------------


def _run_group(run_id: str, base_secs: int, prompt: str, reply: str) -> list[str]:
    """Synthesize the canonical 4-event shape of one goldfive run.

    ``conversation_started`` (no ``sequence``, lifecycle frame from the
    persistence sink) + ``run_started`` (seq=0) + a single
    ``goldfive_llm_call_end`` (seq=1, the agent's reply) +
    ``run_completed`` (seq=2). The four events all carry the same
    ``runId``. The base second offsets the lifecycle timestamps so the
    test can build groups with interleaved-yet-distinct chronologies.
    """

    def _ts(offset: float) -> str:
        secs = base_secs + offset
        whole = int(secs)
        frac = secs - whole
        if frac > 0:
            return f"2026-05-19T00:00:{whole:02d}.{int(frac * 1000):03d}Z"
        return f"2026-05-19T00:00:{whole:02d}Z"

    return [
        _camel(
            "conversationStarted",
            {"conversationId": f"c-{run_id}"},
            runId=run_id,
            emittedAt=_ts(0.0),
        ),
        _camel(
            "runStarted",
            {"goalSummary": prompt},
            runId=run_id,
            sequence="0",
            emittedAt=_ts(0.1),
        ),
        _camel(
            "goldfiveLlmCallEnd",
            {"decisionSummary": reply, "targetAgentId": "alpha"},
            runId=run_id,
            sequence="1",
            emittedAt=_ts(0.5),
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": f"run {run_id} done"},
            runId=run_id,
            sequence="2",
            emittedAt=_ts(0.9),
        ),
    ]


def test_multi_run_events_group_per_run_id_and_sort_by_min_emitted_at(
    tmp_path: Path,
) -> None:
    # Bug #172: a ``multi_turn_emulated`` board entry writes N goldfive
    # runs (one per emulated user turn) into one events file. Each run
    # has its own ``conversation_started`` lifecycle frame (no
    # sequence). A flat sort lifted ALL of them to the top, then
    # interleaved the sequenced events. The fix groups events by
    # ``run_id`` first, then sorts groups by min-emittedAt, then within
    # each group applies the original "seq=None first, then by sequence"
    # ordering.
    #
    # Three runs, written deliberately OUT OF chronological order in
    # the file: run_b begins at T=10, run_a begins at T=0, run_c begins
    # at T=20. The grouping logic must reorder the GROUPS so the
    # rendered output reads run_a → run_b → run_c.
    lines = (
        _run_group("run_b", base_secs=10, prompt="prompt B", reply="reply B")
        + _run_group("run_a", base_secs=0, prompt="prompt A", reply="reply A")
        + _run_group("run_c", base_secs=20, prompt="prompt C", reply="reply C")
    )
    t = reconstruct_transcript(_write(tmp_path, lines))

    # Three lifecycle "conversation started" turns appear — one per
    # run — NOT three stacked at the top followed by interleaved bodies.
    cs_turns = [turn for turn in t.turns if turn.text == "conversation started"]
    assert len(cs_turns) == 3, (
        f"expected exactly 3 conversation_started turns (one per run), " f"got {len(cs_turns)}"
    )

    # The flat ``turns`` list groups each run's turns CONTIGUOUSLY, in
    # chronological run order. Every turn carries the 1-based
    # ``run_index`` of its group.
    indices = [turn.run_index for turn in t.turns]
    assert indices == sorted(indices), (
        f"run_index must be non-decreasing across the flat turn list, " f"got {indices}"
    )
    # Specifically: 1,1,1,1, 2,2,2,2, 3,3,3,3 (four turns per group:
    # conversation_started + run_started + agent reply + run_completed).
    assert (
        indices == [1] * 4 + [2] * 4 + [3] * 4
    ), f"expected four turns per run group in run-index order, got {indices}"

    # Each run group starts with its own conversation_started frame.
    assert t.turns[0].text == "conversation started"
    assert t.turns[4].text == "conversation started"
    assert t.turns[8].text == "conversation started"

    # And each group's run_id is the one of the goldfive run that owns it.
    assert t.turns[0].run_id == "run_a"
    assert t.turns[4].run_id == "run_b"
    assert t.turns[8].run_id == "run_c"

    # The user prompts appear in run order (A, then B, then C),
    # interleaved CORRECTLY with the agent replies, not scrambled.
    user_prompts = [turn.text for turn in t.turns if turn.role == "user"]
    assert user_prompts == [
        "prompt A",
        "prompt B",
        "prompt C",
    ], f"user prompts must appear in chronological run order, got {user_prompts}"
    agent_replies = [
        turn.text for turn in t.turns if turn.role == "agent" and turn.text.startswith("reply")
    ]
    assert agent_replies == ["reply A", "reply B", "reply C"]

    # The transcript-level run_id picks the chronologically earliest run.
    assert t.run_id == "run_a"


def test_single_run_events_emit_run_index_one_on_every_turn(tmp_path: Path) -> None:
    # Single-run files (the common case) must collapse to a single
    # group; every turn carries ``run_index == 1`` and the prior
    # "conversation_started first" invariant holds verbatim.
    lines = _run_group("only_run", base_secs=0, prompt="hi", reply="hello")
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert t.run_id == "only_run"
    assert all(turn.run_index == 1 for turn in t.turns), (
        f"single-run transcript must stamp run_index=1 on every turn, "
        f"got {[turn.run_index for turn in t.turns]}"
    )
    assert all(turn.run_id == "only_run" for turn in t.turns)
    # And the "conversation started" frame is FIRST, same as before.
    assert t.turns[0].text == "conversation started"


def test_multi_run_delegation_does_not_leak_across_run_boundaries(
    tmp_path: Path,
) -> None:
    # A delegation in run 1 must never match an
    # ``agent_invocation_completed`` from run 2: a multi-run grouping
    # that left state shared across runs would scramble tool-call
    # pairing too. Construct two runs where each has its own
    # coordinator → worker delegation and assert the calls pair within
    # their OWN run.
    def _delegating_run(run_id: str, base_secs: int, work: str) -> list[str]:
        def _ts(offset: float) -> str:
            secs = base_secs + offset
            whole = int(secs)
            return f"2026-05-19T00:00:{whole:02d}Z"

        return [
            _camel(
                "conversationStarted",
                {"conversationId": f"c-{run_id}"},
                runId=run_id,
                emittedAt=_ts(0),
            ),
            _camel(
                "runStarted",
                {"goalSummary": f"goal {run_id}"},
                runId=run_id,
                sequence="0",
                emittedAt=_ts(1),
            ),
            _camel(
                "delegationObserved",
                {
                    "fromAgent": "coordinator",
                    "toAgent": "worker",
                    "taskId": f"task-{run_id}",
                    "toolArgsJson": json.dumps({"work": work}),
                },
                runId=run_id,
                sequence="1",
                emittedAt=_ts(2),
            ),
            _camel(
                "agentInvocationCompleted",
                {
                    "agentName": "worker",
                    "summary": f"done {work}",
                    "taskId": f"task-{run_id}",
                },
                runId=run_id,
                sequence="2",
                emittedAt=_ts(3),
            ),
            _camel(
                "runCompleted",
                {"outcomeSummary": f"{run_id} ok"},
                runId=run_id,
                sequence="3",
                emittedAt=_ts(4),
            ),
        ]

    lines = _delegating_run("r1", base_secs=0, work="alpha") + _delegating_run(
        "r2", base_secs=10, work="beta"
    )
    t = reconstruct_transcript(_write(tmp_path, lines))

    # The coordinator turn for r1 has a tool-result paired to its OWN
    # work ("done alpha"), not r2's "done beta".
    r1_coord = next(turn for turn in t.turns if turn.run_index == 1 and turn.agent == "coordinator")
    r2_coord = next(turn for turn in t.turns if turn.run_index == 2 and turn.agent == "coordinator")
    assert len(r1_coord.tool_results) == 1
    assert r1_coord.tool_results[0]["result"] == "done alpha"
    assert len(r2_coord.tool_results) == 1
    assert r2_coord.tool_results[0]["result"] == "done beta"


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
    turn_keys = {
        "seq",
        "ts",
        "agent",
        "role",
        "kind",
        "text",
        "tool_calls",
        "tool_results",
        "run_id",
        "run_index",
    }
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
