"""Index v11 — the board-reflection projection (reflections + judge_scorecards).

The additive migration (v10 → v11 in place + a fresh build both carry the two
new tables), the finalize-time + rebuild-time upsert writers, tolerant readers
(``IndexNotBuiltError`` ⇒ ``[]`` / ``None``), and the reindex walk discovering a
reflection directory. The generations ``elo`` / ``elo_games`` stubs are never
touched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zicato.core.workspace import (
    reflection_dir,
    reflection_findings_path,
    reflection_plan_path,
    reflection_scorecards_path,
)
from zicato.index import query as iq
from zicato.index import schema
from zicato.index.ingest import ingest_reflection, rebuild_index

REFL = "refl-20260701000000-abcd1234"
EPOCH = "epoch-1"


def _write_reflection_files(
    workspace: Path,
    *,
    reflection_id: str = REFL,
    epoch_id: str = EPOCH,
    mode: str = "active",
    executed: bool = True,
    scorecards: list[dict] | None = None,
    findings: list[dict] | None = None,
    summary: dict | None = None,
) -> None:
    plan_path = reflection_plan_path(workspace, epoch_id, reflection_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "reflection_id": reflection_id,
                "epoch_id": epoch_id,
                "candidates": ["v0", "v1"],
                "entries": ["entryA"],
                "replicates": 3,
                "adjudicator_model": "meta",
                "checks": ["judge-audit"],
                "mode": mode,
                "pre_registered": False,
                "executed": executed,
                "created_at": "2026-07-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    reflection_scorecards_path(workspace, epoch_id, reflection_id).write_text(
        json.dumps({"reflection_id": reflection_id, "scorecards": scorecards or []}),
        encoding="utf-8",
    )
    reflection_findings_path(workspace, epoch_id, reflection_id).write_text(
        json.dumps({"reflection_id": reflection_id, "findings": findings or []}),
        encoding="utf-8",
    )
    if summary is not None:
        (reflection_dir(workspace, epoch_id, reflection_id) / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )


def _card(name: str, **kw: object) -> dict:
    base = {
        "judge_name": name,
        "tp": 2,
        "fp": 1,
        "fn": 0,
        "tn": 3,
        "ambiguous": 0,
        "precision": 0.667,
        "recall": 1.0,
        "f1": 0.8,
        "severity_accuracy": 1.0,
        "disagreement_rate": 0.0,
        "self_consistency_kappa": 0.9,
        "redundant_with": [],
        "exercised": True,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Migration — fresh build AND in-place v10 -> v11
# ---------------------------------------------------------------------------


def test_fresh_build_carries_v11_tables() -> None:
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    # v11 introduced the tables; the build is now stamped at the CURRENT
    # version (>= 11 — v12 added the elo_se column on top).
    assert schema.read_schema_version(conn) >= 11
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reflections", "judge_scorecards"} <= tables


def test_in_place_migrate_v10_to_v11_adds_tables() -> None:
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    # Simulate a v10 database: drop the v11 tables and re-stamp the version.
    conn.execute("DROP TABLE reflections")
    conn.execute("DROP TABLE judge_scorecards")
    conn.execute("PRAGMA user_version = 10")
    assert schema.read_schema_version(conn) == 10
    schema.apply_schema(conn)  # in-place migrate (carries through to current)
    assert schema.read_schema_version(conn) >= 11
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reflections", "judge_scorecards"} <= tables


def test_migration_leaves_elo_stub_columns_untouched() -> None:
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(generations)")}
    assert {"elo", "elo_games"} <= cols


# ---------------------------------------------------------------------------
# Upsert / read round-trips
# ---------------------------------------------------------------------------


def test_ingest_reflection_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _write_reflection_files(
        workspace,
        scorecards=[_card("j1"), _card("j2", fp=0, precision=1.0)],
        findings=[{"finding_id": "f1"}, {"finding_id": "f2"}],
        summary={"noise_floor_max_abs_delta": 0.05, "decision_flip_p": 0.12},
    )
    ingest_reflection(workspace, None, EPOCH, REFL)
    db = workspace / "index.db"

    row = iq.reflection_row(db, REFL)
    assert row is not None
    assert row["epoch_id"] == EPOCH
    assert row["mode"] == "active"
    assert bool(row["executed"]) is True
    assert row["n_findings"] == 2
    assert row["n_judges"] == 2
    assert abs(row["noise_floor_max_abs_delta"] - 0.05) < 1e-9
    assert abs(row["decision_flip_p"] - 0.12) < 1e-9
    verdicts = json.loads(row["verdict_counts_json"])
    assert verdicts["tp"] == 4  # 2 + 2
    assert verdicts["fp"] == 1  # 1 + 0

    epoch_rows = iq.reflections_for_epoch(db, EPOCH)
    assert [r["reflection_id"] for r in epoch_rows] == [REFL]

    cards = iq.judge_scorecards_for_reflection(db, REFL)
    assert [c["judge_name"] for c in cards] == ["j1", "j2"]
    assert cards[0]["tp"] == 2
    assert abs(cards[0]["kappa"] - 0.9) < 1e-9
    assert bool(cards[0]["exercised"]) is True


def test_scorecards_upsert_is_delete_then_insert(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _write_reflection_files(workspace, scorecards=[_card("j1"), _card("j2")])
    ingest_reflection(workspace, None, EPOCH, REFL)
    db = workspace / "index.db"
    assert len(iq.judge_scorecards_for_reflection(db, REFL)) == 2

    # Shrink the scorecard set on disk and re-ingest — no stale row survives.
    _write_reflection_files(workspace, scorecards=[_card("j1")])
    ingest_reflection(workspace, None, EPOCH, REFL)
    cards = iq.judge_scorecards_for_reflection(db, REFL)
    assert [c["judge_name"] for c in cards] == ["j1"]


def test_passive_reflection_no_scorecards_still_projects_a_row(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _write_reflection_files(workspace, mode="passive", scorecards=[], findings=[])
    ingest_reflection(workspace, None, EPOCH, REFL)
    db = workspace / "index.db"
    row = iq.reflection_row(db, REFL)
    assert row is not None
    assert row["mode"] == "passive"
    assert row["n_judges"] == 0
    assert iq.judge_scorecards_for_reflection(db, REFL) == []


# ---------------------------------------------------------------------------
# Rebuild walk discovers a reflection dir
# ---------------------------------------------------------------------------


def test_reindex_discovers_reflection_dir(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    # A minimal epoch so the rebuild walk visits the epoch (lineage + config).
    (workspace / "epochs" / EPOCH).mkdir(parents=True)
    (workspace / "epochs" / EPOCH / "config.json").write_text(
        json.dumps({"id": EPOCH, "name": EPOCH, "created_at": "", "closed": False}),
        encoding="utf-8",
    )
    (workspace / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": EPOCH, "generations": []}]}), encoding="utf-8"
    )
    _write_reflection_files(
        workspace,
        scorecards=[_card("j1")],
        findings=[{"finding_id": "f1"}],
        summary={"noise_floor_max_abs_delta": 0.03, "decision_flip_p": 0.0},
    )
    db = rebuild_index(workspace)
    row = iq.reflection_row(db, REFL)
    assert row is not None
    assert row["n_findings"] == 1
    assert [c["judge_name"] for c in iq.judge_scorecards_for_reflection(db, REFL)] == ["j1"]


# ---------------------------------------------------------------------------
# Tolerant readers — no index / no table ⇒ empty, never raise
# ---------------------------------------------------------------------------


def test_readers_tolerate_missing_index(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "index.db"
    assert iq.reflections_for_epoch(missing, EPOCH) == []
    assert iq.reflection_row(missing, REFL) is None
    assert iq.judge_scorecards_for_reflection(missing, REFL) == []


def test_readers_tolerate_preexisting_index_without_v11_tables(tmp_path: Path) -> None:
    # A v10-shaped index with no reflections table: readers degrade, not raise.
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    schema.apply_schema(conn)
    conn.execute("DROP TABLE reflections")
    conn.execute("DROP TABLE judge_scorecards")
    conn.commit()
    conn.close()
    assert iq.reflections_for_epoch(db, EPOCH) == []
    assert iq.reflection_row(db, REFL) is None
    assert iq.judge_scorecards_for_reflection(db, REFL) == []
