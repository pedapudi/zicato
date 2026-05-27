"""Tests for the per-judge-loss promotion pipeline.

The per-judge weighted-loss attribution lives on
:attr:`zicato.core.types.LossProfile.per_judge_loss` (the reducer's
output), is persisted via :func:`zicato.telemetry.reducer.write_loss_profile`,
ingested into the analytical index's ``judge_losses`` table, and surfaced
in the analyzer's per-judge drift-attribution table. These tests cover
each link in that chain end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.repair_judge_losses import repair_judge_losses_cmd
from zicato.core.types import (
    BoardEntry,
    DriftCount,
    JudgeLoss,
    LossProfile,
    ScoringWeights,
)
from zicato.core.workspace import (
    loss_profile_path,
    scoring_path,
)
from zicato.epoch.lifecycle import new_epoch
from zicato.index.ingest import ingest_run, rebuild_index
from zicato.index.query import (
    judge_loss_trend,
    judge_losses_for_generation,
    judge_losses_for_run,
)
from zicato.index.schema import SCHEMA_VERSION, apply_schema
from zicato.telemetry.reducer import (
    compute_per_judge_loss,
    read_loss_profile,
    reduce_loss,
    write_loss_profile,
)

# ---------------------------------------------------------------------------
# Event fixture helpers
# ---------------------------------------------------------------------------


def _write_events_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, sort_keys=True) + "\n")


def _judgement_emitted(judge_name: str, *, severity: str = "warning", seq: int = 0) -> dict:
    return {
        "event_id": f"j{seq}",
        "run_id": "run-J",
        "sequence": seq,
        "judgement_emitted": {
            "judge_name": judge_name,
            "verdict_kind": "drift",
            "drift_kind": "custom",
            "severity": severity,
        },
    }


def _drift_detected(
    *,
    kind: str = "DRIFT_KIND_CUSTOM",
    severity: str = "DRIFT_SEVERITY_WARNING",
    seq: int = 0,
) -> dict:
    return {
        "event_id": f"d{seq}",
        "run_id": "run-J",
        "sequence": seq,
        "drift_detected": {"kind": kind, "severity": severity, "detail": ""},
    }


def _single_turn_entry(entry_id: str = "ent-1") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


# ---------------------------------------------------------------------------
# 1) Reducer aggregates per-judge from events with correct weighting
# ---------------------------------------------------------------------------


def test_reduce_loss_populates_per_judge_loss_with_correct_weighting(tmp_path: Path) -> None:
    """Two custom judges fire — per_judge_loss carries each with its
    raw_loss, weight, and weighted_loss.

    Verifies the reducer's promotion of per-judge attribution out of the
    events stream and through the LossProfile output. The aggregate
    drift_loss is left untouched — per_judge_loss is the breakdown, not
    a re-derivation — and the per-judge weighted_loss values sum to the
    custom-attributed portion of drift_loss exactly.
    """
    events = [
        _judgement_emitted("judge_a", severity="warning", seq=0),
        _drift_detected(severity="DRIFT_SEVERITY_WARNING", seq=1),
        _judgement_emitted("judge_b", severity="critical", seq=2),
        _drift_detected(severity="DRIFT_SEVERITY_CRITICAL", seq=3),
    ]
    p = tmp_path / "events.jsonl"
    _write_events_jsonl(p, events)
    weights = ScoringWeights(per_judge_weights={"judge_a": 2.0, "judge_b": 5.0})
    profile = reduce_loss(
        events_jsonl_path=p,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=weights,
    )
    by_name = {jl.judge_name: jl for jl in profile.per_judge_loss}
    assert set(by_name) == {"judge_a", "judge_b"}
    # judge_a: severity_weights["warning"] * count == 3.0 * 1.0
    assert by_name["judge_a"].raw_loss == pytest.approx(3.0)
    assert by_name["judge_a"].weight == pytest.approx(2.0)
    assert by_name["judge_a"].weighted_loss == pytest.approx(6.0)
    # judge_b: severity_weights["critical"] * count == 10.0 * 1.0
    assert by_name["judge_b"].raw_loss == pytest.approx(10.0)
    assert by_name["judge_b"].weight == pytest.approx(5.0)
    assert by_name["judge_b"].weighted_loss == pytest.approx(50.0)


def test_compute_per_judge_loss_unattributed_custom_drift_uses_default_weight() -> None:
    """A custom drift with no paired judgement scores at default_judge_weight
    under the empty-string judge_name bucket."""
    weights = ScoringWeights(default_judge_weight=2.5)
    # Bare "custom" kind (no `:<judge_name>` suffix) is the unattributed
    # bucket the reducer keeps for drifts that lacked a paired judgement.
    drift_counts = (DriftCount(kind="custom", severity="warning", count=2),)
    result = compute_per_judge_loss(drift_counts, weights)
    assert len(result) == 1
    only = result[0]
    assert only.judge_name == ""
    # severity_weights["warning"] == 3.0, count == 2 → raw_loss = 6.0
    assert only.raw_loss == pytest.approx(6.0)
    assert only.weight == pytest.approx(2.5)
    assert only.weighted_loss == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# 2) LossProfile round-trips per_judge_loss
# ---------------------------------------------------------------------------


def test_loss_profile_round_trips_per_judge_loss(tmp_path: Path) -> None:
    """A LossProfile written via write_loss_profile + read_loss_profile
    recovers the per_judge_loss tuple verbatim."""
    profile = LossProfile(
        run_id="run-RT",
        entry_id="ent-RT",
        generation_id="v1",
        epoch_id="ep1",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=42.0,
        pass_fail=None,
        per_judge_loss=(
            JudgeLoss(judge_name="alpha", raw_loss=1.5, weight=2.0, weighted_loss=3.0),
            JudgeLoss(judge_name="beta", raw_loss=7.0, weight=0.5, weighted_loss=3.5),
        ),
    )
    target = tmp_path / "loss.json"
    write_loss_profile(profile, target)
    # The JSON should carry the per_judge_loss array — sanity-check the
    # on-disk shape before re-reading.
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(raw.get("per_judge_loss"), list)
    assert {j["judge_name"] for j in raw["per_judge_loss"]} == {"alpha", "beta"}
    reloaded = read_loss_profile(target)
    assert reloaded.per_judge_loss == profile.per_judge_loss


# ---------------------------------------------------------------------------
# 3) Schema migration: SCHEMA_VERSION=2, judge_losses table created
# ---------------------------------------------------------------------------


def test_schema_v2_creates_judge_losses_table_and_index() -> None:
    """SCHEMA_VERSION is 2; apply_schema creates judge_losses + its index.

    The table carries (run_id, judge_name) as its composite PK and three
    numeric columns. The companion index idx_judge_losses_run keys on
    run_id for the per-run lookup the analyzer issues.
    """
    assert SCHEMA_VERSION == 2
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    # Table exists with the contracted columns.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(judge_losses)").fetchall()]
    assert cols == ["run_id", "judge_name", "weighted_loss", "raw_loss", "weight"]
    # Composite primary key.
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(judge_losses)").fetchall() if r[5]]
    assert set(pk_cols) == {"run_id", "judge_name"}
    # Index exists.
    idxs = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert "idx_judge_losses_run" in idxs
    conn.close()


# ---------------------------------------------------------------------------
# 4) Ingest populates judge_losses from loss.json
# ---------------------------------------------------------------------------


def _build_min_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A minimal workspace with one epoch + v0 generation directory."""
    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    return ws, cfg.id


