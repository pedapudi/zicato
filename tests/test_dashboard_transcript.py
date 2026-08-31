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


def test_agent_messages_and_turn_ordering(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "Build a thing"},
            runId="r1",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
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

    assert [turn.role for turn in t.turns] == ["user", "agent", "agent", "system"]
    assert t.turns[0].text == "Build a thing"

    alpha = t.turns[1]
    assert alpha.agent == "alpha"
    assert "first thought" in alpha.text and "second thought" in alpha.text
    assert t.turns[2].agent == "beta"
    assert t.turns[3].text == "done"

    seqs = [turn.seq for turn in t.turns]
    assert seqs == sorted(seqs)


def test_run_started_and_goal_derived_prompt_not_duplicated(tmp_path: Path) -> None:
    prompt = "Outline a deck on quarterly metrics for Q3."
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": prompt},
            runId="r1",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "goalDerived",
            {"goals": [{"summary": prompt}]},
            runId="r1",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "runCompleted",
            {"outcomeSummary": "done"},
            runId="r1",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert [turn.role for turn in t.turns] == ["user", "system"]
    user = t.turns[0]
    assert user.text == prompt
    assert user.text.count(prompt) == 1


def test_distinct_goal_derived_text_still_appended(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "Outline a deck on Q3 metrics."},
            runId="r1",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "goalDerived",
            {"goals": [{"summary": "Also include a revenue chart."}]},
            runId="r1",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    user = t.turns[0]
    assert "Outline a deck on Q3 metrics." in user.text
    assert "Also include a revenue chart." in user.text


