"""The one event-log reader: one envelope schema, one casing rule.

Each test names a shape that reaches an ``events.jsonl`` on disk and states
what the reader must make of it.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.telemetry.event_log import (
    ENVELOPE_KEYS,
    EventRecord,
    parse_event,
    read_event_log,
    snake_deep,
    to_snake,
)


def _write(path: Path, lines: list[dict | str]) -> Path:
    path.write_text(
        "".join((json.dumps(x) if isinstance(x, dict) else x) + "\n" for x in lines),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The casing rule
# ---------------------------------------------------------------------------


def test_camel_case_names_convert_and_snake_case_names_pass_through() -> None:
    assert to_snake("driftDetected") == "drift_detected"
    assert to_snake("LadderTransitionDecided") == "ladder_transition_decided"
    assert to_snake("goldfiveLlmCallStart") == "goldfive_llm_call_start"
    assert to_snake("already_snake_case") == "already_snake_case"
    assert to_snake("target4Score") == "target4_score"


def test_conversion_is_idempotent() -> None:
    for name in ("driftDetected", "runId", "goldfiveLlmCallEnd", "target4Score"):
        once = to_snake(name)
        assert to_snake(once) == once


def test_a_run_of_capitals_is_one_word() -> None:
    """A hand-written log may spell an acronym in full capitals.

    Splitting inside the run would produce ``goldfive_l_l_m_call_start``,
    which no dispatch table holds.
    """
    assert to_snake("goldfiveLLMCallStart") == "goldfive_llmcall_start"
    assert to_snake("HTTPServer") == "httpserver"


def test_nested_keys_convert_at_every_depth() -> None:
    value = {"planRevised": {"revisionIndex": 1, "plan": {"taskCount": [{"taskId": "t"}]}}}
    assert snake_deep(value) == {
        "plan_revised": {"revision_index": 1, "plan": {"task_count": [{"task_id": "t"}]}}
    }


# ---------------------------------------------------------------------------
# The envelope schema
# ---------------------------------------------------------------------------


def test_the_payload_key_shape_resolves_its_oneof_case() -> None:
    record = parse_event(
        {
            "eventId": "x:1:y",
            "runId": "x",
            "sessionId": "s",
            "sequence": 3,
            "emittedAt": "2026-05-16T04:36:54Z",
            "driftDetected": {"kind": "off_topic", "currentAgentId": "planner"},
        }
    )
    assert record.case == "drift_detected"
    assert record.payload == {"kind": "off_topic", "current_agent_id": "planner"}
    assert (record.run_id, record.session_id, record.event_id) == ("x", "s", "x:1:y")
    assert record.sequence == 3
    assert record.emitted_at == "2026-05-16T04:36:54Z"


def test_the_normalized_shape_resolves_the_same_case() -> None:
    """``kind`` plus ``payload`` must select the case ``kind`` names.

    Reading the first message-valued field instead would select ``payload``
    itself, which is the envelope's own field and never an event kind.
    """
    record = parse_event(
        {
            "event_id": "x:12:y",
            "run_id": "x",
            "kind": "pin_resolved",
            "payload": {"agentName": "research_agent"},
            "emitted_at": {"seconds": 1778906222, "nanos": 0},
        }
    )
    assert record.case == "pin_resolved"
    assert record.payload == {"agent_name": "research_agent"}


def test_both_shapes_of_one_event_resolve_alike() -> None:
    payload_key_shape = parse_event(
        {"runId": "x", "sequence": 1, "taskProgress": {"taskId": "t", "fraction": 0.5}}
    )
    normalized_shape = parse_event(
        {
            "run_id": "x",
            "sequence": 1,
            "kind": "taskProgress",
            "payload": {"task_id": "t", "fraction": 0.5},
        }
    )
    assert payload_key_shape.case == normalized_shape.case == "task_progress"
    assert payload_key_shape.payload == normalized_shape.payload


def test_an_envelope_carrying_no_payload_names_no_case() -> None:
    record = parse_event({"eventId": "x:1:y", "runId": "x", "sessionId": "s"})
    assert record == EventRecord(
        case="",
        payload={},
        run_id="x",
        session_id="s",
        event_id="x:1:y",
        raw={"event_id": "x:1:y", "run_id": "x", "session_id": "s"},
    )


def test_a_scalar_beside_the_envelope_is_not_a_payload() -> None:
    """The payload ``oneof`` holds a message, so a scalar cannot be one.

    Run directories carry files sharing the events extension that hold
    other record shapes; naming their first field as an event kind invents
    telemetry that was never emitted.
    """
    assert parse_event({"seq": 1, "round": "first"}).case == ""
    assert parse_event({"role": "user", "content": "hello"}).case == ""


def test_every_envelope_field_is_skipped_when_the_payload_key_is_sought() -> None:
    """A field named here can never be mistaken for the payload case."""
    assert ENVELOPE_KEYS == {
        "emitted_at",
        "event_id",
        "kind",
        "payload",
        "run_id",
        "seq",
        "sequence",
        "session_id",
    }
    envelope_only = dict.fromkeys(ENVELOPE_KEYS, {"a message": 1})
    assert parse_event(envelope_only).case == ""


# ---------------------------------------------------------------------------
# Envelope fields
# ---------------------------------------------------------------------------


def test_the_sequence_number_reads_from_either_spelling() -> None:
    assert parse_event({"sequence": 7}).sequence == 7
    assert parse_event({"seq": 7}).sequence == 7
    # Protobuf renders a 64-bit integer as a JSON string.
    assert parse_event({"sequence": "7"}).sequence == 7
    assert parse_event({"sequence": "not a number"}).sequence is None
    assert parse_event({}).sequence is None


def test_a_proto_timestamp_renders_as_an_utc_string() -> None:
    assert (
        parse_event({"emitted_at": {"seconds": 1778906222, "nanos": 939036939}}).emitted_at
        == "2026-05-16T04:37:02.939037Z"
    )
    # A zero-valued field is omitted from the message, and zero seconds is a
    # real instant rather than a missing one.
    assert parse_event({"emitted_at": {"nanos": 250000000}}).emitted_at == (
        "1970-01-01T00:00:00.250000Z"
    )
    assert parse_event({"emittedAt": "2026-05-16T04:36:54Z"}).emitted_at == "2026-05-16T04:36:54Z"
    assert parse_event({"emitted_at": {"seconds": "junk"}}).emitted_at is None
    assert parse_event({}).emitted_at is None


def test_identity_fields_holding_a_non_string_read_as_absent() -> None:
    record = parse_event({"run_id": 7, "session_id": None, "event_id": ["x"]})
    assert (record.run_id, record.session_id, record.event_id) == ("", "", "")


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------


def test_a_missing_file_reads_as_an_empty_log(tmp_path: Path) -> None:
    log = read_event_log(tmp_path / "absent.jsonl")
    assert log.records == ()
    assert log.malformed_line_count == 0
    assert log.last_line_ok is True


def test_a_bad_line_is_counted_and_the_rest_of_the_file_still_reads(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "events.jsonl",
        [
            {"runId": "x", "sequence": 1, "runStarted": {"goalSummary": "g"}},
            "{ not json",
            "[1, 2, 3]",
            "",
            {"runId": "x", "sequence": 2, "runCompleted": {"outcomeSummary": "done"}},
        ],
    )
    log = read_event_log(path)
    assert [r.case for r in log.records] == ["run_started", "run_completed"]
    assert log.malformed_line_count == 2
    assert log.last_line_ok is True


def test_a_truncated_final_line_is_reported(tmp_path: Path) -> None:
    """A log still being appended to ends mid-line; a live reader says so."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"runId": "x", "sequence": 1, "runStarted": {}}\n{"runId": "x", "seq', "utf-8")
    log = read_event_log(path)
    assert [r.case for r in log.records] == ["run_started"]
    assert log.last_line_ok is False


def test_an_invalid_byte_never_raises_through_the_reader(tmp_path: Path) -> None:
    """A foreign or truncated log may carry bytes that are not UTF-8.

    Decoding substitutes the replacement character. Inside a quoted string
    the line still parses and is kept, with the substitution visible in the
    value; where the byte breaks the JSON structure the line is counted
    malformed. Either way the read returns, so one bad byte cannot take out
    the endpoint reading the file.
    """
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"runId": "x", "sequence": 1, "runStarted": {"goalSummary": "\xff\xfe"}}\n'
        b'{"runId": "x", "sequence": 2, \xff "runAborted": {}}\n'
        b'{"runId": "x", "sequence": 3, "runCompleted": {}}\n'
    )
    log = read_event_log(path)
    assert [r.case for r in log.records] == ["run_started", "run_completed"]
    assert log.records[0].payload == {"goal_summary": "��"}
    assert log.malformed_line_count == 1


def test_a_directory_in_place_of_the_file_reads_as_an_empty_log(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").mkdir()
    assert read_event_log(tmp_path / "events.jsonl").records == ()