def test_ingest_run_populates_judge_losses_from_loss_json(tmp_path: Path) -> None:
    """ingest_run writes a judge_losses row per JudgeLoss entry on the profile."""
    ws, epoch_id = _build_min_workspace(tmp_path)
    profile = LossProfile(
        run_id="run_ing",
        entry_id="e1",
        generation_id="v0",
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=10.0,
        pass_fail=None,
        per_judge_loss=(
            JudgeLoss(judge_name="quality", raw_loss=2.0, weight=3.0, weighted_loss=6.0),
            JudgeLoss(judge_name="schema", raw_loss=1.0, weight=4.0, weighted_loss=4.0),
        ),
    )
    write_loss_profile(profile, loss_profile_path(ws, epoch_id, "v0", "e1"))
    ingest_run(ws, None, epoch_id, "v0", "e1")
    rows = judge_losses_for_run(ws / "index.db", "run_ing")
    by_name = {r["judge_name"]: r for r in rows}
    assert set(by_name) == {"quality", "schema"}
    assert by_name["quality"]["weighted_loss"] == pytest.approx(6.0)
    assert by_name["quality"]["raw_loss"] == pytest.approx(2.0)
    assert by_name["quality"]["weight"] == pytest.approx(3.0)
    assert by_name["schema"]["weighted_loss"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# 5) Query helpers return expected rows
# ---------------------------------------------------------------------------


def _seed_runs_with_judge_losses(ws: Path, epoch_id: str) -> None:
    """Write two profiles (different generations) with the same judge so the
    per-generation aggregator + cross-generation trend have something to
    chew on."""
    profile_v0 = LossProfile(
        run_id="run_v0",
        entry_id="e1",
        generation_id="v0",
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=5.0,
        pass_fail=None,
        per_judge_loss=(
            JudgeLoss(judge_name="quality", raw_loss=3.0, weight=2.0, weighted_loss=6.0),
        ),
    )
    profile_v1 = LossProfile(
        run_id="run_v1",
        entry_id="e1",
        generation_id="v1",
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=2.0,
        pass_fail=None,
        per_judge_loss=(
            JudgeLoss(judge_name="quality", raw_loss=1.0, weight=2.0, weighted_loss=2.0),
        ),
    )
    write_loss_profile(profile_v0, loss_profile_path(ws, epoch_id, "v0", "e1"))
    write_loss_profile(profile_v1, loss_profile_path(ws, epoch_id, "v1", "e1"))
    ingest_run(ws, None, epoch_id, "v0", "e1")
    ingest_run(ws, None, epoch_id, "v1", "e1")


def test_judge_loss_query_helpers_aggregate_across_runs_and_generations(
    tmp_path: Path,
) -> None:
    """judge_losses_for_generation sums per-judge weighted_loss across runs in
    one generation; judge_loss_trend returns one row per generation along
    an epoch's timeline."""
    ws, epoch_id = _build_min_workspace(tmp_path)
    _seed_runs_with_judge_losses(ws, epoch_id)
    db = ws / "index.db"

    # judge_losses_for_generation
    rows_v0 = judge_losses_for_generation(db, epoch_id, "v0")
    assert len(rows_v0) == 1
    assert rows_v0[0]["judge_name"] == "quality"
    assert rows_v0[0]["total_weighted_loss"] == pytest.approx(6.0)
    assert rows_v0[0]["total_raw_loss"] == pytest.approx(3.0)
    assert rows_v0[0]["run_count"] == 1
    assert rows_v0[0]["weight"] == pytest.approx(2.0)

    # judge_loss_trend across v0 + v1
    trend = judge_loss_trend(db, epoch_id, "quality")
    by_gen = {r["generation_id"]: r for r in trend}
    assert set(by_gen) == {"v0", "v1"}
    assert by_gen["v0"]["total_weighted_loss"] == pytest.approx(6.0)
    assert by_gen["v1"]["total_weighted_loss"] == pytest.approx(2.0)
    # A nonexistent judge yields empty.
    assert judge_loss_trend(db, epoch_id, "nonexistent") == []


# ---------------------------------------------------------------------------
# 6) Repair subcommand re-derives from events.jsonl idempotently
# ---------------------------------------------------------------------------


def test_repair_judge_losses_subcommand_backfills_idempotently(tmp_path: Path) -> None:
    """repair-judge-losses rewrites loss.json with populated per_judge_loss
    and is idempotent across re-runs."""
    ws, epoch_id = _build_min_workspace(tmp_path)
    # Override the epoch's scoring.json so the repair picks up a non-default
    # weighting for the judge. The repair re-reads scoring.json for the epoch.
    scoring_payload = {
        "drift_weight": 1.0,
        "pass_weight": 1.0,
        "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
        "per_judge_weights": {"quality": 4.0},
        "plan_revision_weight": 0.5,
        "runtime_weight": 0.0,
        "promote_margin": 0.01,
        "pass_rate_monotonicity": True,
    }
    scoring_path(ws, epoch_id).write_text(json.dumps(scoring_payload), encoding="utf-8")
    # Seed a loss.json carrying drift_counts attributed to "quality" but
    # an empty per_judge_loss field (the pre-fix shape).
    stale = LossProfile(
        run_id="run_stale",
        entry_id="e1",
        generation_id="v0",
        epoch_id=epoch_id,
        drift_counts=(DriftCount(kind="custom:quality", severity="warning", count=2),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=24.0,
        pass_fail=None,
        per_judge_loss=(),
    )
    write_loss_profile(stale, loss_profile_path(ws, epoch_id, "v0", "e1"))

    runner = CliRunner()
    first = runner.invoke(repair_judge_losses_cmd, ["--workspace", str(ws)])
    assert first.exit_code == 0, first.output
    # After repair: the loss.json carries one JudgeLoss for "quality"
    # with raw_loss = severity * count = 3.0 * 2 = 6.0, weight = 4.0.
    profile_after = read_loss_profile(loss_profile_path(ws, epoch_id, "v0", "e1"))
    by_name = {j.judge_name: j for j in profile_after.per_judge_loss}
    assert set(by_name) == {"quality"}
    assert by_name["quality"].raw_loss == pytest.approx(6.0)
    assert by_name["quality"].weight == pytest.approx(4.0)
    assert by_name["quality"].weighted_loss == pytest.approx(24.0)
    # Re-ingest landed during the repair — judge_losses table has the row.
    rows = judge_losses_for_run(ws / "index.db", "run_stale")
    assert len(rows) == 1
    assert rows[0]["weighted_loss"] == pytest.approx(24.0)
    # Idempotency: running the repair again does not change the file.
    before_second = (loss_profile_path(ws, epoch_id, "v0", "e1")).read_text(encoding="utf-8")
    second = runner.invoke(repair_judge_losses_cmd, ["--workspace", str(ws)])
    assert second.exit_code == 0
    after_second = (loss_profile_path(ws, epoch_id, "v0", "e1")).read_text(encoding="utf-8")
    assert before_second == after_second


# ---------------------------------------------------------------------------
# 7) Analyzer renders per-judge attribution table
# ---------------------------------------------------------------------------


def test_analyzer_renders_per_judge_attribution_table(tmp_path: Path) -> None:
    """The analyzer's report_sections.render_per_judge_attribution_table
    emits a judge x generation table when per_judge_loss is populated on
    the per-generation loss profiles."""
    from zicato.analyzer.report_data import gather_epoch_report_data
    from zicato.analyzer.report_sections import render_per_judge_attribution_table

    ws, epoch_id = _build_min_workspace(tmp_path)
    _seed_runs_with_judge_losses(ws, epoch_id)
    # Also write minimal experiment.json files so the generations appear
    # on the EpochReportData view (the gather walks experiments).
    from zicato.epoch.journal import write_experiment
    from zicato.testing.fixtures import make_experiment, make_outcome_record

    write_experiment(
        ws,
        epoch_id,
        "v0",
        make_experiment(
            epoch_id=epoch_id,
            generation_id="v0",
            parent_generation_id="",
            outcome=None,
        ),
    )
    write_experiment(
        ws,
        epoch_id,
        "v1",
        make_experiment(
            epoch_id=epoch_id,
            generation_id="v1",
            parent_generation_id="v0",
            outcome=make_outcome_record(),
        ),
    )
    data = gather_epoch_report_data(ws, epoch_id)
    # Confirm the gather populated per_judge_loss_totals — the loss.json
    # files we wrote carry the judge "quality".
    totals_by_gen = {g.generation_id: dict(g.per_judge_loss_totals) for g in data.generations}
    assert "quality" in totals_by_gen.get("v0", {})
    assert totals_by_gen["v0"]["quality"] == pytest.approx(6.0)
    assert totals_by_gen["v1"]["quality"] == pytest.approx(2.0)
    # Render the table — it should mention the judge name, both
    # generations, and the per-cell weighted losses.
    table = render_per_judge_attribution_table(data)
    assert "quality" in table
    assert "| v0" in table or "v0 |" in table or "| v0 |" in table
    assert "v1" in table
    assert "6.000" in table  # v0 weighted_loss
    assert "2.000" in table  # v1 weighted_loss
    assert "total" in table  # totals column header


# ---------------------------------------------------------------------------
# Bonus — rebuild_index populates judge_losses from on-disk loss.json
# ---------------------------------------------------------------------------


def test_rebuild_index_populates_judge_losses_from_loss_profile(tmp_path: Path) -> None:
    """A full rebuild_index walks every run's loss.json and lands its
    JudgeLoss entries as judge_losses rows under the new schema."""
    ws, epoch_id = _build_min_workspace(tmp_path)
    _seed_runs_with_judge_losses(ws, epoch_id)
    # Rebuild from scratch — drops index.db first, re-derives all rows.
    rebuild_index(ws)
    rows_v0 = judge_losses_for_run(ws / "index.db", "run_v0")
    assert len(rows_v0) == 1
    assert rows_v0[0]["judge_name"] == "quality"
    assert rows_v0[0]["weighted_loss"] == pytest.approx(6.0)
