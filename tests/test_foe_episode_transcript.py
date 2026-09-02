"""Reading a Foe episode log: the derived-message rule, and the transcript.

The three ``.jsonl`` files under ``tests/fixtures/foe_episodes/`` are copied
verbatim from ``view/fixtures/`` in the Foe repository at commit
``63ba2c61``, where they are the fixtures Foe's own viewer is tested against.
Sharing them is what makes the agreement test below meaningful: both readers
answer for the same bytes. Every ordinary request in those bytes records the
message list it sent, so a recomputed list that differs from the recorded one
is a defect in one of the two readers.

Refreshing them means copying the files again from a named Foe commit and
recording that commit here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.query.foe_episode import (
    EpisodeEvent,
    derive_messages,
    is_episode_log,
    is_summary_request,
    read_episode_log,
)
from zicato.query.transcript_reconstruction import reconstruct_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "foe_episodes"

#: The episode each fixture holds, named by what it exercises.
MULTI_TOOL = FIXTURES / "rich.jsonl"
SEEDED = FIXTURES / "fork.jsonl"
COMPACTED = FIXTURES / "compact.jsonl"


def _request_messages(
    events: tuple[EpisodeEvent, ...],
) -> list[tuple[int, list, list[dict]]]:
    """Every ordinary request paired with its recorded and its recomputed list."""
    return [
        (event.seq, event.data.get("messages"), derive_messages(events, event.seq))
        for event in events
        if event.type == "model/request" and not is_summary_request(event.data.get("request_id"))
    ]


def _write(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _episode(*events: dict) -> list[dict]:
    """One well-formed log: the start event, the given events, then the end."""
    body = [
        {"seq": index + 1, "time": 1_724_200_000_000 + index, **event}
        for index, event in enumerate(events)
    ]
    return [
        {
            "seq": 0,
            "time": 1_724_200_000_000,
            "version": 3,
            "type": "episode/start",
            "data": {"id": "ep_test", "contract": {"name": "proposer"}, "fork_origin": None},
        },
        *body,
        {
            "seq": len(body) + 1,
            "time": 1_724_200_001_000,
            "type": "episode/end",
            "data": {"outcome": {"kind": "completed", "value": "done"}},
        },
    ]


# ---------------------------------------------------------------------------
# Which reader answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.jsonl")), ids=lambda p: p.name)
def test_every_shared_fixture_is_recognised_as_an_episode_log(fixture: Path) -> None:
    assert is_episode_log(fixture) is True


def test_an_adk_event_stream_is_not_an_episode_log() -> None:
    adk = Path(__file__).parent / "fixtures" / "adk_transcripts" / "nested_agents.jsonl"
    assert is_episode_log(adk) is False


def test_a_log_whose_first_event_is_not_the_episode_start_is_not_one(tmp_path: Path) -> None:
    """The signature is the whole test, so a log missing it takes the other reader."""
    path = _write(
        tmp_path / "episode.jsonl",
        [{"seq": 1, "type": "episode/start", "data": {"id": "ep_late"}}],
    )
    assert is_episode_log(path) is False


def test_a_missing_file_is_not_an_episode_log(tmp_path: Path) -> None:
    assert is_episode_log(tmp_path / "absent.jsonl") is False


# ---------------------------------------------------------------------------
# The derived-message rule, checked against the log's own record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.jsonl")), ids=lambda p: p.name)
def test_recomputed_messages_equal_the_messages_the_request_recorded(fixture: Path) -> None:
    events = read_episode_log(fixture).events
    requests = _request_messages(events)
    assert requests, f"{fixture.name} records no ordinary request to check"
    for seq, recorded, derived in requests:
        assert derived == recorded, f"{fixture.name} request at seq {seq}"


def test_the_shared_fixtures_cover_the_rule_s_three_hard_cases() -> None:
    """Guard the corpus: tool results, a copied prefix, and a compaction.

    Each is a clause of the rule that a simpler log would not reach — the
    tool message, the seeded prefix's renumbered events, and the summary
    boundary that replaces the head of the list.
    """
    kinds = {
        fixture.name: {event.type for event in read_episode_log(fixture).events}
        for fixture in FIXTURES.glob("*.jsonl")
    }
    assert "tool/result" in kinds["rich.jsonl"]
    assert "seed/end" in kinds["fork.jsonl"]
    assert "compaction/summary" in kinds["compact.jsonl"]


def test_a_summarization_exchange_contributes_no_message() -> None:
    """A ``cmp_`` request and the response answering it are left out."""
    events = read_episode_log(COMPACTED).events
    assert [seq for seq, _recorded, _derived in _request_messages(events)] == [3, 6, 9, 20]


def test_the_post_compaction_list_opens_with_the_task_and_the_continuation() -> None:
    events = read_episode_log(COMPACTED).events
    _seq, _recorded, derived = _request_messages(events)[-1]
    assert derived[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "Rename the helper and update its callers."}],
    }
    assert derived[1]["role"] == "user"
    assert derived[1]["content"][0]["text"].startswith("## Continuation state\n\n")


# ---------------------------------------------------------------------------
# The transcript a Foe log reconstructs to
# ---------------------------------------------------------------------------


def test_a_multi_tool_episode_reconstructs_at_exact_fidelity() -> None:
    """One response issuing two calls, each answered by its own result.

    The fidelity is a property of the format: every call carries exactly one
    ``call_id``-matched result and every request records its messages, so no
    node is inferred and none is left unresolved.
    """
    payload = reconstruct_transcript(MULTI_TOOL).to_dict()
    execution = payload["execution"]
    assert execution["fidelity"] == "exact"
    assert execution["unresolved_ids"] == []
    assert {node["fidelity"] for node in execution["nodes"]} == {"exact"}
    assert execution["root_ids"] == ["ep_rich"]

    responder = next(turn for turn in payload["turns"] if turn["tool_calls"])
    assert [call["name"] for call in responder["tool_calls"]] == ["edit", "read"]
    assert [result["call_id"] for result in responder["tool_results"]] == ["tc_r1", "tc_r2"]
    assert responder["activity_ids"] == ["tool:ep_rich:tc_r1", "tool:ep_rich:tc_r2"]
    assert payload["run_id"] == "ep_rich"
    assert payload["complete"] is True


def test_each_tool_node_hangs_off_the_episode_and_reports_its_own_outcome() -> None:
    nodes = {n["node_id"]: n for n in reconstruct_transcript(MULTI_TOOL).execution["nodes"]}
    assert nodes["tool:ep_rich:tc_r1"]["parent_id"] == "ep_rich"
    assert nodes["tool:ep_rich:tc_r1"]["status"] == "completed"
    assert nodes["ep_rich"]["kind"] == "agent"
    assert nodes["ep_rich"]["name"] == "writer"


def test_the_seeded_prefix_is_attributed_to_the_episode_it_was_copied_from() -> None:
    """A fork's copied events are the source episode's work, and say so."""
    payload = reconstruct_transcript(SEEDED).to_dict()
    groups = [(turn["run_index"], turn["run_id"]) for turn in payload["turns"]]
    assert groups == [
        (1, "ep_root"),
        (1, "ep_root"),
        (2, "ep_fork"),
        (2, "ep_fork"),
        (2, "ep_fork"),
    ]

    boundary = next(note for note in payload["annotations"] if note["kind"] == "seed")
    assert boundary["summary"] == (
        "end of the prefix copied from ep_root up to its seq 12; " "the episode's own events follow"
    )
    assert boundary["anchor_seq"] == 9
    assert boundary["detail"]["fork_origin"] == {"episode_id": "ep_root", "seq": 12}


def test_an_unseeded_episode_carries_one_run_group_and_no_boundary() -> None:
    payload = reconstruct_transcript(MULTI_TOOL).to_dict()
    assert {turn["run_index"] for turn in payload["turns"]} == {1}
    assert payload["annotations"] == []


def test_the_episode_outcome_closes_the_transcript() -> None:
    payload = reconstruct_transcript(SEEDED).to_dict()
    last = payload["turns"][-1]
    assert last["role"] == "system"
    assert last["kind"] == "episode/end"
    assert last["text"] == "blocked: The edit tool is not available."


def test_an_inner_dispatch_contributes_no_turn_and_nests_under_its_outer_call(
    tmp_path: Path,
) -> None:
    """Only the composing call's own result reaches the model, so only it shows."""
    path = _write(
        tmp_path / "episode.jsonl",
        _episode(
            {
                "type": "inbox/item",
                "data": {
                    "source": "task",
                    "content": [{"type": "text", "text": "Count the callers."}],
                },
            },
            {
                "type": "model/request",
                "data": {"request_id": "rq_1", "consumed": [1], "messages": []},
            },
            {
                "type": "assistant/message",
                "data": {
                    "request_id": "rq_1",
                    "text": "Composing a search.",
                    "tool_calls": [{"id": "tc_outer", "name": "python", "args": {}}],
                    "interrupted": False,
                },
            },
            {
                "type": "tool/inner-call",
                "data": {
                    "outer_call_id": "tc_outer",
                    "call_id": "tc_inner",
                    "index": 0,
                    "name": "grep",
                },
            },
            {
                "type": "tool/result",
                "data": {
                    "call_id": "tc_inner",
                    "name": "grep",
                    "rendered": "two matches",
                    "is_error": False,
                },
            },
            {
                "type": "tool/result",
                "data": {
                    "call_id": "tc_outer",
                    "name": "python",
                    "rendered": "2",
                    "is_error": False,
                },
            },
        ),
    )
    payload = reconstruct_transcript(path).to_dict()
    responder = next(turn for turn in payload["turns"] if turn["tool_calls"])
    assert [result["call_id"] for result in responder["tool_results"]] == ["tc_outer"]

    nodes = {node["node_id"]: node for node in payload["execution"]["nodes"]}
    assert nodes["tool:ep_test:tc_inner"]["parent_id"] == "tool:ep_test:tc_outer"
    assert payload["execution"]["fidelity"] == "exact"