def test_conversation_started_without_sequence_sorts_first(tmp_path: Path) -> None:
    lines = [
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

    assert t.turns, "transcript must produce at least one turn"
    first = t.turns[0]
    assert first.role == "system"
    assert first.text == "conversation started", (
        f"expected the synthetic conversation_started turn first, "
        f"got role={first.role!r} kind={first.kind!r} text={first.text!r}"
    )

    roles = [turn.role for turn in t.turns]
    assert roles == [
        "system",
        "user",
        "agent",
        "system",
    ], f"expected [system, user, agent, system] but got {roles}"
    agent_text = t.turns[2].text
    assert "first thought" in agent_text and "final thought" in agent_text
    assert agent_text.index("first thought") < agent_text.index("final thought")
    assert t.turns[-1].text == "done"


def test_conversation_started_first_even_when_listed_late(tmp_path: Path) -> None:
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


def test_source_index_is_the_parsed_position_not_the_sequence(tmp_path: Path) -> None:
    lines = [
        _camel("runStarted", {"goalSummary": "g"}, runId="r", sequence="5"),
        _camel(
            "taskCompleted",
            {"summary": "later text", "agentName": "a"},
            runId="r",
            sequence="1",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert [turn.text for turn in t.turns] == ["later text", "g"]
    assert [turn.source_index for turn in t.turns] == [1, 0]
    assert t.event_count == 2


def test_merged_turn_carries_the_highest_source_index_it_absorbed(tmp_path: Path) -> None:
    lines = [
        _camel("goldfiveLlmCallStart", {"inputPreview": "thinking"}, runId="r", sequence="0"),
        _camel("goldfiveLlmCallEnd", {"decisionSummary": "done"}, runId="r", sequence="1"),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert len(t.turns) == 1
    assert t.turns[0].source_index == 1


def test_unparseable_line_takes_no_source_index_position(tmp_path: Path) -> None:
    lines = [
        _camel("runStarted", {"goalSummary": "g"}, runId="r", sequence="0"),
        "{ not json",
        _camel(
            "taskCompleted",
            {"summary": "s", "agentName": "a"},
            runId="r",
            sequence="1",
        ),
    ]
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert [turn.source_index for turn in t.turns] == [0, 1]
    assert t.event_count == 2


def _run_group(run_id: str, base_secs: int, prompt: str, reply: str) -> list[str]:
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
    lines = (
        _run_group("run_b", base_secs=10, prompt="prompt B", reply="reply B")
        + _run_group("run_a", base_secs=0, prompt="prompt A", reply="reply A")
        + _run_group("run_c", base_secs=20, prompt="prompt C", reply="reply C")
    )
    t = reconstruct_transcript(_write(tmp_path, lines))

    cs_turns = [turn for turn in t.turns if turn.text == "conversation started"]
    assert len(cs_turns) == 3, (
        f"expected exactly 3 conversation_started turns (one per run), " f"got {len(cs_turns)}"
    )

    indices = [turn.run_index for turn in t.turns]
    assert indices == sorted(indices), (
        f"run_index must be non-decreasing across the flat turn list, " f"got {indices}"
    )
    assert (
        indices == [1] * 4 + [2] * 4 + [3] * 4
    ), f"expected four turns per run group in run-index order, got {indices}"

    assert t.turns[0].text == "conversation started"
    assert t.turns[4].text == "conversation started"
    assert t.turns[8].text == "conversation started"

    assert t.turns[0].run_id == "run_a"
    assert t.turns[4].run_id == "run_b"
    assert t.turns[8].run_id == "run_c"

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

    assert t.run_id == "run_a"


def test_single_run_events_emit_run_index_one_on_every_turn(tmp_path: Path) -> None:
    lines = _run_group("only_run", base_secs=0, prompt="hi", reply="hello")
    t = reconstruct_transcript(_write(tmp_path, lines))

    assert t.run_id == "only_run"
    assert all(turn.run_index == 1 for turn in t.turns), (
        f"single-run transcript must stamp run_index=1 on every turn, "
        f"got {[turn.run_index for turn in t.turns]}"
    )
    assert all(turn.run_id == "only_run" for turn in t.turns)
    assert t.turns[0].text == "conversation started"


def test_multi_run_delegation_does_not_leak_across_run_boundaries(
    tmp_path: Path,
) -> None:
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

    r1_coord = next(turn for turn in t.turns if turn.run_index == 1 and turn.agent == "coordinator")
    r2_coord = next(turn for turn in t.turns if turn.run_index == 2 and turn.agent == "coordinator")
    assert len(r1_coord.tool_results) == 1
    assert r1_coord.tool_results[0]["result"] == "done alpha"
    assert len(r2_coord.tool_results) == 1
    assert r2_coord.tool_results[0]["result"] == "done beta"


def test_tool_call_and_result_pairing(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "delegate"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
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

    coord = next(turn for turn in t.turns if turn.agent == "coordinator")
    assert len(coord.tool_calls) == 1
    call = coord.tool_calls[0]
    assert call["name"] == "worker"
    assert call["args"] == {"request": "do work"}

    assert len(coord.tool_results) == 1
    result = coord.tool_results[0]
    assert result["name"] == "worker"
    assert result["result"] == "work finished"


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


def test_camelcase_and_snakecase_keys_both_accepted(tmp_path: Path) -> None:
    lines = [
        _camel(
            "runStarted",
            {"goalSummary": "camel goal"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
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

    # A string on the line passes through as written; a proto timestamp
    # renders through the one reader, in the fixed-width fractional form the
    # run-log tail already served.
    assert t.turns[0].ts == "2026-05-16T04:38:56.123456Z"
    assert t.turns[1].ts == "2026-05-16T04:38:56.500000Z"


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
    truncated = '{"runCompleted": {"outcomeSumm'
    path = tmp_path / "events.jsonl"
    path.write_text(good + "\n" + good2 + "\n" + truncated, encoding="utf-8")

    t = reconstruct_transcript(path, partial_ok=True)
    assert t.event_count == 2
    assert any(turn.text == "thought" for turn in t.turns)
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


def test_annotation_before_any_turn_anchors_to_first_turn(tmp_path: Path) -> None:
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

    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["run_id"] == "r"
    assert decoded["complete"] is True
    assert decoded["event_count"] == 4
    assert isinstance(decoded["turns"], list)
    assert isinstance(decoded["annotations"], list)

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
        "source_index",
        "activity_ids",
    }
    for turn in decoded["turns"]:
        assert set(turn.keys()) == turn_keys
    ann_keys = {"kind", "ts", "summary", "anchor_seq", "detail", "source_index"}
    for ann in decoded["annotations"]:
        assert set(ann.keys()) == ann_keys

    assert Turn().to_dict()["tool_calls"] == []
    assert Turn().to_dict()["activity_ids"] == []
    assert Annotation().to_dict()["detail"] == {}


def test_execution_topology_uses_only_explicit_invocation_parents(tmp_path: Path) -> None:
    lines = [
        _camel(
            "agentInvocationStarted",
            {
                "agentName": "coordinator",
                "invocationId": "root",
                "taskId": "task-root",
            },
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "agentInvocationStarted",
            {
                "agentName": "researcher",
                "invocationId": "child",
                "parentInvocationId": "root",
                "taskId": "task-child",
            },
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "agentInvocationStarted",
            {
                "agentName": "detached",
                "invocationId": "orphan",
                "parentInvocationId": "absent",
            },
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        _camel(
            "agentInvocationCompleted",
            {
                "agentName": "researcher",
                "invocationId": "child",
                "summary": "evidence gathered",
                "taskId": "task-child",
            },
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
    ]

    transcript = reconstruct_transcript(_write(tmp_path, lines))
    execution = transcript.execution
    nodes = {node["node_id"]: node for node in execution["nodes"]}

    assert execution["fidelity"] == "partial"
    assert execution["root_ids"] == ["root"]
    assert execution["unresolved_ids"] == ["orphan"]
    assert nodes["child"] == {
        "node_id": "child",
        "kind": "agent",
        "parent_id": "root",
        "name": "researcher",
        "status": "completed",
        "start_source_index": 1,
        "summary": "evidence gathered",
        "fidelity": "exact",
    }
    assert nodes["orphan"]["parent_id"] == "absent"
    assert nodes["orphan"]["fidelity"] == "unresolved"
    assert transcript.turns[0].activity_ids == ["child"]


def test_observed_tool_is_turn_scoped_without_inferred_parent(tmp_path: Path) -> None:
    lines = [
        _camel(
            "delegationObserved",
            {"fromAgent": "lead", "toAgent": "worker", "taskId": "task-1"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        )
    ]

    transcript = reconstruct_transcript(_write(tmp_path, lines))
    node = transcript.execution["nodes"][0]

    assert transcript.execution["fidelity"] == "partial"
    assert transcript.execution["root_ids"] == ["tool:r:0"]
    assert transcript.turns[0].activity_ids == ["tool:r:0"]
    assert node["kind"] == "tool"
    assert node["parent_id"] is None
    assert node["status"] == "observed"
    assert node["fidelity"] == "turn"


def test_execution_cycle_is_unresolved_not_hidden(tmp_path: Path) -> None:
    lines = [
        _camel(
            "agentInvocationStarted",
            {"agentName": name, "invocationId": name, "parentInvocationId": parent},
            runId="r",
            sequence=str(index),
        )
        for index, (name, parent) in enumerate((("a", "b"), ("b", "a")))
    ]

    execution = reconstruct_transcript(_write(tmp_path, lines)).execution

    assert execution["root_ids"] == []
    assert execution["unresolved_ids"] == ["a", "b"]
    assert all(node["fidelity"] == "unresolved" for node in execution["nodes"])


def test_delegation_nests_under_its_stated_invocation(tmp_path: Path) -> None:
    lines = [
        _camel(
            "agentInvocationStarted",
            {"agentName": "coordinator", "invocationId": "root"},
            runId="r",
            sequence="0",
            emittedAt="2026-05-16T00:00:00Z",
        ),
        _camel(
            "delegationObserved",
            {"fromAgent": "coordinator", "toAgent": "worker", "invocationId": "root"},
            runId="r",
            sequence="1",
            emittedAt="2026-05-16T00:00:01Z",
        ),
        _camel(
            "agentInvocationStarted",
            {"agentName": "worker", "invocationId": "child", "parentInvocationId": "root"},
            runId="r",
            sequence="2",
            emittedAt="2026-05-16T00:00:02Z",
        ),
        # The boundary event attributes the child to the host agent (a real
        # producer trait): only its stated reason may be consumed, never its
        # agent_name.
        _camel(
            "invocationBoundaryExited",
            {"agentName": "coordinator", "invocationId": "child", "reason": "error:Boom"},
            runId="r",
            sequence="3",
            emittedAt="2026-05-16T00:00:03Z",
        ),
        _camel(
            "agentInvocationCompleted",
            {"agentName": "coordinator", "invocationId": "root", "summary": "wrapped up"},
            runId="r",
            sequence="4",
            emittedAt="2026-05-16T00:00:04Z",
        ),
    ]

    execution = reconstruct_transcript(_write(tmp_path, lines)).execution
    nodes = {node["node_id"]: node for node in execution["nodes"]}

    assert execution["fidelity"] == "exact"
    assert execution["root_ids"] == ["root"]
    assert (nodes["tool:r:1"]["parent_id"], nodes["tool:r:1"]["fidelity"]) == ("root", "exact")
    assert (nodes["child"]["name"], nodes["child"]["status"]) == ("worker", "failed")
    assert nodes["child"]["summary"] == "error:Boom"
    assert (nodes["root"]["status"], nodes["root"]["summary"]) == ("completed", "wrapped up")
    # Chronological within the tree: the delegation precedes the invocation
    # it observed.
    assert [node["node_id"] for node in execution["nodes"]] == ["root", "tool:r:1", "child"]


def test_invocation_cancelled_states_cancellation(tmp_path: Path) -> None:
    lines = [
        _camel(
            "agentInvocationStarted",
            {"agentName": "coordinator", "invocationId": "root"},
            runId="r",
            sequence="0",
        ),
        _camel(
            "invocationCancelled",
            {
                "invocationId": "root",
                "reason": "drift",
                "detail": "steering cancelled the dispatch",
            },
            runId="r",
            sequence="1",
        ),
    ]

    execution = reconstruct_transcript(_write(tmp_path, lines)).execution

    assert execution["nodes"][0]["status"] == "cancelled"
    assert execution["nodes"][0]["summary"] == "steering cancelled the dispatch"


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
    assert t.run_id
    assert t.event_count > 0
    assert t.turns
    seqs = [turn.seq for turn in t.turns if turn.seq is not None]
    assert seqs == sorted(seqs)
    turn_seqs = {turn.seq for turn in t.turns}
    for ann in t.annotations:
        assert ann.anchor_seq is None or ann.anchor_seq in turn_seqs
    json.dumps(t.to_dict())
