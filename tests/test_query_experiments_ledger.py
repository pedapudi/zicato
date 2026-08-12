"""Known-answer tests for the EXPERIMENTS LEDGER reader (issue #194 §3).

``build_experiments_ledger`` is a pure join over the analytical index:
``experiments`` (idea + verdict + deltas) × ``patches`` (the sites touched)
× ``generations`` (the birth round). The tests pin the three things a join
can get wrong — the SHAPE of a row, the CORRECTNESS of the join (sites land
on their own experiment; the round comes from the generation), and the
DEGRADES (no index / no epoch / unknown epoch / a legacy index with no
``round_index`` column) — plus the round ordering the ledger renders in.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.index.schema import apply_schema
from zicato.query import build_experiments_ledger
from zicato.query.paths import WorkspacePaths

EPOCH = "e_ledger"


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace)


def _seed_workspace(workspace: Path) -> None:
    """The on-disk epoch the id resolver validates against."""
    edir = workspace / "epochs" / EPOCH
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "config.json").write_text(
        json.dumps({"id": EPOCH, "created_at": "2026-07-01", "closed": True}),
        encoding="utf-8",
    )
    (workspace / "current_epoch").write_text(EPOCH, encoding="utf-8")


def _gen(
    conn: sqlite3.Connection,
    gid: str,
    parent: str | None,
    promoted: int,
    created: str,
    round_index: int | None,
) -> None:
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at, round_index) VALUES(?,?,?,?,?,?)",
        (EPOCH, gid, parent, promoted, created, round_index),
    )


def _exp(
    conn: sqlite3.Connection,
    gid: str,
    core_idea: str | None,
    decision: str | None,
    *,
    rejection: str = "",
    scalar: float | None = None,
    drift: float | None = None,
    pass_rate: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO experiments(epoch_id, generation_id, hypothesis_core_idea, "
        "hypothesis_why, hypothesis_json, tournament_decision, rejection_reason, "
        "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            EPOCH,
            gid,
            core_idea,
            "because",
            None,
            decision,
            rejection,
            scalar,
            drift,
            pass_rate,
            None,
        ),
    )


def _patch(conn: sqlite3.Connection, gid: str, patch_id: str, mutation_id: str) -> None:
    conn.execute(
        "INSERT INTO patches(patch_id, epoch_id, generation_id, mutation_id, op, rationale) "
        "VALUES(?,?,?,?,?,?)",
        (patch_id, EPOCH, gid, mutation_id, "replace", "r"),
    )


def _seed_index(workspace: Path) -> None:
    conn = sqlite3.connect(str(workspace / "index.db"))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) VALUES(?,?,?,?)",
            (EPOCH, "h", "2026-07-01", 1),
        )
        # v0 seed (round 0), then round 1 mints v2 and v1 — v2 CREATED FIRST, so
        # the round-1 tie must break on created_at, not on the id.
        _gen(conn, "v0", None, 1, "2026-07-01T00:00:00Z", 0)
        _gen(conn, "v2", "v0", 0, "2026-07-02T00:00:00Z", 1)
        _gen(conn, "v1", "v0", 1, "2026-07-02T00:01:00Z", 1)
        # ... and round 2 mints v3, which is still in flight (no decision).
        _gen(conn, "v3", "v1", None, "2026-07-03T00:00:00Z", 2)

        _exp(conn, "v0", "the seed", "promoted", scalar=None)
        _exp(
            conn,
            "v2",
            "widen the outline rubric",
            "rejected",
            rejection="insufficient improvement: 0.73 vs 0.72 (margin 0.0200)",
            scalar=0.014,
            drift=0.01,
            pass_rate=-0.25,
        )
        _exp(conn, "v1", "name the audience up front", "promoted", scalar=-0.08, drift=-0.06)
        _exp(conn, "v3", "cut the preamble", None)

        # v2 touched two sites; one of them TWICE (a re-edit within one proposal).
        _patch(conn, "v2", "p1", "prompt.system")
        _patch(conn, "v2", "p2", "prompt.system")
        _patch(conn, "v2", "p3", "agent.temperature")
        _patch(conn, "v1", "p4", "prompt.audience")
        conn.commit()
    finally:
        conn.close()


def _by_gen(ledger: dict) -> dict[str, dict]:
    return {r["generation_id"]: r for r in ledger["experiments"]}


# ---------------------------------------------------------------------------
# Shape + join correctness
# ---------------------------------------------------------------------------


def test_ledger_row_shape_and_join(tmp_path: Path) -> None:
    """One row per experiment, carrying idea + sites + verdict + deltas + round."""
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    ledger = build_experiments_ledger(_paths(tmp_path), EPOCH)

    assert ledger["epoch_id"] == EPOCH
    # a healthy read carries NO note (omit-when-default).
    assert "note" not in ledger

    rows = _by_gen(ledger)
    assert set(rows) == {"v0", "v1", "v2", "v3"}
    assert set(rows["v2"]) == {
        "generation_id",
        "round_index",
        "core_idea",
        "mutation_ids",
        "decision",
        "promoted",
        "rejection_reason",
        "scalar_score_delta",
        "drift_loss_delta",
        "pass_rate_delta",
    }

    v2 = rows["v2"]
    assert v2["core_idea"] == "widen the outline rubric"
    assert v2["decision"] == "rejected"
    assert v2["promoted"] is False
    assert v2["rejection_reason"].startswith("insufficient improvement")
    assert v2["scalar_score_delta"] == 0.014
    assert v2["pass_rate_delta"] == -0.25
    # the birth round comes from the GENERATION row, not the experiment.
    assert v2["round_index"] == 1
    # sites are DEDUPED (two patches against prompt.system name it once) and
    # scoped to their own experiment.
    assert v2["mutation_ids"] == ["agent.temperature", "prompt.system"]
    assert rows["v1"]["mutation_ids"] == ["prompt.audience"]
    # an experiment with no patch rows reads as an empty site list, not null.
    assert rows["v0"]["mutation_ids"] == []


def test_unsettled_experiment_keeps_its_row(tmp_path: Path) -> None:
    """An in-flight experiment degrades FIELD-BY-FIELD — it never vanishes."""
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    v3 = _by_gen(build_experiments_ledger(_paths(tmp_path), EPOCH))["v3"]

    assert v3["core_idea"] == "cut the preamble"
    assert v3["decision"] is None
    # tri-state: NEVER a default False on a candidate that has not raced.
    assert v3["promoted"] is None
    assert v3["rejection_reason"] is None
    assert v3["scalar_score_delta"] is None
    assert v3["round_index"] == 2


def test_rows_are_in_round_order(tmp_path: Path) -> None:
    """Round ascending; within a round, the index's own (created_at) order."""
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    ledger = build_experiments_ledger(_paths(tmp_path), EPOCH)

    assert [r["generation_id"] for r in ledger["experiments"]] == ["v0", "v2", "v1", "v3"]
    assert [r["round_index"] for r in ledger["experiments"]] == [0, 1, 1, 2]


