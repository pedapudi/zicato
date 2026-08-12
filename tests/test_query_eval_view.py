"""Known-answer tests for the eval-centric readers (EVAL-VIEW.md §3 / WS-READ).

A small seeded workspace exercises all three readers end-to-end against
REAL-SHAPED data — the run ids the pipeline actually emits (``loss_profiles``
PK ``run_id`` = one row per (gen, entry)), per-replicate ``loss.json`` /
``loss.r<N>.json`` files on disk as the worker writes them, and
``experiment.json`` records as the journal writes them. This matters: the
``loss_profiles`` table CANNOT hold "same-match row pairs" (its PK forbids two
rows for one (gen, entry)), so discrimination is read from the DURABLE matchup
records (``build_matchup_grid``) and cell evidence from the replicate FILES —
not a fabricated row count. A gauntlet where the champion faces three
challengers yields ``discrimination_pairs=3`` (§2.3 / F2). The pure analytics
helpers are tested independently; every degrade path plus the digest round-trip
is covered.
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
from zicato.workspace.layout import WorkspaceLayout

EPOCH = "e_eval"
DEAD_EP = "e_dead"


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace)


# ---------------------------------------------------------------------------
# Real-shaped writers — the on-disk artifacts the pipeline actually emits.
# ---------------------------------------------------------------------------


def _write_run_loss(
    workspace: Path,
    epoch: str,
    gen: str,
    entry: str,
    *,
    passes: bool | None,
    drift: float,
    runtime: int,
    replicate: int = 0,
    cached: bool = False,
    score: float | None = None,
) -> None:
    """Write ONE per-replicate ``loss.json`` / ``loss.r<N>.json`` (the worker's output)."""
    from zicato.telemetry import reducer  # noqa: PLC0415

    suffix = "" if replicate == 0 else f":r{replicate}"
    loss = LossProfile(
        run_id=f"{gen}:{entry}{suffix}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=epoch,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=runtime,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift,
        pass_fail=passes,
        cached=cached,
        score=score,
    )
    reducer.write_loss_profile(loss, _unit_loss_path(workspace, epoch, gen, entry, replicate))


def _write_experiment(
    workspace: Path, epoch: str, gen: str, parent: str | None, decision: str | None
) -> None:
    """Write ONE ``experiment.json`` (the journal record the matchup enumerator reads)."""
    layout = WorkspaceLayout.from_root(workspace)
    path = layout.experiment(epoch, gen)
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict = {"generation_id": gen, "parent_generation_id": parent}
    if decision is not None:
        record["outcome"] = {"tournament_decision": decision}
    path.write_text(json.dumps(record), encoding="utf-8")


def _lp_row(conn: sqlite3.Connection, epoch: str, gen: str, entry: str, **kw: object) -> None:
    """Insert ONE ``loss_profiles`` row — run_id ``{gen}:{entry}`` (the PK the pipeline emits)."""
    loss_json = json.dumps({"score": kw.get("score")}) if kw.get("score") is not None else None
    conn.execute(
        "INSERT INTO loss_profiles(run_id, epoch_id, generation_id, entry_id, "
        "drift_loss, pass_fail, runtime_ms, wall_clock_budget_exceeded, loss_json, "
        "tournament_id, match_id, cached, source_epoch, source_run, abort_cause) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{gen}:{entry}",
            epoch,
            gen,
            entry,
            kw.get("drift"),
            1 if kw.get("passes") else 0,
            kw.get("runtime"),
            0,
            loss_json,
            kw.get("tour"),
            None,
            1 if kw.get("cached") else 0,
            None,
            None,
            None,
        ),
    )


def _gen_row(
    conn: sqlite3.Connection,
    epoch: str,
    gid: str,
    parent: str | None,
    promo: int,
    rnd: int,
    created: str,
) -> None:
    conn.execute(
        "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
        "promoted, created_at, round_index, elo, elo_se, elo_games) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (epoch, gid, parent, promo, created, rnd, 1500.0, 40.0, 4),
    )


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
# Fixture workspace (built programmatically) — the EVAL epoch.
# ---------------------------------------------------------------------------