def test_a_failed_call_marks_its_node_failed(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "episode.jsonl",
        _episode(
            {
                "type": "assistant/message",
                "data": {
                    "request_id": "rq_1",
                    "text": "Reading it.",
                    "tool_calls": [{"id": "tc_1", "name": "read", "args": {"path": "/private"}}],
                    "interrupted": False,
                },
            },
            {
                "type": "tool/result",
                "data": {
                    "call_id": "tc_1",
                    "name": "read",
                    "rendered": "read: /private is outside this tool's permissions",
                    "is_error": True,
                    "subject": "read /private denied",
                },
            },
        ),
    )
    nodes = {n["node_id"]: n for n in reconstruct_transcript(path).execution["nodes"]}
    assert nodes["tool:ep_test:tc_1"]["status"] == "failed"
    assert nodes["tool:ep_test:tc_1"]["summary"] == "read /private denied"


def test_a_log_with_no_end_event_is_incomplete_and_still_readable(tmp_path: Path) -> None:
    """A running episode is read as far as it has been written."""
    events = _episode(
        {
            "type": "assistant/message",
            "data": {
                "request_id": "rq_1",
                "text": "Working.",
                "tool_calls": [],
                "interrupted": False,
            },
        }
    )
    path = _write(tmp_path / "episode.jsonl", events[:-1])
    payload = reconstruct_transcript(path).to_dict()
    assert payload["complete"] is False
    assert [turn["text"] for turn in payload["turns"]] == ["Working."]
    assert payload["execution"]["nodes"][0]["status"] == "running"


def test_a_torn_final_line_is_skipped_and_reported_as_incomplete(tmp_path: Path) -> None:
    path = _write(tmp_path / "episode.jsonl", _episode())
    path.write_text(
        path.read_text(encoding="utf-8") + '{"seq": 9, "type": "assis', encoding="utf-8"
    )
    payload = reconstruct_transcript(path).to_dict()
    assert payload["complete"] is False
    assert payload["event_count"] == 2