def test_unstamped_round_sorts_last(tmp_path: Path) -> None:
    """A generation with no birth round trails the stamped sequence."""
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        _gen(conn, "v9", "v1", 0, "2026-06-01T00:00:00Z", None)  # earliest, unstamped
        _exp(conn, "v9", "a legacy experiment", "rejected")
        conn.commit()
    finally:
        conn.close()

    ledger = build_experiments_ledger(_paths(tmp_path), EPOCH)
    assert [r["generation_id"] for r in ledger["experiments"]][-1] == "v9"
    assert ledger["experiments"][-1]["round_index"] is None


def test_legacy_decision_spellings_are_canonicalised(tmp_path: Path) -> None:
    """The ledger speaks the ONE canonical verdict vocabulary."""
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        _gen(conn, "v4", "v1", 1, "2026-07-04T00:00:00Z", 3)
        _exp(conn, "v4", "an old record", "accept")
        conn.commit()
    finally:
        conn.close()

    v4 = _by_gen(build_experiments_ledger(_paths(tmp_path), EPOCH))["v4"]
    assert v4["decision"] == "promoted"
    assert v4["promoted"] is True


# ---------------------------------------------------------------------------
# Degrades
# ---------------------------------------------------------------------------


def test_absent_index_degrades_with_a_note(tmp_path: Path) -> None:
    """No index → an empty ledger + the honest reindex note, never a raise."""
    _seed_workspace(tmp_path)
    ledger = build_experiments_ledger(_paths(tmp_path), EPOCH)
    assert ledger == {
        "epoch_id": EPOCH,
        "experiments": [],
        "note": "index not built; run zicato reindex",
    }