def _write_calibration_replicate(
    workspace: Path, gen: str, entry: str, *, passes: bool, replicate: int
) -> None:
    """Write ONE base-1000 A/A calibration draw file for the per-entry flip rate."""
    _write_run_loss(
        workspace,
        EPOCH,
        gen,
        entry,
        passes=passes,
        drift=0.0 if passes else 0.5,
        runtime=10,
        replicate=replicate,
    )


def _seed_index(workspace: Path) -> None:
    conn = sqlite3.connect(str(workspace / "index.db"))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) VALUES(?,?,?,?)",
            (EPOCH, "h", "2026-07-01", 0),
        )
        # g0 seed (promoted), g1 promoted child, g2 rejected child.
        _gen_row(conn, EPOCH, "g0", None, 1, 0, "2026-07-01")
        _gen_row(conn, EPOCH, "g1", "g0", 1, 1, "2026-07-02")
        _gen_row(conn, EPOCH, "g2", "g0", 0, 1, "2026-07-02")

        # ONE loss_profiles row per (gen, entry) — the PK the pipeline emits.
        _lp_row(
            conn, EPOCH, "g0", "entryA", drift=0.2, passes=True, runtime=10, tour="T0", score=0.9
        )
        _lp_row(conn, EPOCH, "g0", "entryB", drift=0.0, passes=True, runtime=5, tour="T0")
        _lp_row(
            conn, EPOCH, "g0", "entryC", drift=0.1, passes=True, runtime=5, tour="T0", cached=True
        )
        _lp_row(conn, EPOCH, "g1", "entryA", drift=0.3, passes=True, runtime=10, tour="T1")
        _lp_row(conn, EPOCH, "g2", "entryA", drift=0.5, passes=False, runtime=10, tour="T1")
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

    # The canonical per-entry loss.json each candidate wrote (drives the matrix
    # cells AND build_matchup_grid's parent/child verdicts). g0/entryA carries a
    # SECOND duel replicate (loss.r1.json, index 1 — a real evidence slot) so its
    # cell is genuinely REPLICATED off the durable files, not a fabricated count.
    _write_run_loss(workspace, EPOCH, "g0", "entryA", passes=True, drift=0.2, runtime=10, score=0.9)
    _write_run_loss(
        workspace, EPOCH, "g0", "entryA", passes=True, drift=0.4, runtime=20, replicate=1, score=0.7
    )
    _write_run_loss(workspace, EPOCH, "g0", "entryB", passes=True, drift=0.0, runtime=5)
    _write_run_loss(
        workspace, EPOCH, "g0", "entryC", passes=True, drift=0.1, runtime=5, cached=True
    )
    _write_run_loss(workspace, EPOCH, "g1", "entryA", passes=True, drift=0.3, runtime=10)
    _write_run_loss(workspace, EPOCH, "g2", "entryA", passes=False, drift=0.5, runtime=10)

    # The experiment records: g1 promoted, g2 rejected (each a settled matchup vs
    # its parent g0). g0 is the seed — no experiment.json → the axis falls back to
    # the index promoted bool (True), exercising the fallback path.
    _write_experiment(workspace, EPOCH, "g1", "g0", "promoted")
    _write_experiment(workspace, EPOCH, "g2", "g0", "rejected")

    if with_calibration:
        # entryA: T, T, F → flip 1/3 ; entryB: all pass → 0.0 ; entryC: no draws.
        for i, p in enumerate([True, True, False]):
            _write_calibration_replicate(workspace, "g0", "entryA", passes=p, replicate=1000 + i)
        for i, p in enumerate([True, True, True]):
            _write_calibration_replicate(workspace, "g0", "entryB", passes=p, replicate=1000 + i)
    _seed_index(workspace)


