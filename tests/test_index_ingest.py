"""Tests for the zicato analytical index ingest path (:mod:`zicato.index.ingest`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import (
    Generation,
    MetricCount,
    ScoringWeights,
)
from zicato.core.workspace import events_jsonl_path, loss_profile_path
from zicato.epoch.journal import write_experiment
from zicato.epoch.lifecycle import new_epoch
from zicato.epoch.lineage import append_to_lineage
from zicato.index.ingest import (
    backfill_generations,
    ingest_experiment,
    ingest_run,
    rebuild_index,
)
from zicato.index.query import (
    experiments_for_epoch,
    generations_for_epoch,
    index_counts,
    loss_profiles_for_generation,
    metric_counts_for_run,
    runs_for_generation,
    tournaments_for_epoch,
)
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import (
    make_drift_count,
    make_experiment,
    make_loss_profile,
    make_outcome_record,
    make_patch,
    make_synthetic_events_jsonl,
)

# ---------------------------------------------------------------------------
# Synthetic-workspace builder
# ---------------------------------------------------------------------------


def _build_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Build a small synthetic workspace; return ``(workspace_root, epoch_id)``.

    Two epochs, each with a ``v0`` and ``v1`` generation. The first
    epoch's ``v1`` has runs with loss.json + events.jsonl and a
    resolved experiment; the second epoch is sparser. Returns the first
    epoch's id (the richer one) for tests that drill in.
    """
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", '
        '"wall_clock_budget_seconds": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", '
        '"wall_clock_budget_seconds": 60, "input": "ho"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")

    # --- Epoch one (rich) ---
    cfg_a = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid_a = cfg_a.id
    _seed_lineage(ws, eid_a)
    # Resolved experiment on v1.
    exp = make_experiment(
        epoch_id=eid_a,
        generation_id="v1",
        parent_generation_id="v0",
        outcome=make_outcome_record(),
    )
    write_experiment(ws, eid_a, "v1", exp)
    # Two runs per generation, with loss.json + events.jsonl.
    for gid in ("v0", "v1"):
        for entry_id in ("e1", "e2"):
            profile = make_loss_profile(
                run_id=f"run_{eid_a}_{gid}_{entry_id}",
                entry_id=entry_id,
                generation_id=gid,
                epoch_id=eid_a,
                drift_loss=2.5,
                runtime_ms=1234,
            )
            write_loss_profile(profile, loss_profile_path(ws, eid_a, gid, entry_id))
            make_synthetic_events_jsonl(
                events_jsonl_path(ws, eid_a, gid, entry_id),
                drift_events=[("off_topic", "warning"), ("off_topic", "warning")],
            )

    # --- Epoch two (sparse: just lineage, no runs) ---
    cfg_b = new_epoch(ws, "beta", board, rubric, ScoringWeights())
    _seed_lineage(ws, cfg_b.id)

    return ws, eid_a


def _dump_index(db: Path) -> str:
    """Serialise the whole SQLite index to a deterministic SQL dump.

    Used to assert that two rebuilds produce byte-identical indexes.
    ``iterdump`` emits schema + data as SQL text; two equal dumps mean
    two equal databases (row-for-row).
    """
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _seed_lineage(ws: Path, epoch_id: str) -> None:
    """Register a v0 (promoted seed) and v1 (promoted child) in lineage."""
    g0 = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, epoch_id, g0, None)
    append_to_lineage(ws, epoch_id, g1, "v0")


# ---------------------------------------------------------------------------
# rebuild_index
# ---------------------------------------------------------------------------