def test_no_epoch_and_unknown_epoch_degrade_to_the_empty_ledger(tmp_path: Path) -> None:
    """No current epoch / an unknown id reads empty — the endpoint never 500s."""
    assert build_experiments_ledger(_paths(tmp_path)) == {"epoch_id": None, "experiments": []}
    _seed_workspace(tmp_path)
    _seed_index(tmp_path)
    assert build_experiments_ledger(_paths(tmp_path), "nope") == {
        "epoch_id": None,
        "experiments": [],
    }


def test_legacy_index_without_round_index_still_reads(tmp_path: Path) -> None:
    """A pre-v7 index (no ``round_index`` column) reads with null rounds.

    The regression this guards: naming ``round_index`` in the SELECT would
    fail the whole query on a legacy index and blank the ledger, rather than
    degrading the ONE field that is genuinely absent.
    """
    _seed_workspace(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        conn.execute(
            "CREATE TABLE generations (epoch_id TEXT, generation_id TEXT, "
            "parent_generation_id TEXT, promoted INTEGER, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE experiments (epoch_id TEXT, generation_id TEXT, "
            "hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT, "
            "tournament_decision TEXT, rejection_reason TEXT, scalar_score_delta REAL, "
            "drift_loss_delta REAL, pass_rate_delta REAL, outcome_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE patches (patch_id TEXT, epoch_id TEXT, generation_id TEXT, "
            "mutation_id TEXT, op TEXT, rationale TEXT)"
        )
        conn.execute(
            "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
            "promoted, created_at) VALUES(?,?,?,?,?)",
            (EPOCH, "v1", "v0", 1, "2026-07-02T00:00:00Z"),
        )
        _exp(conn, "v1", "a legacy idea", "promoted", scalar=-0.05)
        _patch(conn, "v1", "p1", "prompt.system")
        conn.commit()
    finally:
        conn.close()

    ledger = build_experiments_ledger(_paths(tmp_path), EPOCH)
    assert [r["generation_id"] for r in ledger["experiments"]] == ["v1"]
    assert ledger["experiments"][0]["round_index"] is None
    assert ledger["experiments"][0]["mutation_ids"] == ["prompt.system"]
    assert ledger["experiments"][0]["scalar_score_delta"] == -0.05


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_endpoint_serves_the_ledger_and_degrades_at_200(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_workspace(workspace)
    _seed_index(workspace)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    with TestClient(create_app(workspace, static_dir, read_only=True)) as client:
        res = client.get(f"/api/epoch/{EPOCH}/experiments-ledger")
        assert res.status_code == 200
        assert [r["generation_id"] for r in res.json()["experiments"]] == ["v0", "v2", "v1", "v3"]

        # a malformed id degrades at 200 like every other coordinate handler.
        bad = client.get("/api/epoch/not a real epoch!/experiments-ledger")
        assert bad.status_code == 200
        assert bad.json()["experiments"] == []

        # ... and so does a well-formed id for an epoch that does not exist.
        unknown = client.get("/api/epoch/e_nope/experiments-ledger")
        assert unknown.status_code == 200
        assert unknown.json() == {"epoch_id": None, "experiments": []}