def _seed_dead(workspace: Path) -> None:
    """A GAUNTLET whose instrument has a genuinely DEAD channel (§5 WS-HEALTH).

    The champion g0 faces THREE challengers (g1, g2, g3). entryD runs on all four
    and always AGREES → zero discrimination over 3 both-sides matchups → DEAD
    (this is the "champion faces 3 challengers ⇒ discrimination_pairs=3" pin).
    entryE runs on g0/g1/g2 only → 2 both-sides matchups → below the 3-comparison
    threshold → "insufficient comparisons", never dead (the honesty boundary).
    """
    edir = workspace / "epochs" / DEAD_EP
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "config.json").write_text(
        json.dumps({"id": DEAD_EP, "created_at": "2026-07-02", "closed": False}), encoding="utf-8"
    )
    (edir / "scoring.json").write_text(json.dumps({}), encoding="utf-8")
    board = [
        {"board_meta": True},
        {"id": "entryD", "kind": "single_turn", "input": "d", "wall_clock_budget_seconds": 60},
        {"id": "entryE", "kind": "single_turn", "input": "e", "wall_clock_budget_seconds": 60},
    ]
    (edir / "board.jsonl").write_text("\n".join(json.dumps(r) for r in board), encoding="utf-8")
    (workspace / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": DEAD_EP, "generations": []}]}), encoding="utf-8"
    )
    (workspace / "current_epoch").write_text(DEAD_EP, encoding="utf-8")

    # entryD on every gen (always pass → always-agree); entryE on g0/g1/g2 only.
    for gen in ("g0", "g1", "g2", "g3"):
        _write_run_loss(workspace, DEAD_EP, gen, "entryD", passes=True, drift=0.1, runtime=10)
    for gen in ("g0", "g1", "g2"):
        _write_run_loss(workspace, DEAD_EP, gen, "entryE", passes=True, drift=0.1, runtime=10)
    # The three settled matchups: each challenger raced g0 and was rejected.
    for gen in ("g1", "g2", "g3"):
        _write_experiment(workspace, DEAD_EP, gen, "g0", "rejected")

    conn = sqlite3.connect(str(workspace / "index.db"))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) VALUES(?,?,?,?)",
            (DEAD_EP, "h", "2026-07-02", 0),
        )
        _gen_row(conn, DEAD_EP, "g0", None, 1, 0, "2026-07-02")
        for i, gid in enumerate(("g1", "g2", "g3"), start=1):
            _gen_row(conn, DEAD_EP, gid, "g0", 0, 1, f"2026-07-0{2 + i}")
            _lp_row(conn, DEAD_EP, gid, "entryD", drift=0.1, passes=True, runtime=10)
        _lp_row(conn, DEAD_EP, "g0", "entryD", drift=0.1, passes=True, runtime=10)
        _lp_row(conn, DEAD_EP, "g0", "entryE", drift=0.1, passes=True, runtime=10)
        _lp_row(conn, DEAD_EP, "g1", "entryE", drift=0.1, passes=True, runtime=10)
        _lp_row(conn, DEAD_EP, "g2", "entryE", drift=0.1, passes=True, runtime=10)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build_eval_matrix
# ---------------------------------------------------------------------------


