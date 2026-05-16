"""Tests for the zicato analytical index ingest path (:mod:`zicato.index.ingest`)."""

from __future__ import annotations

from pathlib import Path

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
from zicato.index.ingest import ingest_experiment, ingest_run, rebuild_index
from zicato.index.query import (
    experiments_for_epoch,
    index_counts,
    loss_profiles_for_generation,
    metric_counts_for_run,
    runs_for_generation,
    tournaments_for_epoch,
)
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import (
    make_drift_count,
    make_experiment,
    make_loss_profile,
    make_outcome_record,
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
    # Each run has 2 off_topic/warning drift events folded into one
    # drift:off_topic metric row.
    assert counts["metric_counts"] == 4


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


def test_rebuild_index_metric_counts_from_drift_events(tmp_path: Path) -> None:
    ws, eid = _build_workspace(tmp_path)
    db = rebuild_index(ws)
    run_id = f"run_{eid}_v0_e1"
    metrics = metric_counts_for_run(db, run_id)
    drift_rows = [m for m in metrics if m["namespace"] == "drift"]
    assert len(drift_rows) == 1
    assert drift_rows[0]["name"] == "drift:off_topic"
    assert drift_rows[0]["severity"] == "warning"
    # Two synthetic off_topic/warning events were folded into the count.
    assert drift_rows[0]["count"] == pytest.approx(2.0)


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
