"""Durable FIELD-level tournament persistence (swiss / single-elim).

A completed non-gauntlet tournament settles ONE field record per round —
the round-by-round pairings + the Copeland standings + the proposing
field-status — which the per-challenger ``experiment.json`` audit cannot
reconstruct on its own. These tests pin the persistence path end to end:

* the orchestrator's settled-structure serialisers + the durable snapshot
  open writer (:func:`zicato.evolve.dashboard_projection._open_field_tournament`),
* the index ingest of that record
  (:func:`zicato.index.ingest.ingest_field_tournament` +
  ``rebuild_index`` from the snapshot file),
* the dashboard read path serving the field record for a completed swiss
  epoch (:func:`zicato.query.build_bracket` /
  ``build_tournament_structure``).

The field record is driven from a REAL :class:`SwissStrategy` resolved to
a settled decision, so the persisted shape is exactly what a live run
produces — the regression this guards is the old hardcoded empty
``standings_json`` / ``field_status_json``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from zicato.core.types import TournamentStructure
from zicato.core.workspace import field_tournament_path
from zicato.evolve.dashboard_projection import (
    _open_field_tournament,
    _serialise_rounds,
    _serialise_standings,
)
from zicato.index.ingest import ingest_field_tournament, rebuild_index
from zicato.query import (
    WorkspacePaths,
    build_bracket,
    build_tournament_structure,
)
from zicato.selection import make_strategy
from zicato.selection.strategy import Contestant, MatchupResult
from zicato.tournament.gate import GateOutcome

# ---------------------------------------------------------------------------
# Drive a real strategy to a settled decision (mirrors the live driver loop).
# ---------------------------------------------------------------------------


def _agg(scalar: float) -> dict[str, float]:
    return {"scalar": scalar}


def _result(matchup, *, left_scalar: float, right_scalar: float) -> MatchupResult:
    delta = right_scalar - left_scalar
    decision = "promoted" if delta < 0 else "rejected"
    return MatchupResult(
        matchup_id=matchup.matchup_id,
        left_id=matchup.left.generation_id,
        right_id=matchup.right.generation_id,
        left_agg=_agg(left_scalar),
        right_agg=_agg(right_scalar),
        outcome=GateOutcome(
            decision=decision,  # type: ignore[arg-type]
            reason="" if decision == "promoted" else "synthetic reject",
            delta_scalar=delta,
            delta_pass_rate=0.0,
        ),
        stage_index=matchup.stage_index,
        bracket_slot=matchup.bracket_slot,
    )


def _run_strategy(strategy, champion, challengers, scalars):
    strategy.seed(champion, challengers)
    guard = 0
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        for m in batch:
            strategy.record_result(
                _result(
                    m,
                    left_scalar=scalars[m.left.generation_id],
                    right_scalar=scalars[m.right.generation_id],
                )
            )
        guard += 1
        if guard > 100:
            raise AssertionError("strategy did not converge")
    return strategy.champion()


def _settle_swiss(structure: str = "swiss"):
    """Resolve a 4-challenger swiss; return ``(strategy, decision, competitors)``.

    Scalars make ``v3`` the field leader (lowest scalar) and let it clear
    the champion gate against ``v0`` — a promotion, so the standings carry
    a ``champion`` status row.
    """
    strategy = make_strategy(TournamentStructure(structure=structure, params={"field_size": 4}))
    champion = Contestant(generation_id="v0", role="champion")
    challengers = [Contestant(generation_id=f"v{i}", role="challenger") for i in (1, 2, 3, 4)]
    scalars = {"v0": 1.0, "v1": 0.9, "v2": 0.8, "v3": 0.3, "v4": 0.7}
    decision = _run_strategy(strategy, champion, challengers, scalars)
    competitors = [{"generation_id": "v0", "seed": 1, "role": "champion"}] + [
        {"generation_id": c.generation_id, "seed": i + 2, "role": "challenger"}
        for i, c in enumerate(challengers)
    ]
    return strategy, decision, competitors


def _field_record(strategy, decision, competitors, *, epoch_id: str, structure: str) -> dict:
    return {
        "tournament_id": f"{epoch_id}:field:v1",
        "epoch_id": epoch_id,
        "structure": structure,
        "structure_params": {"field_size": 4},
        "competitors": competitors,
        "rounds": _serialise_rounds(strategy.rounds()),
        "standings": _serialise_standings(decision.standings),
        "field_status": [
            {"generation_id": "v1", "status": "applied", "reason": "", "seed": 2},
            {"generation_id": "v2", "status": "applied", "reason": "", "seed": 3},
            {"generation_id": "v3", "status": "applied", "reason": "", "seed": 4},
            {"generation_id": "v4", "status": "applied", "reason": "", "seed": 5},
        ],
        "promoted_generation_id": decision.promoted_generation_id or "",
        "champion_generation_id": "v0",
        "decision": decision.decision,
        "reason": decision.reason,
        "delta_scalar": None,
        "ran_at": "2026-06-02T00:00:00Z",
    }


def _read_tournament_row(db: Path, tournament_id: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM tournaments WHERE tournament_id = ?",
            (tournament_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ingest_field_tournament — the field row carries non-empty standings/rounds
# ---------------------------------------------------------------------------


def test_settled_swiss_persists_field_row_with_standings_and_rounds(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    db = ws / "index.db"
    strategy, decision, competitors = _settle_swiss()
    record = _field_record(strategy, decision, competitors, epoch_id="e1", structure="swiss")

    ingest_field_tournament(ws, db, record)

    import json

    row = _read_tournament_row(db, "e1:field:v1")
    assert row is not None
    assert row["structure"] == "swiss"
    standings = json.loads(row["standings_json"])
    rounds = json.loads(row["rounds_json"])
    competitors_out = json.loads(row["competitors_json"])
    field_status = json.loads(row["field_status_json"])
    # The regression: these were hardcoded empty. They must now be populated.
    assert standings, "standings_json must not be empty for a settled swiss"
    assert rounds, "rounds_json must not be empty for a settled swiss"
    assert len(competitors_out) == 5
    assert field_status, "field_status_json must carry the proposing field"
    # Round pairings carry the dashboard match shape (competitors + winner).
    first_match = rounds[0]["matches"][0]
    assert "competitors" in first_match
    assert "winner" in first_match
    # The promoted leader carries a champion-status standing.
    assert any(s.get("status") == "champion" for s in standings)
    # The field row carries the crowning verdict in `decision` but leaves the
    # per-matchup parent/child columns empty (it is not a duel) so it never
    # collides with the per-challenger crowning row for the promoted gen.
    assert row["decision"] == decision.decision
    assert (row["parent_generation_id"] or "") == ""
    assert (row["child_generation_id"] or "") == ""


def test_ingest_field_tournament_is_idempotent(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    db = ws / "index.db"
    strategy, decision, competitors = _settle_swiss()
    record = _field_record(strategy, decision, competitors, epoch_id="e1", structure="swiss")

    ingest_field_tournament(ws, db, record)
    ingest_field_tournament(ws, db, record)

    conn = sqlite3.connect(str(db))
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM tournaments WHERE tournament_id = ?",
            ("e1:field:v1",),
        ).fetchone()
    finally:
        conn.close()
    assert count == 1


def test_gauntlet_field_writes_no_field_row(tmp_path: Path) -> None:
    """A degenerate two-competitor field is a no-op (per-challenger row suffices)."""
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    db = ws / "index.db"
    record = {
        "tournament_id": "e1:field:v1",
        "epoch_id": "e1",
        "structure": "gauntlet",
        "competitors": [
            {"generation_id": "v0", "seed": 1, "role": "champion"},
            {"generation_id": "v1", "seed": 2, "role": "challenger"},
        ],
        "rounds": [],
        "standings": [],
        "field_status": [],
    }
    ingest_field_tournament(ws, db, record)
    assert _read_tournament_row(db, "e1:field:v1") is None


# ---------------------------------------------------------------------------
# _open_field_tournament — in-progress snapshot + dual-write
# ---------------------------------------------------------------------------


def test_open_writes_in_progress_snapshot_and_dual_writes(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    _strategy, _decision, competitors = _settle_swiss()

    _open_field_tournament(
        ws,
        field_tournament_id="e1:field:v1",
        first_challenger_id="v1",
        epoch_id="e1",
        structure="swiss",
        structure_params={"field_size": 4},
        competitors=competitors,
        field_status=[{"generation_id": "v1", "status": "applied", "reason": "", "seed": 2}],
    )

    # The durable snapshot exists and describes work in progress.
    snap = field_tournament_path(ws, "e1", "v1")
    assert snap.exists()
    import json

    blob = json.loads(snap.read_text())
    assert blob["state"] == "in_progress"
    assert blob["standings"] == []
    assert blob["rounds"] == []

    # The dual-write put the field row in the index immediately.
    row = _read_tournament_row(ws / "index.db", "e1:field:v1")
    assert row is not None
    assert json.loads(row["standings_json"]) == []


def test_rebuild_index_rederives_field_row_from_snapshot(tmp_path: Path) -> None:
    """``zicato repair index`` must reconstruct the field row from the snapshot file."""
    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch

    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    epoch_id = cfg.id

    strategy, decision, competitors = _settle_swiss()
    record = _field_record(strategy, decision, competitors, epoch_id=epoch_id, structure="swiss")

    # Write ONLY the durable snapshot (no index), then rebuild from files.
    snap = field_tournament_path(ws, epoch_id, "v1")
    snap.parent.mkdir(parents=True, exist_ok=True)
    import json

    snap.write_text(json.dumps(record), encoding="utf-8")

    db = rebuild_index(ws)
    row = _read_tournament_row(db, f"{epoch_id}:field:v1")
    assert row is not None
    assert json.loads(row["standings_json"])
    assert json.loads(row["rounds_json"])


# ---------------------------------------------------------------------------
# Read path — a completed swiss epoch serves the field record
# ---------------------------------------------------------------------------


def _completed_swiss_workspace(tmp_path: Path, structure: str = "swiss") -> Path:
    ws = tmp_path / ".zicato"
    (ws / "epochs" / "e1").mkdir(parents=True)
    db = ws / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INT);
        CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT,
            hypothesis_core_idea TEXT);
        CREATE TABLE tournaments(tournament_id TEXT PRIMARY KEY, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT, structure TEXT,
            structure_params_json TEXT, competitors_json TEXT, rounds_json TEXT,
            standings_json TEXT, field_status_json TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?)",
        [
            ("e1", "v0", None, 1),
            ("e1", "v1", "v0", 0),
            ("e1", "v2", "v0", 0),
            ("e1", "v3", "v0", 1),
            ("e1", "v4", "v0", 0),
        ],
    )
    strategy, decision, competitors = _settle_swiss(structure)
    import json

    rounds = _serialise_rounds(strategy.rounds())
    standings = _serialise_standings(decision.standings)
    # The per-challenger crowning rows (the old, empty-structure rows) plus
    # the one field row. The read path must serve the FIELD row for the
    # structure view and suppress the per-challenger ones.
    for child in ("v1", "v2", "v3", "v4"):
        conn.execute(
            "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"e1:v0->{child}",
                "e1",
                "v0",
                child,
                "rejected",
                None,
                None,
                0.0,
                "",
                "2026-06-02T00:00:00Z",
                structure,
                json.dumps({}),
                json.dumps(["v0", child]),
                json.dumps([{"match_id": "m0", "opponent": "v0", "won": False}]),
                json.dumps([]),  # the old empty standings — the bug
                json.dumps([]),
            ),
        )
    conn.execute(
        "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "e1:field:v1",
            "e1",
            "",
            "",
            decision.decision,
            None,
            None,
            None,
            "",
            "2026-06-02T00:00:01Z",
            structure,
            json.dumps({"field_size": 4}),
            json.dumps(competitors),
            json.dumps(rounds),
            json.dumps(standings),
            json.dumps([{"generation_id": "v1", "status": "applied", "reason": "", "seed": 2}]),
        ),
    )
    conn.commit()
    conn.close()
    return ws


def test_build_bracket_serves_field_record_for_completed_swiss(tmp_path: Path) -> None:
    ws = _completed_swiss_workspace(tmp_path)
    brk = build_bracket(WorkspacePaths(ws), "e1")
    assert brk["structure"] == "swiss"
    # The structure view's tournaments[] must carry exactly ONE swiss record
    # (the field record) — the per-challenger crowning rows are suppressed so
    # the frontend's ``nonGaunt.pop()`` resolves to the field ladder.
    swiss = [t for t in brk["tournaments"] if t["structure"] == "swiss"]
    assert len(swiss) == 1
    assert swiss[0]["tournament_id"] == "e1:field:v1"
    assert swiss[0]["standings"], "the field record must carry standings"
    assert swiss[0]["rounds"], "the field record must carry round pairings"
    # The legacy per-matchup list carries the 4 per-challenger crowning rows
    # only — the field row is excluded (it is not a champion-vs-challenger
    # duel), so the gauntlet matchup reader stays coherent.
    assert len(brk["matchups"]) == 4
    assert all(m["challenger"] in {"v1", "v2", "v3", "v4"} for m in brk["matchups"])


def test_build_tournament_structure_resolves_field_record(tmp_path: Path) -> None:
    ws = _completed_swiss_workspace(tmp_path)
    st = build_tournament_structure(WorkspacePaths(ws), "e1", "e1:field:v1")
    assert st["structure"] == "swiss"
    assert st["standings"], "completed swiss must return populated standings"
    assert st["rounds"], "completed swiss must return populated rounds"
    assert st["source"] == "index"
    assert any(s.get("status") == "champion" for s in st["standings"])


def test_build_bracket_serves_field_record_for_completed_single_elim(tmp_path: Path) -> None:
    ws = _completed_swiss_workspace(tmp_path, structure="single_elim")
    brk = build_bracket(WorkspacePaths(ws), "e1")
    assert brk["structure"] == "single_elim"
    elim = [t for t in brk["tournaments"] if t["structure"] == "single_elim"]
    assert len(elim) == 1
    assert elim[0]["tournament_id"] == "e1:field:v1"
    assert elim[0]["rounds"], "the single-elim field record must carry round pairings"