def test_matrix_axes_and_ordering(tmp_path: Path) -> None:
    _seed(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    assert m["found"] is True
    assert m["epoch_id"] == EPOCH
    # Columns in round order; champion spine = the SEED plus the promoted chain.
    assert [c["generation_id"] for c in m["candidates"]] == ["g0", "g1", "g2"]
    assert [c["champion_spine"] for c in m["candidates"]] == [True, True, False]
    # The parentless g0 is the SEED — the baseline the reign starts from.
    assert [c["seed"] for c in m["candidates"]] == [True, False, False]
    # Tristate promoted, from the ONE lineage classifier: g1 promoted / g2
    # rejected, and g0 NULL — nothing on disk recorded a decision for the seed
    # (it faced no gate), so the axis reports "no decision" rather than
    # inheriting the index bool. The index's `promoted` column is a weaker
    # record than lineage — the June workspace stamps 0 on a generation lineage
    # records as promoted — so it no longer overrides a file-derived answer.
    # The seed is still the epoch's champion: `seed` + `champion_spine` say so.
    assert [c["promoted"] for c in m["candidates"]] == [None, True, False]
    assert m["candidates"][0]["round_index"] == 0
    # Rows in board order; the tagged entry is flagged holdout.
    assert [e["entry_id"] for e in m["entries"]] == ["entryA", "entryB", "entryC"]
    slices = {e["entry_id"]: e["slice"] for e in m["entries"]}
    assert slices == {"entryA": "train", "entryB": "train", "entryC": "holdout"}


def test_matrix_tristate_promoted_null(tmp_path: Path) -> None:
    # An in-flight generation (experiment.json with NO decision) serves promoted
    # null — never a collapsed False (the Class-B bug), never on the spine.
    _seed(tmp_path)
    _write_experiment(tmp_path, EPOCH, "g2", "g0", None)  # overwrite g2 → undecided
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    g2 = next(c for c in m["candidates"] if c["generation_id"] == "g2")
    assert g2["promoted"] is None
    assert g2["champion_spine"] is False


def test_matrix_cell_aggregation_and_evidence(tmp_path: Path) -> None:
    _seed(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    cell = {(e["entry_id"]): row for e, row in zip(m["entries"], m["cells"], strict=True)}
    a_g0 = cell["entryA"][0]  # replicated cell — TWO real files (r0 + r1)
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
    # The calibration generation is threaded onto every row (N4 — staleness).
    assert flags["entryA"]["calibration_generation"] == "g0"
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
        assert e["calibration_generation"] is None


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
    # Reign matchups (g0,g1) agree + (g0,g2) split → 1/2 over 2 comparisons.
    assert inst["discrimination"] == 0.5
    assert inst["discrimination_pairs"] == 2
    assert inst["replicate_total"] == 3  # one index row per (gen, entry): g0/g1/g2
    assert inst["runtime_ms_max"] == 10.0
    assert inst["cached_share"] == 0.0


def test_dossier_trajectory_and_attribution(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "entryA")
    traj = {t["generation_id"]: t for t in d["trajectory"]}
    assert traj["g0"]["replicates"] == 2  # from the two on-disk replicate files
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
    # entryC ran only on g0 (never both-sided in a matchup) → nothing to discriminate.
    assert d["instrument"]["discrimination"] is None
    assert d["instrument"]["discrimination_pairs"] == 0


def test_dossier_unknown_entry_same_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "no_such_entry")
    assert d["found"] is False
    assert d["trajectory"] == []
    assert d["attribution"]["first_passed_by"] is None
    assert d["attribution"]["regressed_by"] == []
    # An empty panel carries WHY it is empty, on the cold path too (#207 §3).
    assert d["trajectory_reason"] == "No records for this epoch and entry were found."
    assert d["attribution"]["first_passed_reason"] == d["trajectory_reason"]
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


# ---------------------------------------------------------------------------
# The live MDE ladder (pure — known answers hand-computed against CAMPAIGN.md §3)
# ---------------------------------------------------------------------------


def test_students_t_upper_quantile_table_values() -> None:
    # Standard Student-t upper-tail critical values, to 4 dp against the tables.
    assert abs(ev.students_t_upper_quantile(0.025, 10) - 2.2281) < 1e-3
    assert abs(ev.students_t_upper_quantile(0.20, 10) - 0.8791) < 1e-3
    assert abs(ev.students_t_upper_quantile(0.05, 10) - 1.8125) < 1e-3
    assert abs(ev.students_t_upper_quantile(0.025, 2) - 4.3027) < 1e-3
    assert abs(ev.students_t_upper_quantile(0.005, 20) - 2.8453) < 1e-3


def test_mde_ladder_known_answers() -> None:
    # CAMPAIGN.md §3 pins ≈1.79·floor (α=.05) and ≈1.55·floor (α=.10) at n=6, df=10.
    b = ev.mde_ladder(1.0, 6)
    assert b["usable"] is True
    assert b["formula_n"] == 6 and b["df"] == 10
    assert abs(b["mde"] - 1.7939) < 1e-3
    assert abs(b["mde_relaxed"] - 1.5539) < 1e-3
    # Scales linearly with the floor (MDE ∝ sd).
    assert abs(ev.mde_ladder(0.06, 6)["mde"] - 0.06 * 1.7939) < 1e-4
    assert b["floor_measured"] is True and b["floor"] == 1.0


def test_mde_ladder_degrades_honestly() -> None:
    # No floor → floor unmeasured, never a fabricated bound (§4).
    nf = ev.mde_ladder(None, 6)
    assert nf["floor_measured"] is False and nf["mde"] is None and nf["usable"] is False
    assert "floor unmeasured" in nf["note"]
    # n<2 → the two-sample form is undefined; honest "insufficient replication".
    n1 = ev.mde_ladder(0.06, 1)
    assert n1["usable"] is False and n1["mde"] is None
    assert "at least 2 replicates" in n1["note"]
    # A non-int replicate count coerces to 0 → the n<2 note, never a crash.
    assert ev.mde_ladder(0.06, None).get("mde") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_eval_health — the WS-HEALTH instrument panel
# ---------------------------------------------------------------------------


def test_health_mde_uses_floor_and_replicates(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Add a tournament block so the epoch's realised replicate count is 6.
    edir = tmp_path / "epochs" / EPOCH
    scoring = json.loads((edir / "scoring.json").read_text(encoding="utf-8"))
    scoring["tournament"] = {"structure": "gauntlet", "params": {"replicates": 6}}
    (edir / "scoring.json").write_text(json.dumps(scoring), encoding="utf-8")
    h = ev.build_eval_health(_paths(tmp_path), EPOCH)
    assert h["found"] is True
    mde = h["mde"]
    assert mde["floor"] == 0.06 and mde["replicates"] == 6 and mde["formula_n"] == 6
    assert mde["usable"] is True
    assert abs(mde["mde"] - 0.06 * 1.7939) < 1e-4
    assert "t_{α/2,df}" in mde["formula"]


def test_health_mde_defaults_to_single_replicate(tmp_path: Path) -> None:
    _seed(tmp_path)  # no tournament block → replicates defaults to 1 → n<2 note.
    h = ev.build_eval_health(_paths(tmp_path), EPOCH)
    assert h["mde"]["replicates"] == 1
    assert h["mde"]["usable"] is False
    assert "at least 2 replicates" in h["mde"]["note"]


def test_health_noisiest_and_runtime_rankings(tmp_path: Path) -> None:
    _seed(tmp_path)
    h = ev.build_eval_health(_paths(tmp_path), EPOCH)
    # Noisiest: measured entries by descending flip rate; unmeasured entryC omitted.
    assert [r["entry_id"] for r in h["noisiest"]] == ["entryA", "entryB"]
    assert abs(h["noisiest"][0]["flip_rate"] - 1 / 3) < 1e-9
    assert h["noisiest"][1]["flip_rate"] == 0.0
    assert all(r["entry_id"] != "entryC" for r in h["noisiest"])
    # Runtime cost: entryA (3 rows @10ms) leads; single-row entries trail at 5.
    rc = {r["entry_id"]: r for r in h["runtime_cost"]}
    assert h["runtime_cost"][0]["entry_id"] == "entryA"
    assert rc["entryA"]["runtime_ms_mean"] == 10.0
    assert rc["entryA"]["replicate_total"] == 3


def test_health_dead_vs_insufficient_honesty(tmp_path: Path) -> None:
    _seed(tmp_path)
    h = ev.build_eval_health(_paths(tmp_path), EPOCH)
    # entryA discriminates over only 2 matchups (< the 3-comparison threshold), so
    # it is "insufficient comparisons", never dead. entryB/entryC have no matchup.
    assert h["dead"] == []
    ins = {r["entry_id"]: r for r in h["insufficient"]}
    assert set(ins) == {"entryA", "entryB", "entryC"}
    assert ins["entryA"]["discrimination_pairs"] == 2


def test_health_dead_channel_above_threshold(tmp_path: Path) -> None:
    _seed_dead(tmp_path)
    h = ev.build_eval_health(_paths(tmp_path), DEAD_EP)
    dead = {r["entry_id"]: r for r in h["dead"]}
    ins = {r["entry_id"] for r in h["insufficient"]}
    # entryD: champion vs 3 challengers, always agree → zero discrimination over 3
    # comparisons → DEAD (the gauntlet discrimination_pairs=3 pin).
    assert "entryD" in dead and dead["entryD"]["discrimination_pairs"] == 3
    # entryE: only 2 both-sides matchups → below threshold → insufficient, never dead.
    assert "entryD" not in ins
    assert "entryE" in ins and "entryE" not in dead


def test_health_rotation_holdout_and_redundancy_defer(tmp_path: Path) -> None:
    _seed(tmp_path)
    h = ev.build_eval_health(_paths(tmp_path), EPOCH)
    # No holdout-ladder confirmations recorded → no budget yet, no cadence pressure.
    assert h["holdout_budget"] is None
    assert h["rotation"]["refresh_recommended"] is False
    assert h["rotation"]["max_generations_per_contract"] is None
    # No reflection built → redundancy defers with an explicit pointer at reflect.
    assert h["redundancy"]["available"] is False
    assert h["redundancy"]["clusters"] == []
    assert "reflect" in h["redundancy"]["note"]


def test_health_cold_index_same_shape(tmp_path: Path) -> None:
    h = ev.build_eval_health(_paths(tmp_path), "nope")
    assert h["found"] is False
    assert h["noisiest"] == [] and h["dead"] == [] and h["runtime_cost"] == []
    assert h["mde"]["floor_measured"] is False and h["mde"]["mde"] is None
    assert h["holdout_budget"] is None


def test_health_round_trip_byte_identical(tmp_path: Path) -> None:
    _seed(tmp_path)
    p = _paths(tmp_path)
    assert json.dumps(ev.build_eval_health(p, EPOCH), sort_keys=True) == json.dumps(
        ev.build_eval_health(p, EPOCH), sort_keys=True
    )


def test_endpoint_eval_health(tmp_path: Path) -> None:
    with _client(tmp_path) as c:
        r = c.get(f"/api/epoch/{EPOCH}/eval-health")
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert [x["entry_id"] for x in body["noisiest"]] == ["entryA", "entryB"]
        # A malformed id still returns the same-shape empty payload.
        r2 = c.get("/api/epoch/bad%20id/eval-health")
        assert r2.status_code == 200
        assert r2.json()["found"] is False


# ---------------------------------------------------------------------------
# The JUNE-SHAPED workspace (issue #207 §2/§3) — the decisions live in
# lineage.json, NOT in experiment.json.
# ---------------------------------------------------------------------------
#
# Every real pre-stamp workspace looks like this: the orchestrator wrote each
# generation's ``experiment.json`` with ``outcome: null`` at PROPOSE time and
# never rewrote it at settle time; the gate journalled the decision to
# ``lineage.json`` instead. So ``/api/lineage`` reports a settled promoted /
# rejected while the experiment record on disk still reads undecided.
#
# The eval matrix used to classify off ``experiment.json`` ALONE, which made it
# report ``promoted: null`` for every candidate — six columns of "racing…" on an
# epoch that finished in June — and, because the spine is the promoted set, an
# empty champion spine, which is what made the board dossier claim there was no
# trajectory. Both reds are pinned below.


def _seed_june_shaped(workspace: Path) -> None:
    """``_seed`` with the decisions moved to lineage.json (the June shape)."""
    _seed(workspace)
    # Every experiment record is present but UNDECIDED (outcome: null).
    for gid, parent in (("g0", None), ("g1", "g0"), ("g2", "g0")):
        _write_experiment(workspace, EPOCH, gid, parent, None)
    # lineage.json carries the settled truth: the seed reigns, g1 won, g2 lost.
    (workspace / "lineage.json").write_text(
        json.dumps(
            {
                "epochs": [
                    {
                        "id": EPOCH,
                        "generations": [
                            {"id": "g0", "parent_id": None, "promoted": True},
                            {"id": "g1", "parent_id": "g0", "promoted": True},
                            {"id": "g2", "parent_id": "g0", "promoted": False},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_matrix_reads_settled_decisions_from_lineage(tmp_path: Path) -> None:
    # RED (#207 §2): a settled REJECTION recorded only in lineage.json must
    # render as rejected, not as a never-raced null the UI spells "racing…".
    _seed_june_shaped(tmp_path)
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    by_gen = {c["generation_id"]: c for c in m["candidates"]}
    assert by_gen["g2"]["promoted"] is False
    assert by_gen["g1"]["promoted"] is True
    assert by_gen["g0"]["promoted"] is True
    # The matrix and /api/lineage now answer from the ONE classifier, so they
    # cannot disagree about the same generation.
    from zicato.query.lineage_view import build_lineage_view

    lineage = build_lineage_view(_paths(tmp_path), EPOCH, include_ratings=False)
    assert {n["generation_id"]: n["promoted"] for n in lineage["generations"]} == {
        g: c["promoted"] for g, c in by_gen.items()
    }


def test_spine_includes_the_seed_when_every_challenger_was_rejected(tmp_path: Path) -> None:
    # RED (#207 §3): an epoch whose challengers were ALL rejected still has a
    # spine — the seed alone — and a one-generation spine is a real trajectory.
    _seed_june_shaped(tmp_path)
    # Demote g1 so the seed is the only reigning generation.
    (tmp_path / "lineage.json").write_text(
        json.dumps(
            {
                "epochs": [
                    {
                        "id": EPOCH,
                        "generations": [
                            {"id": "g0", "parent_id": None, "promoted": True},
                            {"id": "g1", "parent_id": "g0", "promoted": False},
                            {"id": "g2", "parent_id": "g0", "promoted": False},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    by_gen = {c["generation_id"]: c for c in m["candidates"]}
    assert by_gen["g0"]["seed"] is True and by_gen["g0"]["champion_spine"] is True
    assert [g for g, c in by_gen.items() if c["champion_spine"]] == ["g0"]

    d = ev.build_eval_dossier(_paths(tmp_path), EPOCH, "entryA")
    spine = [t for t in d["trajectory"] if t["champion_spine"]]
    assert [t["generation_id"] for t in spine] == ["g0"]
    # The seed's own reading on the entry IS the trajectory point.
    assert spine[0]["drift_loss"] is not None
    assert d["trajectory_reason"] is None  # there is something to plot


def test_seed_is_on_the_spine_even_with_nothing_promoted(tmp_path: Path) -> None:
    # The seed anchors the reign whether or not anything recorded a promotion
    # for it: it is the champion the epoch started from.
    _seed_june_shaped(tmp_path)
    (tmp_path / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": EPOCH, "generations": []}]}), encoding="utf-8"
    )
    m = ev.build_eval_matrix(_paths(tmp_path), EPOCH)
    by_gen = {c["generation_id"]: c for c in m["candidates"]}
    assert by_gen["g0"]["promoted"] is None  # no decision was ever recorded
    assert by_gen["g0"]["seed"] is True and by_gen["g0"]["champion_spine"] is True


def test_empty_spine_panels_carry_distinct_reasons(tmp_path: Path) -> None:
    # #207 §3: each genuinely-empty panel names WHY, and the reasons differ by
    # cause — a seed that failed the entry reads differently from a spine that
    # never ran it.
    _seed_june_shaped(tmp_path)
    (tmp_path / "lineage.json").write_text(
        json.dumps(
            {
                "epochs": [
                    {
                        "id": EPOCH,
                        "generations": [
                            {"id": "g0", "parent_id": None, "promoted": True},
                            {"id": "g1", "parent_id": "g0", "promoted": False},
                            {"id": "g2", "parent_id": "g0", "promoted": False},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    p = _paths(tmp_path)

    # entryA: the seed RAN it and PASSED it → first-passed-by is real, and the
    # one-generation spine explains why nothing could have regressed.
    passed = ev.build_eval_dossier(p, EPOCH, "entryA")
    assert passed["attribution"]["first_passed_by"] == "g0"
    assert passed["attribution"]["first_passed_reason"] is None
    assert "one-generation spine cannot regress" in passed["attribution"]["regressed_reason"]

    # entryB with the seed's verdict flipped to a FAIL: the honest reading is
    # that the seed failed it and nothing later was promoted — never "not yet".
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        conn.execute("UPDATE loss_profiles SET pass_fail = 0 WHERE run_id = 'g0:entryB'")
        conn.commit()
    finally:
        conn.close()
    _write_run_loss(tmp_path, EPOCH, "g0", "entryB", passes=False, drift=0.9, runtime=5)
    failed = ev.build_eval_dossier(p, EPOCH, "entryB")
    assert failed["attribution"]["first_passed_by"] is None
    assert failed["attribution"]["first_passed_reason"] == (
        "The seed (g0) did not pass this entry, and no later generation was promoted."
    )
    assert "yet" not in failed["attribution"]["first_passed_reason"]

    # entryC: the seed never ran it at all → a different reason again.
    never = ev.build_eval_dossier(p, EPOCH, "entryC")
    assert never["found"] is True
    assert (
        never["attribution"]["first_passed_reason"]
        != (failed["attribution"]["first_passed_reason"])
    )
