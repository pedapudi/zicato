"""The self-healing analytical index (ANALYTICAL-INDEX.md §5).

Three mechanisms, pinned here:

* **M1** — :func:`ensure_index` builds an absent / older-schema / unreadable
  index and leaves a current one alone, always through a temp-then-rename so
  a FAILED build cannot destroy the database it was meant to repair.
* **M2** — the v14 ``ingest_cursors`` table, per-epoch divergence detection
  against cheap workspace signals, and an incremental :func:`heal_index` that
  converges with a from-scratch rebuild.
* **M3** — the routine wiring: the ``evolve``-start preflight line and the
  dashboard's skip-under-lock read path.

The determinism pin (`test_heal_converges_with_a_rebuild*`) is the anchor for
the whole design: an automatic heal is only safe if it cannot produce an index
that a rebuild would not have produced.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from zicato.index.ingest import (
    _epoch_signals,
    _walk_epochs,
    ensure_index,
    heal_index,
    ingest_experiment,
    rebuild_index,
    validate_index,
)
from zicato.index.schema import SCHEMA_VERSION, IndexSchemaNewerError, apply_schema

# The one cell outside the convergence pin: a wall clock, normalised exactly as
# the REINDEX-DUMP parity gate already normalises every ISO timestamp it dumps.
_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")


# ---------------------------------------------------------------------------
# Workspace fixtures — hand-built so the signals are exactly what we set
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _experiment(epoch_id: str, generation_id: str, parent: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "parent_generation_id": parent,
        "proposed_at": "2026-01-01T00:00:00Z",
        "hypothesis": {"core_idea": f"idea for {generation_id}", "why": "because"},
        "patches": [],
        "outcome": None,
    }


def _make_workspace(root: Path, epoch_ids: tuple[str, ...] = ("e1",)) -> Path:
    """A minimal but real workspace: config.json + lineage.json + generations.

    The epoch configs are deliberately loadable by
    :func:`zicato.epoch.lifecycle.load_epoch`, so the walk takes its
    config-bearing branch rather than degrading to the lineage-only one.
    """
    ws = root / ".zicato"
    lineage_epochs = []
    for epoch_id in epoch_ids:
        # A config the canonical loader actually accepts — otherwise the
        # epoch falls through to the thin lineage-only branch and the
        # config-bearing half of the walk is never exercised.
        _write_json(
            ws / "epochs" / epoch_id / "config.json",
            {
                "id": epoch_id,
                "name": epoch_id,
                "created_at": "2026-01-01T00:00:00Z",
                "board_path": "board.jsonl",
                "brief_path": "brief.md",
                "scoring": {},
                "closed": False,
                "contract_hash": f"hash-of-{epoch_id}",
                "goal": f"goal of {epoch_id}",
            },
        )
        (ws / "epochs" / epoch_id / "generations" / "v0").mkdir(parents=True, exist_ok=True)
        (ws / "epochs" / epoch_id / "generations" / "v1").mkdir(parents=True, exist_ok=True)
        _write_json(
            ws / "epochs" / epoch_id / "generations" / "v1" / "experiment.json",
            _experiment(epoch_id, "v1", "v0"),
        )
        lineage_epochs.append(
            {
                "id": epoch_id,
                "started_at": "2026-01-01T00:00:00Z",
                "v0_parent": None,
                "generations": [
                    {
                        "id": "v0",
                        "parent_id": None,
                        "promoted": True,
                        "created_at": "2026-01-01T00:00:00Z",
                        "round_index": 0,
                    },
                    {
                        "id": "v1",
                        "parent_id": "v0",
                        "promoted": False,
                        "created_at": "2026-01-01T00:01:00Z",
                        "round_index": 1,
                    },
                ],
            }
        )
    _write_json(ws / "lineage.json", {"epochs": lineage_epochs})
    return ws


def _dump(db_path: Path, *, sort_rows: bool = False) -> list[str]:
    """The database's SQL dump, with the observational timestamp normalised.

    ``sort_rows`` compares dump CONTENT rather than rowid order — the right
    comparison when a heal re-inserts one epoch of several into non-empty
    tables (§5.2). The single-epoch case needs no sorting and is asserted raw.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        lines = [_ISO_TS.sub("<TS>", line) for line in conn.iterdump()]
    finally:
        conn.close()
    return sorted(lines) if sort_rows else lines


