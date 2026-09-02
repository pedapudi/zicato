"""A generation's proposal transcript comes from its Foe episode, and nowhere else.

Foe is the only runtime that proposes a candidate, so the episode log it
writes under the epoch's ``episodes/`` is the whole record of how a
generation came to exist. A generation also has one board run per entry it
was evaluated on, each with a Goldfive/ADK ``events.jsonl``. The board entry
is what tells the two apart: a conversation asked for by generation alone is
the proposal, and it resolves to the episode log or to nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query.events_index import find_proposal_episode_log
from zicato.query.paths import WorkspacePaths
from zicato.query.transcript_reconstruction import reconstruct_transcript
from zicato.query.transcript_view import resolve_conversation
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-09-01-alpha"
GENERATION = "v3"

_EPISODE = [
    {
        "seq": 0,
        "time": 1_724_200_000_000,
        "version": 3,
        "type": "episode/start",
        "data": {"id": "ep_propose_v3", "contract": {"name": "proposer"}, "fork_origin": None},
    },
    {
        "seq": 1,
        "time": 1_724_200_000_100,
        "type": "inbox/item",
        "data": {
            "source": "task",
            "content": [{"type": "text", "text": "Widen the retry window."}],
        },
    },
    {
        "seq": 2,
        "time": 1_724_200_000_200,
        "type": "model/request",
        "data": {"request_id": "rq_1", "consumed": [1], "messages": []},
    },
    {
        "seq": 3,
        "time": 1_724_200_000_300,
        "type": "assistant/message",
        "data": {
            "request_id": "rq_1",
            "text": "Editing the retry constant.",
            "tool_calls": [{"id": "tc_1", "name": "edit", "args": {"path": "src/retry.py"}}],
            "interrupted": False,
        },
    },
    {
        "seq": 4,
        "time": 1_724_200_000_400,
        "type": "tool/result",
        "data": {"call_id": "tc_1", "name": "edit", "rendered": "+1 -1", "is_error": False},
    },
    {
        "seq": 5,
        "time": 1_724_200_000_500,
        "type": "episode/end",
        "data": {"outcome": {"kind": "completed", "value": "one patch"}},
    },
]

#: A board run for the same generation, in the format the proposal must never
#: be served from.
_BOARD_RUN = [
    {"runId": "run-v3-entry1", "sequence": 1, "runStarted": {"goalSummary": "Answer entry one."}},
    {"runId": "run-v3-entry1", "sequence": 2, "taskCompleted": {"summary": "Entry one answered."}},
    {"runId": "run-v3-entry1", "sequence": 3, "runCompleted": {"outcomeSummary": "Completed."}},
]


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A ``.zicato/`` holding one generation's proposal episode and one board run."""
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    _write_jsonl(layout.proposal_episode_dir(EPOCH, GENERATION) / "episode.jsonl", _EPISODE)
    _write_jsonl(layout.events(EPOCH, GENERATION, "entry1"), _BOARD_RUN)
    return root


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace)


def test_a_generation_without_an_entry_resolves_to_its_episode(workspace: Path) -> None:
    resolved = resolve_conversation(_paths(workspace), GENERATION, gen=GENERATION, epoch=EPOCH)
    assert (
        resolved
        == WorkspaceLayout.from_root(workspace).proposal_episode_dir(EPOCH, GENERATION)
        / "episode.jsonl"
    )


def test_a_generation_with_an_entry_still_resolves_to_that_entry_s_board_run(
    workspace: Path,
) -> None:
    """The board-run path is untouched: the entry coordinate selects it."""
    resolved = resolve_conversation(
        _paths(workspace), "run-v3-entry1", gen=GENERATION, entry="entry1", epoch=EPOCH
    )
    assert resolved == WorkspaceLayout.from_root(workspace).events(EPOCH, GENERATION, "entry1")


def test_the_proposal_transcript_is_read_by_the_foe_reader(workspace: Path) -> None:
    """The served payload is the episode's, at the fidelity the format gives."""
    resolved = resolve_conversation(_paths(workspace), GENERATION, gen=GENERATION, epoch=EPOCH)
    assert resolved is not None
    payload = reconstruct_transcript(resolved).to_dict()
    assert payload["run_id"] == "ep_propose_v3"
    assert payload["execution"]["fidelity"] == "exact"
    assert [turn["kind"] for turn in payload["turns"]] == [
        "model/request",
        "assistant/message",
        "episode/end",
    ]


def test_no_proposal_transcript_is_served_from_an_adk_source(workspace: Path) -> None:
    """An episode directory holding an ADK stream answers nothing.

    Serving it would claim the exact fidelity that only the episode format
    supplies, and the generation's board runs sitting beside it must not
    stand in for the proposal either.
    """
    layout = WorkspaceLayout.from_root(workspace)
    episode = layout.proposal_episode_dir(EPOCH, GENERATION) / "episode.jsonl"
    _write_jsonl(episode, _BOARD_RUN)
    assert find_proposal_episode_log(_paths(workspace), EPOCH, GENERATION) is None
    assert resolve_conversation(_paths(workspace), GENERATION, gen=GENERATION, epoch=EPOCH) is None


def test_a_generation_with_no_episode_answers_nothing(workspace: Path) -> None:
    assert resolve_conversation(_paths(workspace), "v9", gen="v9", epoch=EPOCH) is None


def test_an_epoch_that_does_not_hold_the_generation_falls_back_to_the_one_that_does(
    workspace: Path,
) -> None:
    """A generation id is unique workspace-wide, so a wrong epoch still resolves."""
    resolved = find_proposal_episode_log(_paths(workspace), "2026-08-01-other", GENERATION)
    assert resolved is not None
    assert resolved.parent.name == GENERATION


def test_a_slate_serves_its_lowest_slot_by_default_and_the_named_slot_on_request(
    tmp_path: Path,
) -> None:
    """A best-of-N round runs several episodes toward one generation id."""
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    for slot in (2, 0, 1):
        _write_jsonl(
            layout.proposal_episode_dir(EPOCH, GENERATION, slot) / "episode.jsonl", _EPISODE
        )
    paths = WorkspacePaths(root)
    assert find_proposal_episode_log(paths, EPOCH, GENERATION).parent.name == f"{GENERATION}-0"
    assert (
        find_proposal_episode_log(paths, EPOCH, GENERATION, slot_index=2).parent.name
        == f"{GENERATION}-2"
    )
    assert find_proposal_episode_log(paths, EPOCH, GENERATION, slot_index=7) is None


def test_the_dashboard_serves_the_proposal_episode_as_a_conversation(
    workspace: Path, tmp_path: Path
) -> None:
    """End to end through the route every other transcript is read from."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with TestClient(create_app(workspace, static, read_only=True)) as client:
        response = client.get(
            f"/api/conversation/{GENERATION}", params={"gen": GENERATION, "epoch": EPOCH}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["run_id"] == "ep_propose_v3"
        assert payload["execution"]["fidelity"] == "exact"
        assert payload["turns"][1]["text"] == "Editing the retry constant."

        board = client.get(
            "/api/conversation/run-v3-entry1",
            params={"gen": GENERATION, "entry": "entry1", "epoch": EPOCH},
        )
        assert board.status_code == 200
        assert board.json()["run_id"] == "run-v3-entry1"
