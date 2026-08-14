"""U2 query hygiene: hoisted readers' DQ3 degrades + the read-only index pin.

Two families of pins:

* Every endpoint blob hoisted into ``zicato.query`` (the transcript /
  conversation / journal / analysis / run-result readers) degrades to its
  SAME-shaped empty payload on a missing input — never raises (DQ3) —
  and keeps its happy-path shape.
* Every reader's index connection goes through
  ``zicato.query._sqlite.open_index_ro`` — a READ-ONLY open. The
  regression pin writes through that connection path and asserts sqlite
  refuses; the source pin asserts no reader hand-rolls a bare
  ``sqlite3.connect`` (write mode: lock contention with the ingest
  writer, plus a stray ``index.db`` created on a never-indexed
  workspace — the ``judge_view`` bug class this retired).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from zicato.query import (
    WorkspacePaths,
    build_epoch_analysis,
    build_matchup_conversations,
    build_per_judge_trend,
    build_run_transcript,
    empty_run_transcript,
    read_epoch_journal,
    read_epoch_journal_md,
    read_run_result,
    resolve_conversation,
    resolve_run_id_for_entry,
)
from zicato.query._sqlite import _IndexAbsent, open_index_ro, open_index_ro_or_none

EPOCH = "2026-06-01_e0"
GEN = "v1"
ENTRY = "entry-a"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _base_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False})
    return ws


class _FakeTranscript:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


def _fake_reconstruct(payload: dict):
    def reconstruct(events_path: Path, *, partial_ok: bool = False) -> _FakeTranscript:
        return _FakeTranscript(payload)

    return reconstruct


# ---------------------------------------------------------------------------
# open_index_ro — the read-only discipline itself
# ---------------------------------------------------------------------------


def test_open_index_ro_refuses_writes(tmp_path: Path) -> None:
    """The shared connection path is READ-ONLY: a write through it fails."""
    db = tmp_path / "index.db"
    seed = sqlite3.connect(db)
    seed.execute("CREATE TABLE judge_losses(judge_name TEXT)")
    seed.commit()
    seed.close()
    with open_index_ro(db) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO judge_losses VALUES ('j1')")


def test_open_index_ro_raises_index_absent_and_creates_nothing(tmp_path: Path) -> None:
    """A missing db raises ``_IndexAbsent`` — and NEVER creates a stray file."""
    db = tmp_path / "index.db"
    with pytest.raises(_IndexAbsent):
        with open_index_ro(db):
            pass
    assert not db.exists()


def test_open_index_ro_or_none_yields_none_when_absent(tmp_path: Path) -> None:
    with open_index_ro_or_none(tmp_path / "index.db") as conn:
        assert conn is None
    assert not (tmp_path / "index.db").exists()


def test_no_reader_hand_rolls_a_sqlite_connect() -> None:
    """Source pin: every query reader opens the index via open_index_ro.

    A bare ``sqlite3.connect`` in a reader defaults to WRITE mode (lock
    contention with the ingest writer; creates a stray db on a missing
    path — whose presence then flips LATER readers' degrade branches).
    The one allowed home is ``_sqlite.py`` itself.
    """
    import zicato.query as q

    pkg_dir = Path(q.__file__).parent
    offenders = [
        p.name
        for p in sorted(pkg_dir.glob("*.py"))
        if p.name != "_sqlite.py" and "sqlite3.connect(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"bare sqlite3.connect in query readers: {offenders}"


def test_per_judge_trend_never_creates_index_db(tmp_path: Path) -> None:
    """Regression: the old write-mode connect CREATED index.db; ro must not.

    A missing index degrades field-by-field: empty ``judges``, the
    lineage-derived ``generations`` spine, NO stray file left behind.
    """
    ws = _base_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    out = build_per_judge_trend(paths, EPOCH)
    # F5: an absent index now carries the harmonized degrade note (the
    # generations spine still renders field-by-field).
    assert out == {
        "epoch_id": EPOCH,
        "generations": [],
        "judges": [],
        "note": "index not built; run zicato repair index",
    }
    assert not paths.index_db.exists()


# ---------------------------------------------------------------------------
# journal_view — read_epoch_journal / read_epoch_journal_md
# ---------------------------------------------------------------------------


def test_read_epoch_journal_degrades_to_empty_string(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    out = read_epoch_journal(paths, EPOCH)
    assert out == {"epoch_id": EPOCH, "journal": ""}


def test_read_epoch_journal_shape(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    (ws / "epochs" / EPOCH / "journal.md").write_text("# log\nentry", encoding="utf-8")
    out = read_epoch_journal(WorkspacePaths(ws), EPOCH)
    assert out == {"epoch_id": EPOCH, "journal": "# log\nentry"}


def test_read_epoch_journal_md_none_vs_text(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    assert read_epoch_journal_md(paths, EPOCH) is None
    (ws / "epochs" / EPOCH / "journal.md").write_text("raw", encoding="utf-8")
    assert read_epoch_journal_md(paths, EPOCH) == "raw"


# ---------------------------------------------------------------------------
# epoch_view — build_epoch_analysis
# ---------------------------------------------------------------------------


def test_build_epoch_analysis_degrades_same_shape(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    out = build_epoch_analysis(paths, EPOCH)
    assert out == {
        "epoch_id": EPOCH,
        "analysis_md": "",
        "analysis_html_inline": "",
        "analysis_html_available": False,
    }


def test_build_epoch_analysis_shape_with_report(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    (ws / "epochs" / EPOCH / "analysis.md").write_text("# report", encoding="utf-8")
    (ws / "epochs" / EPOCH / "analysis.html").write_text("<html></html>", encoding="utf-8")
    out = build_epoch_analysis(WorkspacePaths(ws), EPOCH)
    assert out["epoch_id"] == EPOCH
    assert out["analysis_md"] == "# report"
    assert out["analysis_html_available"] is True
    assert isinstance(out["analysis_html_inline"], str)  # best-effort fragment


# ---------------------------------------------------------------------------
# events_index — read_run_result
# ---------------------------------------------------------------------------


def test_read_run_result_none_when_absent(tmp_path: Path) -> None:
    assert read_run_result(tmp_path) is None
    (tmp_path / "loss.json").write_text("not json", encoding="utf-8")
    assert read_run_result(tmp_path) is None  # malformed reads as absent


def test_read_run_result_projects_the_dashboard_subset(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "loss.json",
        {
            "run_id": "r1",
            "drift_loss": 0.5,
            "pass_fail": True,
            "runtime_ms": 1200,
            "wall_clock_budget_exceeded": False,
            "expectation_result": {"kind": "regex", "passed": True, "detail": "ok"},
            "metric_counts": [{"name": "llm_calls", "count": 3, "severity": "info"}],
            "schema_version": 9,  # internal field — must NOT leak
        },
    )
    out = read_run_result(tmp_path)
    assert out == {
        "wall_clock_budget_exceeded": False,
        "runtime_ms": 1200,
        "pass_fail": True,
        "expectation_result": {"kind": "regex", "passed": True, "detail": "ok"},
        "metric_counts": [{"name": "llm_calls", "count": 3.0, "severity": "info"}],
        "drift_loss": 0.5,
    }


# ---------------------------------------------------------------------------
# judge_view — resolve_run_id_for_entry
# ---------------------------------------------------------------------------


def test_resolve_run_id_for_entry_reads_loss_json(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    run_dir = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY
    _write_json(run_dir / "loss.json", {"run_id": "canonical-run-7"})
    assert resolve_run_id_for_entry(WorkspacePaths(ws), EPOCH, GEN, ENTRY) == "canonical-run-7"


def test_resolve_run_id_for_entry_falls_back_to_entry_id(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    assert resolve_run_id_for_entry(paths, EPOCH, GEN, ENTRY) == ENTRY


# ---------------------------------------------------------------------------
# transcript_view — resolve_conversation / build_run_transcript
# ---------------------------------------------------------------------------


def test_resolve_conversation_none_on_empty_workspace(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    assert resolve_conversation(paths, "nope") is None
    assert resolve_conversation(paths, "nope", gen=GEN, entry=ENTRY, epoch=EPOCH) is None


def test_resolve_conversation_finds_the_entry_events(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    events = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"runId": "r1"}\n', encoding="utf-8")
    got = resolve_conversation(WorkspacePaths(ws), "r1", gen=GEN, entry=ENTRY, epoch=EPOCH)
    assert got == events


def test_build_run_transcript_unavailable_without_reconstructor(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    out = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=None)
    assert out == empty_run_transcript(
        EPOCH, GEN, ENTRY, error="transcript reconstruction unavailable"
    )


def test_build_run_transcript_absent_run_is_honest_empty(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    out = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=_fake_reconstruct({}))
    assert out["turns"] == []
    assert out["event_count"] == 0
    assert out["complete"] is False
    assert "error" not in out  # genuine absence carries NO error key


def test_build_run_transcript_stamps_coordinates(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    events = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"runId": "r1"}\n', encoding="utf-8")
    payload = {"run_id": "", "turns": [{"role": "user"}], "annotations": [], "event_count": 1}
    out = build_run_transcript(
        WorkspacePaths(ws), EPOCH, GEN, ENTRY, reconstruct=_fake_reconstruct(payload)
    )
    # The documented stamping step: coordinates + fallback run_id.
    assert out["epoch_id"] == EPOCH
    assert out["generation_id"] == GEN
    assert out["entry_id"] == ENTRY
    assert out["run_id"] == ENTRY  # reducer produced no run_id -> directory-name fallback
    assert out["turns"] == [{"role": "user"}]


def test_build_run_transcript_failure_degrades_same_shape(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    events = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"runId": "r1"}\n', encoding="utf-8")

    def boom(events_path: Path, *, partial_ok: bool = False) -> None:
        raise RuntimeError("torn read")

    out = build_run_transcript(WorkspacePaths(ws), EPOCH, GEN, ENTRY, reconstruct=boom)
    assert out["error"] == "transcript failed: torn read"
    assert out["turns"] == []
    assert out["run_id"] == ENTRY


# ---------------------------------------------------------------------------
# transcript_view — the PARTIAL (in-flight) reconstruction path, wired through
# the REAL reconstructor (exactly as the /api/run/.../transcript endpoint injects
# it). Pins: a growing events.jsonl surfaces more turns across reads; a torn tail
# line is tolerated (never raises); a settled run's served body is identical to a
# direct reconstruction (partial_ok does not perturb a completed run).
# ---------------------------------------------------------------------------


def _live_events_path(ws: Path) -> Path:
    p = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _evline(kind: str, payload: dict, seq: int) -> str:
    return json.dumps(
        {
            kind: payload,
            "runId": "run-live",
            "sequence": str(seq),
            "emittedAt": f"2026-06-01T00:00:{seq:02d}Z",
        }
    )


def _agent_turn(agent: str, text: str, seq: int) -> list[str]:
    # A merged reasoning turn (start+end, same agent) — the proven single-turn
    # shape from the reconstructor's own tests.
    return [
        _evline(
            "goldfiveLlmCallStart",
            {"name": "reason", "inputPreview": text, "targetAgentId": agent},
            seq,
        ),
        _evline(
            "goldfiveLlmCallEnd",
            {"name": "reason", "decisionSummary": text, "targetAgentId": agent},
            seq + 1,
        ),
    ]


def test_build_run_transcript_partial_grows_across_reads(tmp_path: Path) -> None:
    from zicato.dashboard.transcript import reconstruct_transcript

    ws = _base_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    events = _live_events_path(ws)

    # In-flight: a runStarted goal + one agent turn, NO runCompleted (not terminal).
    lines = [
        _evline("runStarted", {"goalSummary": "Build a thing"}, 0),
        *_agent_turn("alpha", "first thought", 1),
    ]
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    first = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=reconstruct_transcript)
    assert first["complete"] is False  # partial: no terminal event seen
    n_first = len(first["turns"])
    assert n_first >= 2  # goal turn + the alpha turn

    # A new agent turn lands on disk (still no terminal) — a fresh read GROWS.
    with events.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_agent_turn("beta", "second thought", 3)) + "\n")

    second = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=reconstruct_transcript)
    assert second["complete"] is False
    assert len(second["turns"]) > n_first  # the partial transcript grew across reads
    assert second["epoch_id"] == EPOCH
    assert second["generation_id"] == GEN
    assert second["entry_id"] == ENTRY


def test_build_run_transcript_partial_tolerates_torn_tail_line(tmp_path: Path) -> None:
    from zicato.dashboard.transcript import reconstruct_transcript

    ws = _base_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    events = _live_events_path(ws)

    good = [
        _evline("runStarted", {"goalSummary": "Build a thing"}, 0),
        *_agent_turn("alpha", "first thought", 1),
    ]
    # A writer caught mid-flush: the final line is a truncated JSON fragment with
    # no trailing newline. The endpoint must never raise; the good turns survive.
    events.write_text(
        "\n".join(good) + "\n" + '{"goldfiveLlmCallStart": {"name": "rea',
        encoding="utf-8",
    )

    out = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=reconstruct_transcript)
    assert "error" not in out  # tolerated — not the failure-degrade shape
    assert len(out["turns"]) >= 2  # the intact turns before the torn tail
    assert out["complete"] is False  # a torn tail is never a clean completion


def test_build_run_transcript_settled_run_matches_direct_reconstruct(tmp_path: Path) -> None:
    from zicato.dashboard.transcript import reconstruct_transcript

    ws = _base_workspace(tmp_path)
    paths = WorkspacePaths(ws)
    events = _live_events_path(ws)

    lines = [
        _evline("runStarted", {"goalSummary": "Build a thing"}, 0),
        *_agent_turn("alpha", "first thought", 1),
        _evline("runCompleted", {"outcomeSummary": "done"}, 3),
    ]
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = build_run_transcript(paths, EPOCH, GEN, ENTRY, reconstruct=reconstruct_transcript)
    assert out["complete"] is True  # a terminal event → settled

    # The served body is byte-identical to a direct reconstruction — the endpoint
    # adds only the stamped coordinates, and partial_ok does not perturb a
    # completed run (so a settled read is exactly today's payload).
    direct = reconstruct_transcript(events, partial_ok=True).to_dict()
    assert out["turns"] == direct["turns"]
    assert out["annotations"] == direct["annotations"]
    assert out["complete"] == direct["complete"]
    assert out["event_count"] == direct["event_count"]


# ---------------------------------------------------------------------------
# conversations_view — build_matchup_conversations
# ---------------------------------------------------------------------------


def test_matchup_conversations_degrade_without_tournament(tmp_path: Path) -> None:
    paths = WorkspacePaths(_base_workspace(tmp_path))
    assert build_matchup_conversations(paths, ENTRY) == {"champion": None, "challenger": None}


def test_matchup_conversations_shape_both_sides(tmp_path: Path) -> None:
    ws = _base_workspace(tmp_path)
    _write_json(
        ws / "runtime" / "active_tournament.json",
        {
            "tournament_id": "t1",
            "parent_generation_id": "v0",
            "child_generation_id": GEN,
            "entries": [
                {"entry_id": ENTRY, "side": "parent", "status": "cached"},
                {"entry_id": ENTRY, "side": "child", "status": "completed"},
            ],
        },
    )
    for gen in ("v0", GEN):
        run_dir = ws / "epochs" / EPOCH / "generations" / gen / "runs" / ENTRY
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text('{"runId": "r"}\n', encoding="utf-8")
        _write_json(run_dir / "loss.json", {"drift_loss": 0.1, "pass_fail": True})
    out = build_matchup_conversations(
        WorkspacePaths(ws), ENTRY, reconstruct=_fake_reconstruct({"turns": []})
    )
    for side, gen in (("champion", "v0"), ("challenger", GEN)):
        record = out[side]
        assert record is not None
        assert record["generation_id"] == gen
        assert record["run_id"] == ENTRY
        assert record["transcript"] == {"turns": []}
        assert record["result"]["pass_fail"] is True


def test_matchup_conversations_no_reconstructor_still_serves_results(tmp_path: Path) -> None:
    """DQ3: an absent reconstructor degrades transcripts, not the payload."""
    ws = _base_workspace(tmp_path)
    _write_json(
        ws / "runtime" / "active_tournament.json",
        {
            "tournament_id": "t1",
            "parent_generation_id": "v0",
            "child_generation_id": GEN,
            "entries": [],
        },
    )
    run_dir = ws / "epochs" / EPOCH / "generations" / GEN / "runs" / ENTRY
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"runId": "r"}\n', encoding="utf-8")
    out = build_matchup_conversations(WorkspacePaths(ws), ENTRY)
    assert out["challenger"]["transcript"] is None
    assert out["challenger"]["generation_id"] == GEN