def _table_rows(db_path: Path, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return list(conn.execute(f"SELECT * FROM {table}"))  # noqa: S608 — test-local name
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# M1 — auto-build, and the temp-then-rename atomicity that makes it safe
# ---------------------------------------------------------------------------


def test_the_fixture_exercises_the_config_bearing_walk(tmp_path: Path) -> None:
    """Guard the guard: a config the loader rejects would silently degrade
    every test below to the thin lineage-only branch."""
    ws = _make_workspace(tmp_path)
    walk = _walk_epochs(ws)
    assert [item.epoch_id for item in walk] == ["e1"]
    assert walk[0].config is not None
    assert walk[0].lineage_entry is not None


def test_an_absent_index_is_built_on_ensure(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    actions: list[str] = []

    db = ensure_index(ws, action_out=actions)

    assert actions == ["built:absent"]
    assert db.exists()
    # The goal comes off config.json — proof the config branch ran.
    assert _table_rows(db, "epochs")[0][4] == "goal of e1"


def test_an_older_schema_index_is_rebuilt_not_migrated(tmp_path: Path) -> None:
    """Whole-table additions need a backfill an in-place ALTER cannot give."""
    ws = _make_workspace(tmp_path)
    db = ws / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    # Stamp it back to a prior generation with no rows: the older writer had
    # nothing in the tables that landed after it.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()

    actions: list[str] = []
    ensure_index(ws, action_out=actions)

    assert actions == ["built:stale-schema"]
    # The rebuild BACKFILLED — an in-place migration would have left these empty.
    assert {r[0] for r in _table_rows(db, "epochs")} == {"e1"}
    assert len(_table_rows(db, "generations")) == 2


def test_a_current_index_is_left_alone(tmp_path: Path) -> None:
    """An equal-version database is not M1's business — content drift is M2's."""
    ws = _make_workspace(tmp_path)
    db = ensure_index(ws)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM generations")
    conn.commit()
    conn.close()

    actions: list[str] = []
    ensure_index(ws, action_out=actions)

    assert actions == ["present"]
    assert _table_rows(db, "generations") == []


def test_an_unreadable_index_is_rebuilt(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    db = ws / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a sqlite database at all")

    actions: list[str] = []
    ensure_index(ws, action_out=actions)

    assert actions == ["built:unreadable"]
    assert len(_table_rows(db, "generations")) == 2


def test_a_newer_index_raises_rather_than_being_deleted(tmp_path: Path) -> None:
    """Auto-deleting a newer database is forbidden — the recovery is the operator's."""
    ws = _make_workspace(tmp_path)
    db = ws / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    conn.execute("INSERT INTO epochs(epoch_id) VALUES('from-the-future')")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(IndexSchemaNewerError, match="newer than this build"):
        ensure_index(ws)

    # Untouched: the row the newer writer left is still there.
    assert {r[0] for r in _table_rows(db, "epochs")} == {"from-the-future"}


def test_a_failed_build_leaves_the_existing_index_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temp-then-rename: the defect class the old unlink-first shape created.

    Building in place meant any mid-build failure left a schema-only file with
    every table empty — along the very path an operator runs to RECOVER a bad
    index. Here the old database survives byte-identical and stays readable.
    """
    ws = _make_workspace(tmp_path)
    db = ensure_index(ws)
    before_bytes = db.read_bytes()
    before_rows = _table_rows(db, "generations")
    assert before_rows

    import zicato.index.ingest as ingest_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk full halfway through the walk")

    monkeypatch.setattr(ingest_mod, "_rebuild_all", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        rebuild_index(ws)

    assert db.read_bytes() == before_bytes
    assert _table_rows(db, "generations") == before_rows
    # And no scratch file is left behind for the next build to inherit.
    assert not (ws / "index.db.tmp").exists()


def test_a_failed_ensure_build_leaves_no_index_and_no_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_workspace(tmp_path)
    import zicato.index.ingest as ingest_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr(ingest_mod, "_rebuild_all", _boom)

    with pytest.raises(RuntimeError, match="nope"):
        ensure_index(ws)

    assert not (ws / "index.db").exists()
    # No scratch of ANY name — the path is per-build, so naming the old fixed
    # one here would pass without checking anything.
    assert list(ws.glob("index.db*.tmp")) == []


def test_concurrent_builders_cannot_publish_each_others_half_built_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unserialised builders must not corrupt the index they repair.

    Evolve builds under the workspace lock, but the dashboard's build only
    SKIPS when it OBSERVES a held lock — two dashboards, or one that lost the
    read/build window to a starting evolve, both build with nothing between
    them. On a shared scratch path that is destructive rather than merely
    wasteful: the second builder's unlink removes the inode the first is
    still writing into, the first's rename then publishes whatever now sits
    at the path, and the sidecar unlink that follows deletes the WAL holding
    the rest of it. The observed result was a valid, EMPTY ``index.db``
    (``user_version=0``, zero tables) installed by the healing path itself,
    no exception raised anywhere.

    The barrier guarantees the two builds are genuinely inside their build
    windows at the same time, which is the only condition the defect needs.
    """
    import threading

    import zicato.index.ingest as ingest_mod

    ws = _make_workspace(tmp_path, epoch_ids=("e1", "e2"))
    real_rebuild_all = ingest_mod._rebuild_all
    # Both builders have created their scratch file and are inside the walk
    # before either is allowed to finish.
    barrier = threading.Barrier(2, timeout=60)

    def _synchronised(conn: object, workspace_root: Path) -> None:
        barrier.wait()
        real_rebuild_all(conn, workspace_root)

    monkeypatch.setattr(ingest_mod, "_rebuild_all", _synchronised)

    failures: list[BaseException] = []

    def _build() -> None:
        try:
            ensure_index(ws)
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=_build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    # Whichever build won the rename, it published a COMPLETE database.
    conn = sqlite3.connect(str(ws / "index.db"))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        epochs = {row[0] for row in conn.execute("SELECT epoch_id FROM epochs").fetchall()}
    finally:
        conn.close()
    assert epochs == {"e1", "e2"}
    assert list(ws.glob("index.db*.tmp")) == []


def test_the_outgoing_wal_is_cleared_before_the_rename_not_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign WAL beside the published index is REPLAYED, not ignored.

    SQLite validates WAL frames with an internal checksum chain seeded from
    the WAL header's own salts — nothing ties them to the main file — so a
    complete WAL from the database that used to be at this path is accepted
    and recovered over the one that just replaced it, page 1 included. The
    published index silently becomes the OLD index again: right
    ``integrity_check``, wrong ``user_version``, wrong tables, and no
    exception raised anywhere.

    Clearing the sidecars AFTER the rename leaves a WINDOW holding exactly
    that pair on disk, so this has to be observed from INSIDE the window —
    asserting on the finished call would pass either way, since the cleanup
    does happen, just too late. ``os.replace`` is made to die the instant it
    returns, which is the crash (SIGKILL, OOM, power loss) the ordering has
    to survive; a reader arriving in the window and CHECKPOINTING the
    foreign frames makes the same damage permanent without any crash.
    """
    import zicato.index.ingest as ingest_mod

    ws = _make_workspace(tmp_path)
    target = ws / "index.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    # An OLD index at the target path with a live, un-checkpointed WAL. The
    # sidecars are captured WHILE the connection is open, because ``close``
    # checkpoints them away — a crash does not, which is the state being
    # reconstructed. An empty WAL would prove nothing: it is the populated
    # frames that get replayed.
    wal_path = target.with_name("index.db-wal")
    shm_path = target.with_name("index.db-shm")
    stale = sqlite3.connect(str(target))
    stale.execute("PRAGMA journal_mode=WAL")
    stale.execute("PRAGMA user_version=1")
    stale.execute("CREATE TABLE stale_marker (x TEXT)")
    stale.executemany("INSERT INTO stale_marker VALUES (?)", [(f"s{i}",) for i in range(400)])
    stale.commit()
    assert wal_path.exists() and wal_path.stat().st_size > 0, "fixture needs a populated WAL"
    live_wal = wal_path.read_bytes()
    live_shm = shm_path.read_bytes() if shm_path.exists() else b""
    stale.close()
    wal_path.write_bytes(live_wal)
    if live_shm:
        shm_path.write_bytes(live_shm)

    # Crash the instant the rename lands — the window, reproduced exactly.
    real_replace = ingest_mod.os.replace

    def _replace_then_die(src: object, dst: object) -> None:
        real_replace(src, dst)
        raise KeyboardInterrupt("killed between the rename and the cleanup")

    monkeypatch.setattr(ingest_mod.os, "replace", _replace_then_die)
    with pytest.raises(KeyboardInterrupt):
        rebuild_index(ws)
    monkeypatch.undo()

    # The published database is the NEW one, and no foreign sidecar survives.
    assert not target.with_name("index.db-wal").exists()
    assert not target.with_name("index.db-shm").exists()
    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "stale_marker" not in names, "the outgoing database was recovered over the new one"
    assert "epochs" in names


def test_the_scratch_sweep_reclaims_dead_builds_and_spares_live_ones(
    tmp_path: Path,
) -> None:
    """Unique scratch names give up self-cleaning; the sweep buys it back.

    A build killed outright leaves a database-sized file that no later build
    will reuse the name of. Liveness is decided by the PID in the name — the
    same stale-owner test the workspace lock uses — because a sweep that
    could delete a CONCURRENT builder's scratch would reintroduce exactly the
    race the unique name exists to prevent.
    """
    import os

    from zicato.index.ingest import _sweep_stale_build_tmps

    ws = _make_workspace(tmp_path)
    target = ws / "index.db"

    dead = target.with_name("index.db.999999.aaaaaaaa.tmp")
    live = target.with_name(f"index.db.{os.getpid()}.bbbbbbbb.tmp")
    legacy = target.with_name("index.db.tmp")
    unrelated = target.with_name("index.db.notes.txt")
    unparseable = target.with_name("index.db.someone-elses.tmp")
    for path in (dead, live, legacy, unrelated, unparseable):
        path.write_bytes(b"scratch")
    # A dead build's WAL sidecars go with it.
    dead.with_name(dead.name + "-wal").write_bytes(b"wal")

    _sweep_stale_build_tmps(target)

    assert not dead.exists(), "a dead builder's scratch must be reclaimed"
    assert not dead.with_name(dead.name + "-wal").exists()
    assert not legacy.exists(), "the pre-unique fixed name is pure leftover"
    assert live.exists(), "a LIVE builder's scratch must never be touched"
    assert unrelated.exists(), "only scratch files are this sweep's business"
    assert unparseable.exists(), "an unrecognised name is left alone"


def test_rebuild_index_still_produces_a_from_scratch_database(tmp_path: Path) -> None:
    """The refactor onto temp-then-rename must not weaken ``zicato reindex``."""
    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO epochs(epoch_id) VALUES('ghost-from-a-prior-build')")
    conn.commit()
    conn.close()

    rebuild_index(ws)

    assert {r[0] for r in _table_rows(db, "epochs")} == {"e1"}


# ---------------------------------------------------------------------------
# M2 — cursors, divergence detection, and the incremental heal
# ---------------------------------------------------------------------------


def test_a_rebuild_writes_one_cursor_per_epoch(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, ("e1", "e2"))
    db = rebuild_index(ws)
    rows = {r[0]: r[1:5] for r in _table_rows(db, "ingest_cursors")}
    assert set(rows) == {"e1", "e2"}
    # (experiments, round_dirs, reflections, lineage_generations)
    assert rows["e1"] == (1, 0, 0, 2)


def test_a_fresh_index_reports_no_divergence(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, ("e1", "e2"))
    rebuild_index(ws)
    assert validate_index(ws) == ()
    assert heal_index(ws) == ()


def test_divergence_on_an_added_experiment(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, ("e1", "e2"))
    rebuild_index(ws)

    (ws / "epochs" / "e1" / "generations" / "v2").mkdir(parents=True)
    _write_json(
        ws / "epochs" / "e1" / "generations" / "v2" / "experiment.json",
        _experiment("e1", "v2", "v0"),
    )

    assert validate_index(ws) == ("e1",)
    assert heal_index(ws) == ("e1",)
    assert validate_index(ws) == ()
    db = ws / "index.db"
    assert {r[1] for r in _table_rows(db, "experiments")} == {"v1", "v2"}


def test_divergence_on_an_added_round_dir(tmp_path: Path) -> None:
    """The index projects no rounds table; the directory is a cheap advance signal."""
    ws = _make_workspace(tmp_path)
    rebuild_index(ws)

    (ws / "epochs" / "e1" / "rounds" / "1").mkdir(parents=True)

    assert validate_index(ws) == ("e1",)
    assert heal_index(ws) == ("e1",)
    assert validate_index(ws) == ()


def test_divergence_on_an_added_reflection(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    rebuild_index(ws)

    (ws / "epochs" / "e1" / "reflections" / "r1").mkdir(parents=True)

    assert validate_index(ws) == ("e1",)
    assert heal_index(ws) == ("e1",)
    assert validate_index(ws) == ()


def test_divergence_on_an_added_lineage_generation(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    rebuild_index(ws)

    lineage = json.loads((ws / "lineage.json").read_text(encoding="utf-8"))
    lineage["epochs"][0]["generations"].append(
        {"id": "v2", "parent_id": "v1", "promoted": False, "created_at": "t", "round_index": 2}
    )
    _write_json(ws / "lineage.json", lineage)

    assert validate_index(ws) == ("e1",)
    assert heal_index(ws) == ("e1",)
    assert validate_index(ws) == ()


def test_an_epoch_deleted_from_the_workspace_is_removed_from_the_index(
    tmp_path: Path,
) -> None:
    import shutil

    ws = _make_workspace(tmp_path, ("e1", "e2"))
    db = rebuild_index(ws)
    assert {r[0] for r in _table_rows(db, "epochs")} == {"e1", "e2"}

    shutil.rmtree(ws / "epochs" / "e2")
    lineage = json.loads((ws / "lineage.json").read_text(encoding="utf-8"))
    lineage["epochs"] = [e for e in lineage["epochs"] if e["id"] != "e2"]
    _write_json(ws / "lineage.json", lineage)

    assert validate_index(ws) == ("e2",)
    assert heal_index(ws) == ("e2",)
    assert {r[0] for r in _table_rows(db, "epochs")} == {"e1"}
    assert {r[0] for r in _table_rows(db, "ingest_cursors")} == {"e1"}
    assert {r[0] for r in _table_rows(db, "generations")} == {"e1"}


def test_the_epoch_scoped_delete_reaches_every_table(tmp_path: Path) -> None:
    """The four tables with no ``epoch_id`` of their own must still be cleared.

    ``metric_counts`` / ``judge_losses`` hang off ``runs`` and
    ``judge_scorecards`` off ``reflections``; a delete that ran AFTER their
    lookup table would match nothing and orphan every row of the epoch.
    """
    from zicato.index.ingest import _delete_epoch_rows

    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO runs(run_id, epoch_id) VALUES('v1--a', 'e1')")
    conn.execute("INSERT INTO metric_counts(run_id, name, count) VALUES('v1--a', 'drift:x', 1.0)")
    conn.execute("INSERT INTO judge_losses(run_id, judge_name) VALUES('v1--a', 'j')")
    conn.execute("INSERT INTO reflections(reflection_id, epoch_id) VALUES('r1', 'e1')")
    conn.execute("INSERT INTO judge_scorecards(reflection_id, judge_name) VALUES('r1', 'j')")
    conn.execute("INSERT INTO pareto_frontier(epoch_id, generation_id) VALUES('e1', 'v1')")
    conn.commit()

    _delete_epoch_rows(conn, "e1")
    conn.commit()

    for table in (
        "epochs",
        "generations",
        "experiments",
        "patches",
        "runs",
        "loss_profiles",
        "metric_counts",
        "judge_losses",
        "tournaments",
        "reflections",
        "judge_scorecards",
        "pareto_frontier",
        "ingest_cursors",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    conn.close()


def test_a_pre_v14_index_reads_as_wholly_diverged_then_heals(tmp_path: Path) -> None:
    """An empty cursor table is the correct conservative answer, and self-corrects."""
    ws = _make_workspace(tmp_path, ("e1", "e2"))
    db = rebuild_index(ws)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM ingest_cursors")
    conn.commit()
    conn.close()

    assert validate_index(ws) == ("e1", "e2")
    assert heal_index(ws) == ("e1", "e2")
    assert validate_index(ws) == ()
    assert heal_index(ws) == ()  # idempotent


def test_validate_tolerates_a_missing_database(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    assert validate_index(ws) == ()
    assert not (ws / "index.db").exists()


def test_the_dual_write_seams_advance_the_cursor(tmp_path: Path) -> None:
    """Without this, every dual-written round would read as divergence."""
    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)

    (ws / "epochs" / "e1" / "generations" / "v2").mkdir(parents=True)
    _write_json(
        ws / "epochs" / "e1" / "generations" / "v2" / "experiment.json",
        _experiment("e1", "v2", "v1"),
    )
    ingest_experiment(ws, db, "e1", "v2")

    assert validate_index(ws) == ()


def test_the_signals_are_directory_counts_not_parses(tmp_path: Path) -> None:
    """A generation directory with no experiment.json does not count as one."""
    ws = _make_workspace(tmp_path)
    walk = {item.epoch_id: item for item in _walk_epochs(ws)}
    assert _epoch_signals(ws, "e1", walk["e1"].lineage_entry) == (1, 0, 0, 2)

    (ws / "epochs" / "e1" / "generations" / "v9").mkdir(parents=True)
    assert _epoch_signals(ws, "e1", walk["e1"].lineage_entry) == (1, 0, 0, 2)


# ---------------------------------------------------------------------------
# The determinism pin — heal and rebuild must converge
# ---------------------------------------------------------------------------


def test_heal_converges_with_a_rebuild_byte_for_byte(tmp_path: Path) -> None:
    """Single epoch: the tables empty out, rowids restart, the dump is identical."""
    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)

    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM generations WHERE epoch_id = 'e1'")
    conn.execute("DELETE FROM experiments WHERE epoch_id = 'e1'")
    conn.execute("UPDATE ingest_cursors SET experiments_count = 99")
    conn.commit()
    conn.close()

    assert heal_index(ws) == ("e1",)

    fresh = tmp_path / "fresh.db"
    rebuild_index(ws, fresh)
    assert _dump(db) == _dump(fresh)


def test_heal_converges_with_a_rebuild_on_a_multi_epoch_workspace(tmp_path: Path) -> None:
    """Several epochs: content identity, since a partial re-insert moves rowids."""
    ws = _make_workspace(tmp_path, ("e1", "e2", "e3"))
    db = rebuild_index(ws)

    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM generations WHERE epoch_id = 'e2'")
    conn.execute("UPDATE ingest_cursors SET experiments_count = 42 WHERE epoch_id = 'e2'")
    conn.commit()
    conn.close()

    assert heal_index(ws) == ("e2",)

    fresh = tmp_path / "fresh.db"
    rebuild_index(ws, fresh)
    assert _dump(db, sort_rows=True) == _dump(fresh, sort_rows=True)


def test_heal_refolds_the_cross_epoch_elo_columns(tmp_path: Path) -> None:
    """The rating fold is cross-epoch; a per-epoch re-insert nulls it.

    Pinned separately from the dump comparison because it is the one part of
    convergence that a per-epoch heal does NOT get for free — dropping and
    re-inserting an epoch's generations clears ``elo`` / ``elo_games``, and
    only a whole-ledger re-fold restores what a rebuild would have written.
    """
    ws = _make_workspace(tmp_path)
    # A SETTLED experiment is what puts a duel in the match ledger for the
    # fold to rate — an unresolved one writes no tournaments row at all.
    settled = _experiment("e1", "v1", "v0")
    settled["outcome"] = {
        "tournament_decision": "promoted",
        "rejection_reason": None,
        "scalar_score_delta": 0.4,
        "drift_loss_delta": -0.1,
        "pass_rate_delta": 0.1,
        "ran_at": "2026-01-01T00:02:00Z",
    }
    _write_json(ws / "epochs" / "e1" / "generations" / "v1" / "experiment.json", settled)
    db = rebuild_index(ws)

    def _ratings(path: Path) -> dict[str, tuple[float | None, int | None]]:
        conn = sqlite3.connect(str(path))
        try:
            return {
                gid: (elo, games)
                for gid, elo, games in conn.execute(
                    "SELECT generation_id, elo, elo_games FROM generations"
                )
            }
        finally:
            conn.close()

    rated_before = _ratings(db)
    # The fixture must actually exercise the fold, or this test proves nothing.
    assert all(elo is not None for elo, _ in rated_before.values())

    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM generations WHERE epoch_id = 'e1'")
    conn.execute("UPDATE ingest_cursors SET experiments_count = 0")
    conn.commit()
    conn.close()

    heal_index(ws)

    assert _ratings(db) == rated_before


# ---------------------------------------------------------------------------
# M3 — the routine wiring
# ---------------------------------------------------------------------------


def test_the_evolve_preflight_reports_a_fresh_build(tmp_path: Path) -> None:
    from zicato.evolve.ingest import index_preflight

    ws = _make_workspace(tmp_path)
    assert index_preflight(ws) == "index: built fresh (absent)"


def test_the_evolve_preflight_reports_what_it_healed(tmp_path: Path) -> None:
    from zicato.evolve.ingest import index_preflight

    ws = _make_workspace(tmp_path, ("e1", "e2"))
    rebuild_index(ws)
    (ws / "epochs" / "e2" / "rounds" / "1").mkdir(parents=True)

    assert index_preflight(ws) == "index: healed epochs e2"
    assert index_preflight(ws) == "index: fresh"


def test_the_evolve_preflight_heals_the_proposers_experiment_memory(tmp_path: Path) -> None:
    """The loop-quality fix: a stale index silently thins the proposer's memory."""
    from zicato.evolve.ingest import _load_prior_experiments, index_preflight

    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)
    # Resolve v1's experiment so it becomes a settled prior experiment...
    settled = _experiment("e1", "v1", "v0")
    settled["outcome"] = {
        "tournament_decision": "rejected",
        "rejection_reason": "worse drift",
        "scalar_score_delta": -0.2,
        "drift_loss_delta": 0.1,
        "pass_rate_delta": 0.0,
        "ran_at": "2026-01-01T00:02:00Z",
    }
    _write_json(ws / "epochs" / "e1" / "generations" / "v1" / "experiment.json", settled)
    # ...and corrupt the index behind its back, as a crashed dual-write would.
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM experiments WHERE epoch_id = 'e1'")
    conn.execute("UPDATE ingest_cursors SET experiments_count = 0")
    conn.commit()
    conn.close()

    assert _load_prior_experiments(ws, "e1") == []

    index_preflight(ws)

    assert [p.core_idea for p in _load_prior_experiments(ws, "e1")] == ["idea for v1"]


def test_the_dashboard_startup_builds_an_absent_index(tmp_path: Path) -> None:
    from zicato.dashboard.server import _ensure_index_at_startup, _resolve_workspace

    ws = _make_workspace(tmp_path)
    _ensure_index_at_startup(_resolve_workspace(ws))
    assert (ws / "index.db").exists()


def test_the_dashboard_startup_skips_while_an_evolve_holds_the_lock(
    tmp_path: Path,
) -> None:
    """§5.3's concurrency rule: skip with a log line, retry-free."""
    from zicato.dashboard.server import _ensure_index_at_startup, _resolve_workspace
    from zicato.runtime.lock import acquire_workspace_lock

    ws = _make_workspace(tmp_path)
    acquire_workspace_lock(ws, "the-running-evolve")

    _ensure_index_at_startup(_resolve_workspace(ws))

    assert not (ws / "index.db").exists()


def test_the_dashboard_startup_skips_a_workspace_with_no_epochs(tmp_path: Path) -> None:
    """Graceful absence: a never-run workspace keeps its "not indexed" state."""
    from zicato.dashboard.server import _ensure_index_at_startup, _resolve_workspace

    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)

    _ensure_index_at_startup(_resolve_workspace(ws))

    assert not (ws / "index.db").exists()


def test_the_dashboard_startup_never_heals(tmp_path: Path) -> None:
    """Healing writes; a reader healing under a live dual-write is the contention case."""
    from zicato.dashboard.server import _ensure_index_at_startup, _resolve_workspace

    ws = _make_workspace(tmp_path)
    db = rebuild_index(ws)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM generations")
    conn.execute("UPDATE ingest_cursors SET experiments_count = 77")
    conn.commit()
    conn.close()

    _ensure_index_at_startup(_resolve_workspace(ws))

    assert _table_rows(db, "generations") == []
    assert validate_index(ws) == ("e1",)


def test_a_stale_lock_does_not_block_the_dashboard_build(tmp_path: Path) -> None:
    from zicato.dashboard.server import _ensure_index_at_startup, _resolve_workspace
    from zicato.runtime._storage import backend_for, lock_key

    ws = _make_workspace(tmp_path)
    (ws / "runtime").mkdir(parents=True, exist_ok=True)
    backend_for(ws).write_json(
        lock_key(),
        {
            "pid": 999_999,
            "instance_id": "dead",
            "acquired_at": "2026-01-01T00:00:00Z",
            "workspace_root": str(ws),
            "start_time": 1.0,
        },
    )

    _ensure_index_at_startup(_resolve_workspace(ws))

    assert (ws / "index.db").exists()
