"""What reading the event log through one reader changed, one test per change.

Before this, each consumer resolved an event line independently. Each test
under a "Change" heading names a line shape they answered differently, the
consumer whose output moved, and what it now says; every one of those
fails against the readers this replaces. The two at the end pin answers
that are the same before and after, because the module they belong to kept
its own envelope key set and now shares the reader's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.analyzer.aggregator import aggregate_decision_events
from zicato.analyzer.process_exemplars import _load_events
from zicato.core import DIALECT_ADK_EVENTS, DIALECT_GOLDFIVE, BoardEntry
from zicato.query.run_log import _tail_events
from zicato.query.transcript_reconstruction import reconstruct_transcript
from zicato.reflection.trace_import import sniff_dialect
from zicato.telemetry.dialects import reduce_adk_events
from zicato.telemetry.event_log import read_event_log
from zicato.telemetry.reducer import _agent_and_user_turns_from_events
from zicato.telemetry.reducer import _load_events as _reducer_load
from zicato.telemetry.terminal_event import _file_already_has_terminal


def _write(path: Path, lines: list[dict | str]) -> Path:
    path.write_text(
        "".join((json.dumps(x) if isinstance(x, dict) else x) + "\n" for x in lines),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Change 1 — the normalized {kind, payload} shape resolves its real case
# ---------------------------------------------------------------------------

_NORMALIZED = {
    "event_id": "x:1:y",
    "run_id": "x",
    "sequence": 1,
    "kind": "drift_detected",
    "payload": {"kind": "off_topic", "severity": "warning", "current_agent_id": "planner"},
}


def test_the_loss_reducer_reads_a_normalized_line_as_its_own_case(tmp_path: Path) -> None:
    """It reported the case as ``payload`` — the envelope's own field name.

    Nothing dispatches on ``payload``, so the drift on this line was
    counted nowhere.
    """
    path = _write(tmp_path / "events.jsonl", [_NORMALIZED])
    assert [r.case for r in _reducer_load(path)] == ["drift_detected"]


def test_the_exemplar_extractor_reads_a_normalized_line_as_its_own_case(tmp_path: Path) -> None:
    path = _write(tmp_path / "events.jsonl", [_NORMALIZED])
    assert [r.case for r in _load_events(path)] == ["drift_detected"]


def test_a_normalized_decision_event_is_counted_by_the_aggregator(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "events.jsonl",
        [
            {
                "run_id": "x",
                "sequence": 1,
                "kind": "steeringDecisionMade",
                "payload": {"detectorName": "plan_thrash", "outcome": "steered"},
            }
        ],
    )
    summary = aggregate_decision_events([path])
    assert summary.total_events_seen == 1
    assert summary.steering_decisions == {"plan_thrash": {"steered": 1}}


# ---------------------------------------------------------------------------
# Change 2 — payload field names are converted too, at every depth
# ---------------------------------------------------------------------------


def test_the_reducer_reads_a_camel_case_payload_s_multi_word_fields(tmp_path: Path) -> None:
    """The transcript proxies were lost on the camelCase wire form.

    The reducer converted the payload's CASE name but not its field
    names, then looked up ``goal_summary`` and ``outcome_summary`` — which
    a ``MessageToJson`` line spells ``goalSummary`` and ``outcomeSummary``.
    Both turns came back empty, and the reducer reaches this reading
    whenever goldfive's strict parser refuses a file, not only where
    goldfive is absent.
    """
    path = _write(
        tmp_path / "events.jsonl",
        [
            {"runId": "x", "sequence": 1, "runStarted": {"goalSummary": "summarise the paper"}},
            {"runId": "x", "sequence": 2, "runCompleted": {"outcomeSummary": "here is a summary"}},
        ],
    )
    agent_turns, user_turns = _agent_and_user_turns_from_events(read_event_log(path).records)
    assert user_turns == ["summarise the paper"]
    assert agent_turns == ["here is a summary"]


def test_a_nested_payload_message_is_converted_too(tmp_path: Path) -> None:
    """Only the transcript reconstruction recursed; the others stopped at
    the top level, so a sub-message kept whichever spelling was written."""
    path = _write(
        tmp_path / "events.jsonl",
        [{"runId": "x", "planRevised": {"revisionIndex": 1, "plan": {"taskCount": 3}}}],
    )
    (record,) = read_event_log(path).records
    assert record.payload == {"revision_index": 1, "plan": {"task_count": 3}}


# ---------------------------------------------------------------------------
# Change 3 — a run of capitals is one word
# ---------------------------------------------------------------------------


def test_an_acronym_case_name_is_not_split_letter_by_letter(tmp_path: Path) -> None:
    """Four readers produced ``goldfive_l_l_m_call_start``, which no
    dispatch table holds; the run-log tail and the transcript produced the
    folded form. All of them now produce the folded form."""
    path = _write(
        tmp_path / "events.jsonl",
        [{"runId": "x", "sequence": 1, "goldfiveLLMCallStart": {"name": "judge"}}],
    )
    assert [r.case for r in read_event_log(path).records] == ["goldfive_llmcall_start"]


# ---------------------------------------------------------------------------
# Change 4 — one timestamp rendering
# ---------------------------------------------------------------------------


def test_the_transcript_and_the_run_log_render_one_instant_alike(tmp_path: Path) -> None:
    """They rendered the fraction differently: the transcript trimmed
    trailing zeros to ``.5``, the run-log tail padded to ``.500000``."""
    line = {
        "run_id": "x",
        "sequence": 1,
        "kind": "task_completed",
        "payload": {"task_id": "t1", "summary": "done"},
        "emitted_at": {"seconds": 1778906336, "nanos": 500000000},
    }
    path = _write(tmp_path / "events.jsonl", [line])
    transcript = reconstruct_transcript(path)
    (tail_record,) = _tail_events(path, 10)
    assert transcript.turns[0].ts == tail_record["ts"] == "2026-05-16T04:38:56.500000Z"


def test_an_instant_inside_the_first_second_still_renders(tmp_path: Path) -> None:
    """A proto message omits a zero-valued field, so an instant in the
    first second of the epoch arrives with ``nanos`` alone. The run-log
    tail dropped its timestamp; zero seconds is a real instant."""
    path = _write(
        tmp_path / "events.jsonl",
        [{"run_id": "x", "sequence": 1, "runStarted": {}, "emitted_at": {"nanos": 250000000}}],
    )
    (record,) = _tail_events(path, 10)
    assert record["ts"] == "1970-01-01T00:00:00.250000Z"


# ---------------------------------------------------------------------------
# Change 5 — a non-envelope field holding a scalar is not a payload
# ---------------------------------------------------------------------------


def test_a_unit_cache_marker_is_not_reported_as_an_event_kind(tmp_path: Path) -> None:
    """The unit cache writes ``{"seq": 1, "round": "first"}`` beside a run's
    telemetry under the same extension. The run-log tail reported ``round``
    as an event kind and the transcript minted a turn from it."""
    path = _write(tmp_path / "events.jsonl", [{"seq": 1, "round": "first"}])
    assert [r["kind"] for r in _tail_events(path, 10)] == ["unknown"]
    assert reconstruct_transcript(path).turns == []


def test_a_bare_transcript_log_is_not_reported_as_event_kinds(tmp_path: Path) -> None:
    """A transcript-dialect log is a list of ``{role, content}`` lines named
    ``events.jsonl``. Both readers reported ``content`` as an event kind."""
    path = _write(
        tmp_path / "events.jsonl",
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
    )
    assert [r["kind"] for r in _tail_events(path, 10)] == ["unknown", "unknown"]


# ---------------------------------------------------------------------------
# Change 6 — an invalid byte degrades instead of escaping the reader
# ---------------------------------------------------------------------------


def test_an_invalid_byte_does_not_escape_the_run_log_or_the_transcript(tmp_path: Path) -> None:
    """Both read with strict UTF-8, and a ``UnicodeDecodeError`` is neither
    an ``OSError`` nor a ``JSONDecodeError``, so it left the reader and
    reached the endpoint serving the panel."""
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"runId": "x", "sequence": 1, "runStarted": {"goalSummary": "\xff"}}\n'
        b'{"runId": "x", "sequence": 2, "runCompleted": {}}\n'
    )
    assert [r["kind"] for r in _tail_events(path, 10)] == ["run_started", "run_completed"]
    assert reconstruct_transcript(path).complete is True


# ---------------------------------------------------------------------------
# Change 7 — a foreign event log's field names are converted as well
# ---------------------------------------------------------------------------


def test_a_foreign_event_log_s_field_names_reduce_in_either_spelling(
    tmp_path: Path,
) -> None:
    """The ``adk_events`` dialect tolerated alias NAMES by listing them
    (``event_type`` beside ``type``) but not their capitalisation, so a
    foreign log that wrote ``eventType`` or ``isError`` reduced to nothing.

    Field names are converted; field VALUES are not. A producer that spells
    the event type itself ``modelUsage`` is still a producer whose
    vocabulary this dialect does not know.
    """
    entry = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=1, input="")
    lines = [
        ({"eventType": "tool_call", "tool": "search", "args": {"q": "a"}},),
        ({"eventType": "tool_response", "isError": True},),
        ({"eventType": "model_usage", "usage": {"inputTokens": 10, "outputTokens": 5}},),
    ]
    camel = _write(tmp_path / "camel.jsonl", [line for (line,) in lines])
    snake = _write(
        tmp_path / "snake.jsonl",
        [
            {"event_type": "tool_call", "tool": "search", "args": {"q": "a"}},
            {"event_type": "tool_response", "is_error": True},
            {"event_type": "model_usage", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ],
    )
    assert reduce_adk_events(camel, entry) == reduce_adk_events(snake, entry)
    signals = reduce_adk_events(camel, entry)
    assert (signals.task_started, signals.task_failed, signals.token_count) == (1, 1, 15)


# ---------------------------------------------------------------------------
# Unchanged where it matters: the terminal-frame check still reads both
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        {"runId": "x", "sequence": 9, "runAborted": {"reason": "wall_clock_budget_exceeded"}},
        {"run_id": "x", "sequence": 9, "run_aborted": {"reason": "wall_clock_budget_exceeded"}},
        {"run_id": "x", "sequence": 9, "kind": "conversationEnded", "payload": {}},
    ],
)
def test_a_terminal_frame_is_recognised_in_every_shape(tmp_path: Path, line: dict) -> None:
    """The hand-written check listed six spellings and missed the
    normalized shape entirely."""
    assert _file_already_has_terminal(_write(tmp_path / "events.jsonl", [line])) is True


def test_a_file_with_no_terminal_frame_is_still_open(tmp_path: Path) -> None:
    path = _write(tmp_path / "events.jsonl", [{"runId": "x", "sequence": 1, "runStarted": {}}])
    assert _file_already_has_terminal(path) is False


# ---------------------------------------------------------------------------
# Unchanged: the foreign-trace sniffer answers as it did
# ---------------------------------------------------------------------------
#
# The sniffer kept the last of the separate envelope key sets. Reading it
# through the one reader narrows that set to the three names that actually
# MARK a producer, and both verdicts below are the ones it already gave.


def test_a_normalized_trace_sniffs_as_goldfive(tmp_path: Path) -> None:
    """A payload under ``payload`` still identifies a goldfive event.

    The reader resolves the case from ``kind``; the sniffer asks only
    whether a case resolved at all, so a normalized line reads as goldfive
    the way a payload-key line does.
    """
    path = _write(
        tmp_path / "trace.jsonl",
        [
            {
                "event_id": "x:1:y",
                "run_id": "x",
                "sequence": 1,
                "emitted_at": {"seconds": 1778906222, "nanos": 0},
                "kind": "drift_detected",
                "payload": {"kind": "off_topic", "severity": "warning"},
            }
        ],
    )
    assert sniff_dialect(path) == DIALECT_GOLDFIVE


def test_a_run_or_session_id_alone_does_not_make_a_trace_goldfive(tmp_path: Path) -> None:
    """An ADK-style log routinely carries both, so neither marks a producer.

    This is why the sniffer's marker set stays narrower than the reader's
    envelope schema rather than folding into it.
    """
    path = _write(
        tmp_path / "trace.jsonl",
        [{"run_id": "x", "session_id": "s", "type": "tool_call", "tool": "search"}],
    )
    assert sniff_dialect(path) == DIALECT_ADK_EVENTS