def test_rebuild_index_empty_workspace(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    db = rebuild_index(ws)
    assert db.exists()
    assert db == ws / "index.db"
    counts = index_counts(db)
    assert all(v == 0 for v in counts.values())


def test_rebuild_index_expected_row_counts(tmp_path: Path) -> None:
    ws, _ = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    counts = index_counts(db)
    # 2 epochs, 2 generations each.
    assert counts["epochs"] == 2
    assert counts["generations"] == 4
    # Only epoch alpha has runs: 2 generations x 2 entries = 4 runs.
    assert counts["runs"] == 4
    assert counts["loss_profiles"] == 4
    # One resolved experiment on alpha:v1, with one patch + one tournament.
    assert counts["experiments"] == 1
    assert counts["patches"] == 1
    assert counts["tournaments"] == 1
    # metric_counts is a pure projection of loss.json's metric surface. The
    # ``_build_workspace`` runs write loss.json with NO drift / metric
    # surface (and the index no longer re-tallies events.jsonl), so they
    # contribute zero metric_counts rows.
    assert counts["metric_counts"] == 0


def test_rebuild_index_custom_db_path(tmp_path: Path) -> None:
    ws, _ = _build_workspace(tmp_path)
    custom = tmp_path / "elsewhere" / "custom.db"
    db = rebuild_index(ws, custom)
    assert db == custom
    assert custom.exists()


def test_rebuild_index_is_idempotent(tmp_path: Path) -> None:
    ws, _ = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    first = index_counts(db)
    db_again = rebuild_index(ws)
    assert db_again == db
    assert index_counts(db) == first


def test_rebuild_index_records_experiment_outcome(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    exps = experiments_for_epoch(db, eid)
    assert len(exps) == 1
    row = exps[0]
    assert row["generation_id"] == "v1"
    assert row["tournament_decision"] == "promoted"
    # The hypothesis core idea is carried through verbatim.
    assert row["hypothesis_core_idea"]
    assert row["outcome_json"] is not None


def test_rebuild_index_records_tournament(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    tournaments = tournaments_for_epoch(db, eid)
    assert len(tournaments) == 1
    t = tournaments[0]
    assert t["parent_generation_id"] == "v0"
    assert t["child_generation_id"] == "v1"
    assert t["decision"] == "promoted"
    # Delta scalar carried from the OutcomeRecord.
    assert t["delta_scalar"] == pytest.approx(0.15)
    # v8: the crowning outcome did not set a champion_eval_mode, so it
    # round-trips as the OutcomeRecord default ("full").
    assert t["champion_eval_mode"] == "full"
    # v8: champion_run_ref is the best-effort pointer at the champion
    # (parent "v0") generation's workspace-relative directory.
    assert t["champion_run_ref"] == f"epochs/{eid}/generations/v0"


def test_rebuild_index_tournament_records_fast_champion_eval_mode(tmp_path: Path) -> None:
    # An outcome whose champion side was evaluated in fast (cached) mode
    # must round-trip champion_eval_mode == "fast" onto the tournament row.
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")

    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage(ws, eid)
    exp = make_experiment(
        epoch_id=eid,
        generation_id="v1",
        parent_generation_id="v0",
        outcome=make_outcome_record(champion_eval_mode="fast"),
    )
    write_experiment(ws, eid, "v1", exp)

    db = rebuild_index(ws)
    tournaments = tournaments_for_epoch(db, eid)
    assert len(tournaments) == 1
    t = tournaments[0]
    assert t["champion_eval_mode"] == "fast"
    assert t["champion_run_ref"] == f"epochs/{eid}/generations/v0"


def test_rebuild_index_metric_counts_are_pure_projection_of_loss_json(
    tmp_path: Path,
) -> None:
    # metric_counts is a PURE projection of loss.json's metric surface —
    # NOT re-derived from events.jsonl. A run whose loss.json carries a
    # drift surface yields exactly those rows; an events.jsonl that
    # disagrees (or a loss.json with no surface) is ignored.
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# r\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage(ws, eid)

    # loss.json carries a drift surface of off_topic/warning x2.
    profile = make_loss_profile(
        run_id="run_drift",
        entry_id="e1",
        generation_id="v0",
        epoch_id=eid,
        drift_counts=(make_drift_count("off_topic", "warning", 2),),
    )
    write_loss_profile(profile, loss_profile_path(ws, eid, "v0", "e1"))
    # An events.jsonl with DIFFERENT drift, to prove it is never consulted.
    make_synthetic_events_jsonl(
        events_jsonl_path(ws, eid, "v0", "e1"),
        drift_events=[("tool_error", "critical")],
    )

    db = rebuild_index(ws)
    rows = metric_counts_for_run(db, "run_drift")

    # The indexed metric_counts equal exactly the loss.json drift surface.
    expected = {
        (mc.name, mc.severity, mc.count)
        for mc in read_loss_profile(loss_profile_path(ws, eid, "v0", "e1")).unified_metrics()
    }
    actual = {(m["name"], m["severity"], m["count"]) for m in rows}
    assert actual == expected
    drift_rows = [m for m in rows if m["namespace"] == "drift"]
    assert len(drift_rows) == 1
    assert drift_rows[0]["name"] == "drift:off_topic"
    assert drift_rows[0]["severity"] == "warning"
    assert drift_rows[0]["count"] == pytest.approx(2.0)
    # The events-only tool_error drift is NOT indexed — events are ignored.
    assert all(m["name"] != "drift:tool_error" for m in rows)


def test_rebuild_index_no_metric_surface_yields_no_metric_counts(
    tmp_path: Path,
) -> None:
    # A loss.json with no drift / metric surface yields zero metric_counts
    # rows even when an events.jsonl with drift sits beside it — the index
    # is a pure projection and never re-tallies events.
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    run_id = f"run_{eid}_v0_e1"
    assert metric_counts_for_run(db, run_id) == []


def test_rebuild_index_dump_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Determinism guard: two rebuilds of the same workspace produce a
    # byte-identical SQL dump (every projection is stable across runs).
    # The cursor stamp is wall-clock, not a projection — pin it so the
    # comparison cannot straddle a second boundary.
    monkeypatch.setattr("zicato.index.ingest._now_iso", lambda: "2026-01-01T00:00:00Z")
    ws, _ = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    first = _dump_index(db)
    rebuild_index(ws)
    assert _dump_index(db) == first


def test_rebuild_index_metric_counts_from_loss_profile_surface(
    tmp_path: Path,
) -> None:
    # A loss profile that already carries its own metric_counts (the
    # realistic reducer output) must have those rows indexed verbatim,
    # not re-derived from events.
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# r\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage(ws, eid)
    profile = make_loss_profile(
        run_id="run_with_metrics",
        entry_id="e1",
        generation_id="v0",
        epoch_id=eid,
        drift_counts=(make_drift_count("tool_error", "critical", 1),),
        metric_counts=(
            MetricCount(name="drift:tool_error", severity="critical", count=1.0),
            MetricCount(name="cost:llm_calls", severity="", count=7.0),
            MetricCount(name="output:chars", severity="", count=512.0),
        ),
    )
    write_loss_profile(profile, loss_profile_path(ws, eid, "v0", "e1"))
    db = rebuild_index(ws)
    metrics = {m["name"]: m for m in metric_counts_for_run(db, "run_with_metrics")}
    assert metrics["drift:tool_error"]["count"] == pytest.approx(1.0)
    assert metrics["drift:tool_error"]["namespace"] == "drift"
    assert metrics["cost:llm_calls"]["count"] == pytest.approx(7.0)
    assert metrics["cost:llm_calls"]["namespace"] == "cost"
    assert metrics["output:chars"]["count"] == pytest.approx(512.0)


def test_rebuild_index_loss_profile_round_trips(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    profiles = loss_profiles_for_generation(db, eid, "v1")
    assert len(profiles) == 2
    p = profiles[0]
    assert p["drift_loss"] == pytest.approx(2.5)
    assert p["runtime_ms"] == 1234
    # The full LossProfile JSON is preserved for consumers that need it.
    assert p["loss_json"]


def test_rebuild_index_run_rows_carry_lineage(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    runs = runs_for_generation(db, eid, "v0")
    assert len(runs) == 2
    for r in runs:
        assert r["epoch_id"] == eid
        assert r["generation_id"] == "v0"
        assert r["entry_id"] in ("e1", "e2")
        assert r["runtime_ms"] == 1234


# ---------------------------------------------------------------------------
# ingest_run
# ---------------------------------------------------------------------------


def test_ingest_run_upserts_one_run(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    # Start from an empty index — ingest_run must create the db.
    db = ws / "index.db"
    assert not db.exists()
    ingest_run(ws, None, eid, "v0", "e1")
    assert db.exists()
    counts = index_counts(db)
    assert counts["runs"] == 1
    assert counts["loss_profiles"] == 1
    # The owning epoch + generation rows are created so the run is not
    # an orphan.
    assert counts["epochs"] == 1
    assert counts["generations"] == 1


def test_ingest_run_is_idempotent(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    ingest_run(ws, None, eid, "v0", "e1")
    db = ws / "index.db"
    first = index_counts(db)
    ingest_run(ws, None, eid, "v0", "e1")
    second = index_counts(db)
    assert first == second
    assert second["runs"] == 1
    assert second["loss_profiles"] == 1
    # metric_counts is delete-then-insert keyed on run_id — must not
    # accumulate duplicates on a repeat ingest.
    assert second["metric_counts"] == first["metric_counts"]


def test_ingest_run_skips_run_without_loss_json(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    # A run id with no loss.json on disk — ingest_run tolerates it.
    ingest_run(ws, None, eid, "v1", "no_such_entry")
    db = ws / "index.db"
    counts = index_counts(db)
    assert counts["runs"] == 0
    assert counts["loss_profiles"] == 0


# ---------------------------------------------------------------------------
# ingest_experiment
# ---------------------------------------------------------------------------


def test_ingest_experiment_upserts_one_experiment(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    ingest_experiment(ws, None, eid, "v1")
    db = ws / "index.db"
    counts = index_counts(db)
    assert counts["experiments"] == 1
    assert counts["patches"] == 1
    # The resolved outcome yields a tournament row.
    assert counts["tournaments"] == 1


def test_ingest_experiment_is_idempotent(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    ingest_experiment(ws, None, eid, "v1")
    db = ws / "index.db"
    first = index_counts(db)
    ingest_experiment(ws, None, eid, "v1")
    second = index_counts(db)
    assert first == second
    assert second["experiments"] == 1
    assert second["patches"] == 1
    assert second["tournaments"] == 1


def test_ingest_experiment_skips_generation_without_experiment(
    tmp_path: Path,
) -> None:
    ws, eid = _build_workspace(tmp_path)
    # v0 is a seed generation with no experiment.json.
    ingest_experiment(ws, None, eid, "v0")
    db = ws / "index.db"
    counts = index_counts(db)
    assert counts["experiments"] == 0
    assert counts["tournaments"] == 0
    # The owning epoch / generation rows are still written.
    assert counts["epochs"] == 1
    assert counts["generations"] == 1


def test_ingest_experiment_unresolved_writes_no_tournament(
    tmp_path: Path,
) -> None:
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# r\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage(ws, eid)
    # Experiment with outcome=None (proposed but not yet run).
    exp = make_experiment(epoch_id=eid, generation_id="v1", outcome=None)
    write_experiment(ws, eid, "v1", exp)
    ingest_experiment(ws, None, eid, "v1")
    db = ws / "index.db"
    counts = index_counts(db)
    assert counts["experiments"] == 1
    # No outcome -> no tournament row yet.
    assert counts["tournaments"] == 0


def test_incremental_ingest_matches_full_rebuild(tmp_path: Path) -> None:
    # Ingesting every run + experiment incrementally must land the same
    # rows a full rebuild would.
    ws, eid = _build_workspace(tmp_path)
    rebuilt = rebuild_index(ws, tmp_path / "rebuilt.db")
    rebuilt_counts = index_counts(rebuilt)

    incremental = tmp_path / "incremental.db"
    for gid in ("v0", "v1"):
        for entry_id in ("e1", "e2"):
            ingest_run(ws, incremental, eid, gid, entry_id)
        ingest_experiment(ws, incremental, eid, gid)
    inc_counts = index_counts(incremental)

    # The incremental path only touched epoch alpha; the rebuild also
    # indexed epoch beta. Compare the alpha-scoped tables.
    assert inc_counts["runs"] == rebuilt_counts["runs"]
    assert inc_counts["loss_profiles"] == rebuilt_counts["loss_profiles"]
    assert inc_counts["experiments"] == rebuilt_counts["experiments"]
    assert inc_counts["patches"] == rebuilt_counts["patches"]
    assert inc_counts["tournaments"] == rebuilt_counts["tournaments"]
    assert inc_counts["metric_counts"] == rebuilt_counts["metric_counts"]


# ---------------------------------------------------------------------------
# generations table — parent + promoted flags from the experiment
# ---------------------------------------------------------------------------


def _row_dict(row: Any) -> dict[str, Any]:
    """Convert a :class:`sqlite3.Row` to a plain dict for ergonomic asserts."""
    return {k: row[k] for k in row.keys()}


def _build_chain_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Build a workspace mirroring the t6 chain: v0 seed, v1 promoted, v2 rejected, v3 promoted.

    The lineage row for v0 is the only one seeded ahead of the
    per-generation ingests — v1/v2/v3 land via ``ingest_experiment``
    BEFORE ``append_to_lineage`` runs (the live orchestrator ordering
    that originally produced the broken rows). This exercises the
    fixed dual-write path: the experiment is the authoritative source
    of the generation's parent + promoted flag.
    """
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")

    cfg = new_epoch(ws, "presn", board, rubric, ScoringWeights())
    eid = cfg.id

    # The seed v0 is registered in lineage (no experiment); the dual-
    # write path for v0 only sees lineage. Calling ingest_experiment on
    # v0 is the orchestrator's idiom for the seed — the experiment file
    # is absent so it's a no-op for the experiments table, but the
    # owning epoch + generation rows still land.
    g0 = Generation(
        id="v0",
        epoch_id=eid,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-05-20T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, eid, g0, None)
    ingest_experiment(ws, None, eid, "v0")

    # v1 — promoted challenger of v0. Write the experiment FIRST,
    # ingest it, THEN append to lineage — mirrors orchestrator order.
    exp_v1 = make_experiment(
        epoch_id=eid,
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-05-20T00:01:00Z",
        patches=(make_patch(id="patch_v1"),),
        outcome=make_outcome_record(tournament_decision="promoted"),
    )
    write_experiment(ws, eid, "v1", exp_v1)
    ingest_experiment(ws, None, eid, "v1")
    g1 = Generation(
        id="v1",
        epoch_id=eid,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-05-20T00:02:00Z",
        promoted=True,
    )
    append_to_lineage(ws, eid, g1, "v0")

    # v2 — rejected challenger of v1.
    exp_v2 = make_experiment(
        epoch_id=eid,
        generation_id="v2",
        parent_generation_id="v1",
        proposed_at="2026-05-20T00:03:00Z",
        patches=(make_patch(id="patch_v2"),),
        outcome=make_outcome_record(
            tournament_decision="rejected",
            rejection_reason="scalar regressed",
            scalar_score_delta=-0.04,
        ),
    )
    write_experiment(ws, eid, "v2", exp_v2)
    ingest_experiment(ws, None, eid, "v2")
    g2 = Generation(
        id="v2",
        epoch_id=eid,
        parent_id="v1",
        snapshot_root=Path("/tmp/snap/v2"),
        created_at="2026-05-20T00:04:00Z",
        promoted=False,
    )
    append_to_lineage(ws, eid, g2, "v1")

    # v3 — promoted challenger of v1 (the surviving champion).
    exp_v3 = make_experiment(
        epoch_id=eid,
        generation_id="v3",
        parent_generation_id="v1",
        proposed_at="2026-05-20T00:05:00Z",
        patches=(make_patch(id="patch_v3"),),
        outcome=make_outcome_record(tournament_decision="promoted"),
    )
    write_experiment(ws, eid, "v3", exp_v3)
    ingest_experiment(ws, None, eid, "v3")
    g3 = Generation(
        id="v3",
        epoch_id=eid,
        parent_id="v1",
        snapshot_root=Path("/tmp/snap/v3"),
        created_at="2026-05-20T00:06:00Z",
        promoted=True,
    )
    append_to_lineage(ws, eid, g3, "v1")

    return ws, eid


def test_ingest_experiment_writes_parent_and_promoted_from_experiment(
    tmp_path: Path,
) -> None:
    """The live dual-write must carry parent + promoted, even when lineage is stale.

    Reproduces the t6 ordering: ``experiment.json`` is written and
    ingested BEFORE ``append_to_lineage`` runs, so the lineage-only
    read at dual-write time misses the row. The experiment itself is
    authoritative for the generation's parent + verdict, so the index
    must use it.
    """
    ws, eid = _build_chain_workspace(tmp_path)
    rows = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(ws / "index.db", eid)}

    # v0 — seeded via lineage only, no experiment.
    assert rows["v0"]["parent_generation_id"] is None
    assert rows["v0"]["promoted"] == 1

    # v1 — promoted challenger of v0.
    assert rows["v1"]["parent_generation_id"] == "v0"
    assert rows["v1"]["promoted"] == 1

    # v2 — rejected challenger of v1: parent is recorded, promoted=0.
    assert rows["v2"]["parent_generation_id"] == "v1"
    assert rows["v2"]["promoted"] == 0

    # v3 — promoted challenger of v1 (NOT v2, which was rejected).
    assert rows["v3"]["parent_generation_id"] == "v1"
    assert rows["v3"]["promoted"] == 1


def test_ingest_experiment_generation_row_is_idempotent(tmp_path: Path) -> None:
    """Re-running ingest_experiment must not flip the parent / promoted flag.

    The chain builder mirrors the orchestrator's order (ingest fires
    before lineage is appended), so the first ingest writes empty
    ``created_at`` values; a re-ingest after lineage has caught up
    fills them in. Compare only the two columns the experiment owns
    authoritatively — the parent + the verdict — which are exactly
    what was broken on t6.
    """
    ws, eid = _build_chain_workspace(tmp_path)

    def _critical_cols() -> dict[str, tuple[Any, int]]:
        return {
            r["generation_id"]: (r["parent_generation_id"], int(r["promoted"]))
            for r in generations_for_epoch(ws / "index.db", eid)
        }

    first = _critical_cols()
    for gid in ("v1", "v2", "v3"):
        ingest_experiment(ws, None, eid, gid)
    second = _critical_cols()
    assert first == second
    # And a third pass is still identical — true idempotency.
    for gid in ("v1", "v2", "v3"):
        ingest_experiment(ws, None, eid, gid)
    assert _critical_cols() == second


def test_ingest_experiment_unresolved_outcome_leaves_promoted_unset(
    tmp_path: Path,
) -> None:
    """A proposer-side experiment (outcome=None) writes promoted=0 and parent set."""
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# r\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    # Note: NO lineage row for v1 yet — the proposer-side write fires
    # before lineage is updated.
    exp = make_experiment(
        epoch_id=eid,
        generation_id="v1",
        parent_generation_id="v0",
        outcome=None,
    )
    write_experiment(ws, eid, "v1", exp)
    ingest_experiment(ws, None, eid, "v1")

    rows = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(ws / "index.db", eid)}
    assert rows["v1"]["parent_generation_id"] == "v0"
    assert rows["v1"]["promoted"] == 0


# ---------------------------------------------------------------------------
# backfill_generations
# ---------------------------------------------------------------------------


def _corrupt_generations_table(db_path: Path) -> None:
    """Mimic the t6 symptom: parent NULL on every row, promoted=0 except v0."""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE generations SET parent_generation_id = NULL")
        conn.execute(
            "UPDATE generations SET promoted = CASE WHEN generation_id = 'v0' " "THEN 1 ELSE 0 END"
        )
        conn.commit()
    finally:
        conn.close()


def test_backfill_repairs_broken_generations_table(tmp_path: Path) -> None:
    """Build the chain, corrupt the index rows, then run the backfill.

    The disk files (lineage.json + per-generation experiment.json) are
    left correct; only the SQLite rows are mangled to match the t6
    symptom. After ``backfill_generations`` the rows should agree with
    disk.
    """
    ws, eid = _build_chain_workspace(tmp_path)
    db = ws / "index.db"
    _corrupt_generations_table(db)
    # Sanity-check the corruption matched the t6 symptom.
    corrupted = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert corrupted["v0"]["promoted"] == 1
    assert corrupted["v1"]["promoted"] == 0
    assert corrupted["v3"]["promoted"] == 0
    for gid in ("v0", "v1", "v2", "v3"):
        assert corrupted[gid]["parent_generation_id"] is None

    result = backfill_generations(ws)
    assert result["scanned"] == 4
    # v0 was already correct (promoted=1, parent NULL is right for the
    # seed); v1, v2, v3 needed a rewrite.
    assert result["updated"] == 3

    repaired = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert repaired["v0"]["parent_generation_id"] is None
    assert repaired["v0"]["promoted"] == 1
    assert repaired["v1"]["parent_generation_id"] == "v0"
    assert repaired["v1"]["promoted"] == 1
    assert repaired["v2"]["parent_generation_id"] == "v1"
    assert repaired["v2"]["promoted"] == 0
    assert repaired["v3"]["parent_generation_id"] == "v1"
    assert repaired["v3"]["promoted"] == 1


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    """A second backfill against a healthy index is a no-op."""
    ws, eid = _build_chain_workspace(tmp_path)
    db = ws / "index.db"
    _corrupt_generations_table(db)
    backfill_generations(ws)
    second = backfill_generations(ws)
    assert second["updated"] == 0
    assert second["scanned"] == 4
    # The table still matches disk.
    rows = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert rows["v3"]["parent_generation_id"] == "v1"
    assert rows["v3"]["promoted"] == 1


def test_backfill_handles_missing_db(tmp_path: Path) -> None:
    """A workspace with no index.db yields a clean no-op result."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    result = backfill_generations(ws)
    assert result == {"updated": 0, "scanned": 0}


def test_backfill_recovers_champion_lineage_for_t6_chain(tmp_path: Path) -> None:
    """The dashboard's champion-lineage walker recovers v0 -> v1 -> v3 after backfill.

    Uses the canonical :func:`_champion_lineage` from the dashboard so
    the test exercises the exact same logic the gauntlet's API surface
    runs. Before the backfill (or before the writer fix) the rows lie
    and the spine collapses to just ``v0``. After the backfill the
    spine is the full champion chain.
    """
    from zicato.query import (  # noqa: PLC0415
        _champion_lineage,  # type: ignore[attr-defined]
    )

    ws, eid = _build_chain_workspace(tmp_path)
    db = ws / "index.db"
    _corrupt_generations_table(db)

    def _spine() -> list[str]:
        rows = generations_for_epoch(db, eid)
        return _champion_lineage([_row_dict(r) for r in rows])

    # Before the backfill the spine collapses (the symptom on t6).
    assert _spine() == ["v0"]
    backfill_generations(ws)
    # After the backfill the full champion chain is recoverable.
    assert _spine() == ["v0", "v1", "v3"]


# ---------------------------------------------------------------------------
# round_index — birth round of a generation (v7)
# ---------------------------------------------------------------------------


def _seed_lineage_with_rounds(ws: Path, epoch_id: str) -> None:
    """Seed v0 (round 0) + two challengers minted in rounds 0 and 1."""
    seed = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )  # genesis seed — round_index defaults to 0.
    c1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
        round_index=0,
    )
    c2 = Generation(
        id="v2",
        epoch_id=epoch_id,
        parent_id="v1",
        snapshot_root=Path("/tmp/snap/v2"),
        created_at="2026-01-03T00:00:00Z",
        promoted=True,
        round_index=1,
    )
    append_to_lineage(ws, epoch_id, seed, None)
    append_to_lineage(ws, epoch_id, c1, "v0")
    append_to_lineage(ws, epoch_id, c2, "v1")


def test_rebuild_index_round_index_round_trips(tmp_path: Path) -> None:
    """A generation's birth round survives a full rebuild + query."""
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage_with_rounds(ws, eid)

    db = rebuild_index(ws)
    rows = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert rows["v0"]["round_index"] == 0
    assert rows["v1"]["round_index"] == 0
    assert rows["v2"]["round_index"] == 1


def test_rebuild_index_legacy_generation_reads_null_round_index(tmp_path: Path) -> None:
    """A lineage row that predates the round_index field reads as null.

    Simulates a legacy ``lineage.json`` whose generation dicts have no
    ``round_index`` key by writing the document directly, then rebuilding.
    The index column must be ``NULL`` (birth round unknown), not coerced
    to 0.
    """
    import json

    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id

    # Overwrite lineage.json with legacy-shaped rows (no round_index key).
    lineage_path = ws / "lineage.json"
    legacy = {
        "epochs": [
            {
                "id": eid,
                "name": "alpha",
                "started_at": "2026-01-01T00:00:00Z",
                "closed_at": "",
                "v0_parent": None,
                "generations": [
                    {
                        "id": "v0",
                        "parent_id": None,
                        "promoted": True,
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "v1",
                        "parent_id": "v0",
                        "promoted": True,
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ],
            }
        ]
    }
    lineage_path.write_text(json.dumps(legacy), encoding="utf-8")

    db = rebuild_index(ws)
    rows = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert rows["v0"]["round_index"] is None
    assert rows["v1"]["round_index"] is None


def test_backfill_fills_round_index_for_legacy_row(tmp_path: Path) -> None:
    """backfill_generations gains round_index once lineage carries it.

    A pre-v7 row has round_index NULL; after lineage records the birth
    round, the backfill writes it through (and is then idempotent).
    """
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id
    _seed_lineage_with_rounds(ws, eid)
    db = rebuild_index(ws)

    # Null out the round_index column to mimic a v6-era row.
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE generations SET round_index = NULL")
        conn.commit()
    finally:
        conn.close()
    before = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert before["v2"]["round_index"] is None

    backfill_generations(ws)
    after = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert after["v1"]["round_index"] == 0
    assert after["v2"]["round_index"] == 1
    # Idempotent: a second pass changes nothing for round_index.
    backfill_generations(ws)
    again = {r["generation_id"]: _row_dict(r) for r in generations_for_epoch(db, eid)}
    assert again["v2"]["round_index"] == 1
