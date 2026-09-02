"""The live conversation pane's cursor-append read (issue #194 §2).

Exercises :func:`zicato.query.build_run_transcript_delta` against a
hand-grown ``events.jsonl`` — the same file a running harness appends to.
The properties under test are the ones the follow pane depends on:

* cursor idempotence — replaying a cursor against an unchanged file
  yields an EMPTY delta (no turn can have a source index past it);
* pure append — a grown file returns only the new turns, each stamped
  with the ``turn_index`` it occupies in the full reconstruction;
* the OPEN turn — a final turn that absorbs a further event comes back
  at its existing index rather than as a phantom new turn;
* torn-line tolerance — a half-written final line takes no cursor
  position, so the completed line arrives whole on the next read (this
  mirrors the run-log reader's own tolerance of a mid-flush writer);
* per-run sequence restarts — a multi-run file (a
  ``multi_turn_emulated`` entry) still advances the cursor monotonically,
  which is precisely why the cursor counts parsed events rather than
  goldfive ``sequence`` numbers;
* the same-shaped degrades (absent run, failed reconstruction, bad id).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.query import (
    FIDELITY_EVENTS,
    build_run_transcript_delta,
    empty_run_transcript_delta,
    transcript_view,
)
from zicato.query.paths import WorkspacePaths

EPOCH = "2026-08-09_e1"
GEN = "v3"
ENTRY = "waffles_single"


# ---------------------------------------------------------------------------
# Fixture helpers — a workspace with one gen×entry run, appended to by hand
# ---------------------------------------------------------------------------


def _events_path(root: Path) -> Path:
    run_dir = root / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "events.jsonl"


def _line(kind: str, payload: dict[str, Any], seq: int, *, run_id: str = "run-a") -> str:
    return json.dumps(
        {
            "kind": kind,
            "payload": payload,
            "sequence": seq,
            "runId": run_id,
            "emittedAt": f"2026-08-09T10:00:{seq:02d}Z",
        }
    )


def _said(agent: str, text: str, seq: int) -> str:
    """One agent turn. Distinct ``agent_name``s keep turns from merging —
    the reconstructor folds consecutive same-agent events into one turn,
    which is a separate behaviour from the cursor under test here."""
    return _line("task_completed", {"agent_name": agent, "summary": text}, seq)


def _append(path: Path, lines: list[str]) -> None:
    """Append whole lines — what a running harness does to its events file."""
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _delta(root: Path, after: int | None = None, **kw: Any) -> dict[str, Any]:
    return build_run_transcript_delta(
        WorkspacePaths(root),
        EPOCH,
        GEN,
        ENTRY,
        after=after,
        **kw,
    )


def _opening(path: Path) -> None:
    """The first two events: a user goal and one agent task completion."""
    _append(
        path,
        [
            _line("run_started", {"goal_summary": "bake waffles"}, 0),
            _said("mixer", "measured the flour", 1),
        ],
    )


# ---------------------------------------------------------------------------
# Cursor idempotence + pure append
# ---------------------------------------------------------------------------


def test_initial_read_returns_every_turn_and_a_cursor(tmp_path: Path) -> None:
    _opening(_events_path(tmp_path))

    out = _delta(tmp_path)

    assert out["found"] is True
    assert out["cursor"] == 2  # two parsed events consumed
    assert out["turn_total"] == len(out["turns"]) == 2
    assert [t["turn_index"] for t in out["turns"]] == [0, 1]
    assert out["turns"][0]["text"] == "bake waffles"
    assert out["turns"][1]["text"] == "measured the flour"
    assert out["complete"] is False  # no terminal event yet
    assert out["truncated"] is False


def test_same_cursor_twice_is_an_empty_delta(tmp_path: Path) -> None:
    _opening(_events_path(tmp_path))

    first = _delta(tmp_path)
    again = _delta(tmp_path, after=first["cursor"])

    assert again["turns"] == []
    assert again["annotations"] == []
    # The cursor is unchanged and the totals still describe the whole file:
    # an empty delta is "nothing new", never "nothing there".
    assert again["cursor"] == first["cursor"]
    assert again["turn_total"] == first["turn_total"]
    assert again["found"] is True

    # And a third replay of the SAME cursor is still empty — idempotent.
    assert _delta(tmp_path, after=first["cursor"])["turns"] == []


def test_append_returns_only_the_new_turn(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _opening(path)
    cursor = _delta(tmp_path)["cursor"]

    _append(path, [_said("pourer", "poured the batter", 2)])
    out = _delta(tmp_path, after=cursor)

    assert [t["text"] for t in out["turns"]] == ["poured the batter"]
    assert [t["turn_index"] for t in out["turns"]] == [2]
    assert out["turn_total"] == 3
    assert out["cursor"] == 3


def test_open_turn_that_grows_comes_back_at_its_own_index(tmp_path: Path) -> None:
    """The llmCallStart → llmCallEnd merge: one turn, two events.

    A follower that has already rendered the turn must receive it AGAIN
    (so its text can be replaced in place), not a phantom new turn — the
    delta is keyed on the highest event index the turn absorbed.
    """
    path = _events_path(tmp_path)
    _append(path, [_line("goldfive_llm_call_start", {"input_preview": "thinking…"}, 0)])
    first = _delta(tmp_path)
    assert [t["turn_index"] for t in first["turns"]] == [0]

    # The SAME turn absorbs a second event (same role, same agent → merged).
    _append(path, [_line("goldfive_llm_call_end", {"decision_summary": "decided"}, 1)])
    out = _delta(tmp_path, after=first["cursor"])

    assert out["turn_total"] == 1
    assert [t["turn_index"] for t in out["turns"]] == [0]
    assert "decided" in out["turns"][0]["text"]


# ---------------------------------------------------------------------------
# Torn line at the tail — the run-log reader's tolerance, mirrored
# ---------------------------------------------------------------------------


def test_torn_final_line_does_not_advance_the_cursor(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _opening(path)
    settled = _delta(tmp_path)

    # A writer caught mid-flush: the final line is half a JSON object.
    tail = _said("flipper", "flipped it", 2)
    torn_at = tail.index('"flipped it"') + 6
    with path.open("a", encoding="utf-8") as handle:
        handle.write(tail[:torn_at])

    torn = _delta(tmp_path, after=settled["cursor"])
    assert torn["turns"] == []
    assert torn["cursor"] == settled["cursor"]

    # The writer finishes the line. It now parses, takes the next cursor
    # position, and arrives whole — nothing was lost and nothing doubled.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(tail[torn_at:] + "\n")

    healed = _delta(tmp_path, after=settled["cursor"])
    assert [t["text"] for t in healed["turns"]] == ["flipped it"]
    assert healed["cursor"] == settled["cursor"] + 1


def test_blank_lines_take_no_cursor_position(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _opening(path)
    settled = _delta(tmp_path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    out = _delta(tmp_path, after=settled["cursor"])
    assert out["turns"] == []
    assert out["cursor"] == settled["cursor"]


# ---------------------------------------------------------------------------
# Multi-run files — why the cursor is not the goldfive sequence
# ---------------------------------------------------------------------------


def test_second_run_restarting_sequence_still_advances_the_cursor(tmp_path: Path) -> None:
    """A ``multi_turn_emulated`` entry writes N runs into one events file.

    Each run restarts ``sequence`` at 0, so a sequence-keyed cursor would
    read run 2's events as already-seen. The parsed-event cursor does not.
    """
    path = _events_path(tmp_path)
    _opening(path)
    cursor = _delta(tmp_path)["cursor"]

    # Run 2 — sequence numbering starts over, timestamps run later.
    _append(
        path,
        [
            json.dumps(
                {
                    "kind": "run_started",
                    "payload": {"goal_summary": "and now pancakes"},
                    "sequence": 0,
                    "runId": "run-b",
                    "emittedAt": "2026-08-09T11:00:00Z",
                }
            )
        ],
    )
    out = _delta(tmp_path, after=cursor)

    assert [t["text"] for t in out["turns"]] == ["and now pancakes"]
    assert out["cursor"] == 3
    assert out["turns"][0]["run_index"] == 2


# ---------------------------------------------------------------------------
# Annotations ride the same cursor
# ---------------------------------------------------------------------------


def test_annotations_are_filtered_by_the_same_cursor(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _opening(path)
    cursor = _delta(tmp_path)["cursor"]

    _append(
        path,
        [_line("drift_detected", {"kind": "SCOPE", "detail": "wandered off"}, 2)],
    )
    out = _delta(tmp_path, after=cursor)

    assert out["turns"] == []
    assert [a["kind"] for a in out["annotations"]] == ["drift"]
    assert "wandered off" in out["annotations"][0]["summary"]
    # …and replaying the new cursor drops it again.
    assert _delta(tmp_path, after=out["cursor"])["annotations"] == []


# ---------------------------------------------------------------------------
# Completion + fidelity
# ---------------------------------------------------------------------------


def test_terminal_event_flips_complete_and_verbatim_is_reported(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _opening(path)
    assert _delta(tmp_path)["complete"] is False

    _append(path, [_line("run_completed", {"outcome_summary": "waffles"}, 2)])
    out = _delta(tmp_path)

    assert out["complete"] is True
    assert out["fidelity"] == FIDELITY_EVENTS
    # No result.json beside the events file — the higher tier is absent, and
    # the caption must not claim one.
    assert out["verbatim_available"] is False
    assert out["events_path"] == str(path)


def test_verbatim_available_tracks_a_valid_result_json(tmp_path: Path) -> None:
    from zicato.tournament.unit_cache import RUN_RESULT_FORMAT_VERSION

    path = _events_path(tmp_path)
    _opening(path)
    result = path.parent / "result.json"

    # A truncated / wrong-version capture is NOT a verbatim capture.
    result.write_text('{"format_version": "nope"}', encoding="utf-8")
    assert _delta(tmp_path)["verbatim_available"] is False

    result.write_text(
        json.dumps({"format_version": RUN_RESULT_FORMAT_VERSION, "transcript": ["hi"]}),
        encoding="utf-8",
    )
    assert _delta(tmp_path)["verbatim_available"] is True


# ---------------------------------------------------------------------------
# Clamping + the far-behind follower
# ---------------------------------------------------------------------------


def test_a_delta_past_the_limit_answers_its_tail_and_says_so(tmp_path: Path) -> None:
    path = _events_path(tmp_path)
    _append(
        path,
        [_said(f"worker{i}", f"step {i}", i) for i in range(12)],
    )

    out = _delta(tmp_path, limit=5)

    assert out["truncated"] is True
    assert len(out["turns"]) == 5
    assert out["turn_total"] == 12
    # It is the TAIL, and its first index proves the gap the client must
    # heal with a full re-read.
    assert [t["turn_index"] for t in out["turns"]] == [7, 8, 9, 10, 11]

    # Within the limit nothing is truncated.
    assert _delta(tmp_path, limit=50)["truncated"] is False


def test_negative_and_absent_cursors_read_from_the_top(tmp_path: Path) -> None:
    _opening(_events_path(tmp_path))

    assert len(_delta(tmp_path, after=None)["turns"]) == 2
    assert len(_delta(tmp_path, after=-5)["turns"]) == 2
    # Zero is a REAL cursor ("I consumed nothing"), not a sentinel — it also
    # yields everything, because every source index is >= 0.
    assert len(_delta(tmp_path, after=0)["turns"]) == 2


# ---------------------------------------------------------------------------
# Same-shaped degrades (DQ3)
# ---------------------------------------------------------------------------


def test_absent_run_degrades_to_the_not_found_shape(tmp_path: Path) -> None:
    (tmp_path / "epochs").mkdir()

    out = _delta(tmp_path)

    assert out["found"] is False
    assert out["turns"] == [] and out["annotations"] == []
    assert out["cursor"] == 0
    assert "error" not in out  # genuine absence carries no error


def test_failed_reconstruction_degrades_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _opening(_events_path(tmp_path))

    def boom(events_path: Path, *, partial_ok: bool = False) -> None:
        raise RuntimeError("torn read")

    monkeypatch.setattr(transcript_view, "reconstruct_transcript", boom)
    out = _delta(tmp_path)

    assert out["found"] is False
    assert out["error"] == "transcript failed: torn read"


def test_every_degrade_shares_the_full_key_set(tmp_path: Path) -> None:
    """A follower must never branch on which failure it hit."""
    _opening(_events_path(tmp_path))
    live = _delta(tmp_path)

    for degraded in (
        empty_run_transcript_delta(EPOCH, GEN, ENTRY),
        empty_run_transcript_delta(EPOCH, GEN, ENTRY, error="invalid id"),
        _delta(tmp_path.parent / "nowhere"),
    ):
        assert set(live) <= set(degraded)
        assert degraded["fidelity"] == FIDELITY_EVENTS
