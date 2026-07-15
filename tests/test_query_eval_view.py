"""Known-answer tests for the eval-centric readers (EVAL-VIEW.md §3 / WS-READ).

A small seeded workspace exercises both readers end-to-end: 2 candidates on
the champion spine (g0, g1) + one non-promoted challenger (g2), entries with
replicates, a holdout-tagged entry, a cached cell, matchup rows for
discrimination, and base-1000 A/A calibration replicate files for the
per-entry flip rate. The pure analytics helpers are tested independently, and
every degrade path (cold index, unknown epoch/entry, no calibration) plus the
digest round-trip (byte-identical payload) is covered.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from zicato.core import LossProfile
from zicato.dashboard.server import create_app
from zicato.index.schema import apply_schema
from zicato.query import eval_view as ev
from zicato.query.paths import WorkspacePaths
from zicato.tournament.unit_cache import _unit_loss_path

EPOCH = "e_eval"


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace)


# ---------------------------------------------------------------------------
# Pure helper unit tests (no I/O)
# ---------------------------------------------------------------------------


def test_flip_rate_known_answers() -> None:
    assert ev.flip_rate([True, True, False, True, True]) == 0.2
    assert ev.flip_rate([True, True, True]) == 0.0
    assert ev.flip_rate([True, False]) == 0.5
    # Fewer than two usable draws is unmeasured — never a fabricated 0.0.
    assert ev.flip_rate([True]) is None
    assert ev.flip_rate([None, True]) is None
    assert ev.flip_rate([]) is None


def test_pass_ratio_and_majority() -> None:
    assert ev.pass_ratio([True, True, False, None]) == 2 / 3
    assert ev.pass_ratio([None, None]) is None
    assert ev.majority_verdict([True, True, False]) is True
    assert ev.majority_verdict([True, False]) is None  # exact tie → ambiguous
    assert ev.majority_verdict([]) is None


def test_evidence_tiers() -> None:
    assert ev.evidence_of(0) == "none"
    assert ev.evidence_of(1) == "single"
    assert ev.evidence_of(2) == "replicated"


def test_runtime_aggregates() -> None:
    assert ev.runtime_aggregates([10, None, 30, 20]) == {"mean": 20.0, "p50": 20.0, "max": 30.0}
    assert ev.runtime_aggregates([None]) == {"mean": None, "p50": None, "max": None}


def test_discrimination_same_match_pairs() -> None:
    # m1 splits (True vs False) → discriminating; m2 agrees → not.
    rate, n = ev.discrimination(
        [(("t", "m1"), True), (("t", "m1"), False), (("t", "m2"), True), (("t", "m2"), True)]
    )
    assert rate == 0.5
    assert n == 2
    # No group with two usable verdicts → nothing to discriminate.
    assert ev.discrimination([(("t", "m1"), True)]) == (None, 0)


# ---------------------------------------------------------------------------
# Fixture workspace (built programmatically)
# ---------------------------------------------------------------------------


def _write_replicate(
    workspace: Path, gen: str, entry: str, *, passes: bool, replicate: int
) -> None:
    from zicato.telemetry import reducer  # noqa: PLC0415

    loss = LossProfile(
        run_id=f"cal-{gen}-{entry}-{replicate}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=EPOCH,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0 if passes else 0.5,
        pass_fail=passes,
    )
    reducer.write_loss_profile(loss, _unit_loss_path(workspace, EPOCH, gen, entry, replicate))


def _seed_index(workspace: Path) -> None:
    conn = sqlite3.connect(str(workspace / "index.db"))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) VALUES(?,?,?,?)",
            (EPOCH, "h", "2026-07-01", 0),
        )
        # (generation_id, parent, promoted, round_index)
        for gid, par, promo, rnd in [("g0", None, 1, 0), ("g1", "g0", 1, 1), ("g2", "g0", 0, 1)]:
            conn.execute(
                "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
                "promoted, created_at, round_index, elo, elo_se, elo_games) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (EPOCH, gid, par, promo, f"2026-07-0{1 + rnd}", rnd, 1500.0, 40.0, 4),
            )

        def lp(run, gen, entry, drift, pf, rt, tour, match, cached, score=None):
            loss_json = json.dumps({"score": score}) if score is not None else None
            conn.execute(
                "INSERT INTO loss_profiles(run_id, epoch_id, generation_id, entry_id, "
                "drift_loss, pass_fail, runtime_ms, wall_clock_budget_exceeded, loss_json, "
                "tournament_id, match_id, cached, source_epoch, source_run, abort_cause) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    EPOCH,
                    gen,
                    entry,
                    drift,
                    pf,
                    rt,
                    0,
                    loss_json,
                    tour,
                    match,
                    cached,
                    None,
                    None,
                    None,
                ),
            )

        # g0/entryA: replicated cell (2 rows, both pass, mean drift 0.3, mean score 0.8).
        lp("g0A1", "g0", "entryA", 0.2, 1, 10, "T0", None, 0, score=0.9)
        lp("g0A2", "g0", "entryA", 0.4, 1, 20, "T0", None, 0, score=0.7)
        lp("g0B1", "g0", "entryB", 0.0, 1, 5, "T0", None, 0)
        lp("g0C1", "g0", "entryC", 0.1, 1, 5, "T0", None, 1)  # cached cell (carried over)
        # entryA matchups: m1 discriminates (g1 pass vs g2 fail), m2 agrees.
        lp("g1A1", "g1", "entryA", 0.3, 1, 10, "T1", "m1", 0)
        lp("g1A2", "g1", "entryA", 0.3, 1, 10, "T2", "m2", 0)
        lp("g2A1", "g2", "entryA", 0.5, 0, 10, "T1", "m1", 0)
        lp("g2A2", "g2", "entryA", 0.3, 1, 10, "T2", "m2", 0)
        conn.commit()
    finally:
        conn.close()


def _seed(workspace: Path, *, with_calibration: bool = True) -> None:
    edir = workspace / "epochs" / EPOCH
    edir.mkdir(parents=True, exist_ok=True)
    config: dict = {"id": EPOCH, "created_at": "2026-07-01", "closed": False}
    if with_calibration:
        config["noise_floor"] = {"generation_id": "g0", "runs": 3, "max_abs_delta": 0.06}
    (edir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    # holdout_fraction must be in (0, 1); the explicit ``holdout`` tag on entryC
    # wins outright (split_board rule 1), so only entryC is held regardless.
    (edir / "scoring.json").write_text(
        json.dumps({"overfitting": {"enabled": True, "holdout_fraction": 0.5}}), encoding="utf-8"
    )
    board = [
        {"board_meta": True},
        {"id": "entryA", "kind": "single_turn", "input": "a", "wall_clock_budget_seconds": 60},
        {"id": "entryB", "kind": "single_turn", "input": "b", "wall_clock_budget_seconds": 60},
        {
            "id": "entryC",
            "kind": "single_turn",
            "input": "c",
            "wall_clock_budget_seconds": 60,
            "tags": ["holdout"],
        },
    ]
    (edir / "board.jsonl").write_text("\n".join(json.dumps(r) for r in board), encoding="utf-8")
    (workspace / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": EPOCH, "generations": []}]}), encoding="utf-8"
    )
    (workspace / "current_epoch").write_text(EPOCH, encoding="utf-8")
    if with_calibration:
        # entryA: T, T, F → flip 1/3 ; entryB: all pass → 0.0 ; entryC: no draws.
        for i, p in enumerate([True, True, False]):
            _write_replicate(workspace, "g0", "entryA", passes=p, replicate=1000 + i)
        for i, p in enumerate([True, True, True]):
            _write_replicate(workspace, "g0", "entryB", passes=p, replicate=1000 + i)
    _seed_index(workspace)


# ---------------------------------------------------------------------------
# build_eval_matrix
# ---------------------------------------------------------------------------


def test_matrix_axes_and_ordering(tmp_path: Path) -> None:
    _seed(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    assert m["found"] is True
    assert m["epoch_id"] == EPOCH
    # Columns in round order; champion spine = the promoted generations.
    assert [c["generation_id"] for c in m["candidates"]] == ["g0", "g1", "g2"]
    assert [c["champion_spine"] for c in m["candidates"]] == [True, True, False]
    assert m["candidates"][0]["round_index"] == 0
    # Rows in board order; the tagged entry is flagged holdout.
    assert [e["entry_id"] for e in m["entries"]] == ["entryA", "entryB", "entryC"]
    slices = {e["entry_id"]: e["slice"] for e in m["entries"]}
    assert slices == {"entryA": "train", "entryB": "train", "entryC": "holdout"}


def test_matrix_cell_aggregation_and_evidence(tmp_path: Path) -> None:
    _seed(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    cell = {(e["entry_id"]): row for e, row in zip(m["entries"], m["cells"], strict=True)}
    a_g0 = cell["entryA"][0]  # replicated cell
    assert a_g0["replicates"] == 2
    assert a_g0["evidence"] == "replicated"
    assert abs(a_g0["drift_loss"] - 0.3) < 1e-9
    assert abs(a_g0["score"] - 0.8) < 1e-9
    assert a_g0["pass_ratio"] == 1.0
    assert a_g0["pass_fail"] is True
    assert a_g0["cached"] is False
    assert a_g0["runtime_ms_mean"] == 15.0
    # entryB has one run under g0 (single evidence), and no cell for g1/g2.
    b = cell["entryB"]
    assert b[0]["evidence"] == "single"
    assert b[1] is None and b[2] is None
    # entryC's only cell is cached (carried over — never a fresh measurement).
    assert cell["entryC"][0]["cached"] is True


def test_matrix_flip_rates_from_calibration(tmp_path: Path) -> None:
    _seed(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    flags = {e["entry_id"]: e for e in m["entries"]}
    # entryA: T,T,F over 3 draws → 1/3.
    assert abs(flags["entryA"]["flip_rate"] - 1 / 3) < 1e-9
    assert flags["entryA"]["flip_rate_measured"] is True
    assert flags["entryA"]["calibration_runs"] == 3
    # entryB never flipped.
    assert flags["entryB"]["flip_rate"] == 0.0
    # entryC has no calibration draws → unmeasured, NOT 0.0.
    assert flags["entryC"]["flip_rate"] is None
    assert flags["entryC"]["flip_rate_measured"] is False
    assert m["calibration"] == {
        "measured": True,
        "generation_id": "g0",
        "runs": 3,
        "max_abs_delta": 0.06,
    }


def test_matrix_no_calibration_degrades(tmp_path: Path) -> None:
    _seed(tmp_path, with_calibration=False)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    assert m["calibration"]["measured"] is False
    for e in m["entries"]:
        assert e["flip_rate"] is None
        assert e["flip_rate_measured"] is False


def test_matrix_cold_index_same_shape(tmp_path: Path) -> None:
    # No workspace files at all → same-shape empty payload, never a raise.
    m = ev.build_eval_matrix(_paths(tmp_path), "nope")
    assert m["found"] is False
    assert m["candidates"] == [] and m["entries"] == [] and m["cells"] == []
    assert m["calibration"]["measured"] is False


def test_matrix_round_trip_byte_identical(tmp_path: Path) -> None:
    _seed(tmp_path)
    p = _paths(tmp_path)
    assert json.dumps(ev.build_eval_matrix(p, EPOCH), sort_keys=True) == json.dumps(
        ev.build_eval_matrix(p, EPOCH), sort_keys=True
    )


# ---------------------------------------------------------------------------
# build_eval_dossier
# ---------------------------------------------------------------------------


def test_dossier_instrument_and_discrimination(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "entryA")
    assert d["found"] is True
    assert d["slice"] == "train"
    inst = d["instrument"]
    assert abs(inst["flip_rate"] - 1 / 3) < 1e-9
    assert inst["flip_rate_measured"] is True
    # m1 splits, m2 agrees → 1/2 over 2 matchups.
    assert inst["discrimination"] == 0.5
    assert inst["discrimination_pairs"] == 2
    assert inst["replicate_total"] == 6  # 2 (g0) + 2 (g1) + 2 (g2)
    assert inst["runtime_ms_max"] == 20.0
    assert inst["cached_share"] == 0.0


def test_dossier_trajectory_and_attribution(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "entryA")
    traj = {t["generation_id"]: t for t in d["trajectory"]}
    assert traj["g0"]["replicates"] == 2
    assert traj["g0"]["champion_spine"] is True
    assert traj["g2"]["champion_spine"] is False
    # First spine gen to pass is g0; both spine gens pass → no regression.
    assert d["attribution"]["first_passed_by"] == "g0"
    assert d["attribution"]["regressed_by"] == []


def test_dossier_holdout_and_cached_entry(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "entryC")
    assert d["found"] is True
    assert d["slice"] == "holdout"
    assert d["tag"] == "holdout"
    assert d["instrument"]["cached_share"] == 1.0
    # Only a cached, non-matchup row → nothing to discriminate.
    assert d["instrument"]["discrimination"] is None
    assert d["instrument"]["discrimination_pairs"] == 0


def test_dossier_unknown_entry_same_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "no_such_entry")
    assert d["found"] is False
    assert d["trajectory"] == []
    assert d["attribution"] == {"first_passed_by": None, "regressed_by": []}
    assert d["instrument"]["flip_rate"] is None


def test_dossier_round_trip_byte_identical(tmp_path: Path) -> None:
    _seed(tmp_path)
    p = _paths(tmp_path)
    assert json.dumps(ev.build_eval_dossier(p, EPOCH, "entryA"), sort_keys=True) == json.dumps(
        ev.build_eval_dossier(p, EPOCH, "entryA"), sort_keys=True
    )


# ---------------------------------------------------------------------------
# Endpoint payload shapes
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    workspace = tmp_path / ".zicato"
    _seed(workspace)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return TestClient(create_app(workspace, static_dir, read_only=True))


def test_endpoint_evals_matrix(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get(f"/api/epoch/{EPOCH}/evals")
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert [e["entry_id"] for e in body["entries"]] == ["entryA", "entryB", "entryC"]


def test_endpoint_eval_entry(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get(f"/api/epoch/{EPOCH}/eval/entryA")
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert body["instrument"]["discrimination"] == 0.5


def test_endpoint_malformed_id_degrades(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/api/epoch/..%2Fetc/evals")
        assert r.status_code in (200, 404)
        # An unsafe id that still routes returns the same-shape empty payload.
        r2 = c.get(f"/api/epoch/{EPOCH}/eval/bad%20id")
        assert r2.status_code == 200
