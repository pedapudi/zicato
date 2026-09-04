"""Phase-1 light-up tests for the dashboard service.

These exercise the readers and endpoints that wire real data into the
phase-0 stubs:

* ``build_workspace_identity`` — structured workspace identity payload.
* ``build_per_judge_trend`` — L1 judge × generation heatmap matrix.
* ``build_per_judge_for_generation`` — L2 per-judge totals.
* ``build_per_entry_for_generation`` — L2 per-entry via tournament FK.
* ``build_per_judge_comparison`` — L3 champion vs challenger judge Δ.
* ``build_per_judge_for_run`` — L4 per-judge totals for a single run.
* ``build_workspace_view`` — now exposes ``parent_epoch_id`` per row.
* ``build_epoch_view`` — now exposes the frozen ``goal`` field.
* Endpoint routes — each new ``/api/...`` path resolves and returns the
  expected shape against the populated fixture workspace.

The five per-judge readers are held by served-payload pins: each response
is compared against the exact JSON text an endpoint writes, so a change to
how a judge row is decoded cannot alter a key name, a key's position, or a
coerced value without failing here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query import (
    WorkspacePaths,
    build_epoch_view,
    build_per_entry_for_generation,
    build_per_judge_comparison,
    build_per_judge_for_entry,
    build_per_judge_for_generation,
    build_per_judge_for_run,
    build_per_judge_trend,
    build_workspace_view,
)
from zicato.query.epoch_view import build_epochs_summary
from zicato.query.judge_view import build_workspace_identity


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: object) -> None:
    _write(path, json.dumps(obj))


@pytest.fixture
def phase1_workspace(tmp_path: Path) -> Path:
    """A workspace populated with judge_losses + tournament FK + epoch goals.

    Two epochs, four generations under e0 (v0 → v1 → v2 with v1a as a
    rejected sibling), and per-judge / per-entry data populated so each
    new endpoint surfaces a non-empty payload.
    """
    ws = tmp_path / ".zicato"
    (ws / "runtime" / "active_runs").mkdir(parents=True)
    (ws / "runtime" / "control").mkdir(parents=True)

    e0 = "2026-05-16_e0"
    e1 = "2026-05-17_e1"
    _write(ws / "current_epoch", e0)

    # Workspace-level config — entrypoint + a (real, walkable) source root.
    source_root = ws / "source_a"
    source_root.mkdir()
    (source_root / "a.py").write_text(
        '# zicato:mutable id="m1"\nSYSTEM_PROMPT = "hello"\n',
        encoding="utf-8",
    )
    _write_json(
        ws / "config.json",
        {
            "adapter": {
                "entrypoint": "kossel_run:root_agent",
                "mutable_trees": [str(source_root)],
            }
        },
    )

    # Heartbeat for instance_id + created_at.
    _write_json(
        ws / "runtime" / "heartbeat.json",
        {
            "pid": 4242,
            "instance_id": "phase1-test",
            "started_at": "2026-05-16T04:00:00Z",
            "last_heartbeat": "2026-05-16T04:30:00Z",
            "epoch_id": e0,
            "generation_id": "v2",
        },
    )

    # Epoch directories — both epochs with brief + scoring + board.
    for eid, goal in (
        (e0, "Tighten the planner to reduce drift on multi-turn boards."),
        (e1, "Reduce wall-clock variance on the long-form board."),
    ):
        epoch_dir = ws / "epochs" / eid
        _write(epoch_dir / "board.jsonl", json.dumps({"id": "entry_alpha"}) + "\n")
        _write(epoch_dir / "brief.md", f"# brief\n\n## Goal\n\n{goal}\n")
        _write_json(epoch_dir / "scoring.json", {"weights": {"drift_loss": 1.0}})
        _write_json(
            epoch_dir / "config.json",
            {"contract_hash": "h", "closed": False, "goal": goal},
        )

    # Generations on e0: v0 (promoted baseline, from #173), v1
    # (promoted child of v0), v1a (rejected sibling), v2 (promoted
    # child of v1).
    e0_dir = ws / "epochs" / e0
    for gid, parent, decision in (
        ("v0", None, "promoted"),
        ("v1", "v0", "promoted"),
        ("v1a", "v0", "rejected"),
        ("v2", "v1", "promoted"),
    ):
        gen_dir = e0_dir / "generations" / gid
        gen_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            gen_dir / "experiment.json",
            {
                "parent_generation_id": parent,
                "proposed_at": "2026-05-16T04:25:00Z",
                "outcome": {
                    "decision": decision,
                    "scalar_score_delta": -0.05 if decision == "promoted" else 0.10,
                },
            },
        )

    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": e0,
                    "generations": [
                        {"id": "v0", "parent_id": None, "promoted": True},
                        {"id": "v1", "parent_id": "v0", "promoted": True},
                        {"id": "v1a", "parent_id": "v0", "promoted": False},
                        {"id": "v2", "parent_id": "v1", "promoted": True},
                    ],
                }
            ]
        },
    )

    # Build the analytical index with judge_losses + tournament_id FK
    # + epochs.goal + epochs.parent_epoch_id (the schema v2 shape).
    _build_index(ws / "index.db", e0, e1, source_root)
    return ws


def _build_index(path: Path, e0: str, e1: str, source_root: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE epochs(epoch_id TEXT PRIMARY KEY, contract_hash TEXT,
            created_at TEXT, closed INTEGER, goal TEXT, parent_epoch_id TEXT);
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INTEGER, created_at TEXT,
            PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT,
            hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT,
            tournament_decision TEXT, rejection_reason TEXT, scalar_score_delta REAL,
            drift_loss_delta REAL, pass_rate_delta REAL, outcome_json TEXT,
            PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE patches(patch_id TEXT PRIMARY KEY, epoch_id TEXT,
            generation_id TEXT, mutation_id TEXT, op TEXT, rationale TEXT);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY, epoch_id TEXT, generation_id TEXT,
            entry_id TEXT, started_at TEXT, ended_at TEXT, aborted INTEGER,
            runtime_ms INTEGER, tournament_id TEXT);
        CREATE TABLE loss_profiles(run_id TEXT PRIMARY KEY, epoch_id TEXT,
            generation_id TEXT, entry_id TEXT, drift_loss REAL, pass_fail INTEGER,
            runtime_ms INTEGER, wall_clock_budget_exceeded INTEGER, loss_json TEXT,
            tournament_id TEXT);
        CREATE TABLE metric_counts(run_id TEXT, namespace TEXT, name TEXT,
            severity TEXT, count REAL);
        CREATE TABLE tournaments(tournament_id TEXT PRIMARY KEY, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT);
        CREATE TABLE judge_losses(run_id TEXT, judge_name TEXT, weighted_loss REAL,
            raw_loss REAL, weight REAL, PRIMARY KEY(run_id, judge_name));
        """
    )

    # Epoch rows — e1 is a child of e0.
    conn.executemany(
        "INSERT INTO epochs VALUES(?,?,?,?,?,?)",
        [
            (
                e0,
                "h",
                "2026-05-16T04:00:00Z",
                0,
                "Tighten the planner to reduce drift on multi-turn boards.",
                None,
            ),
            (
                e1,
                "h",
                "2026-05-17T04:00:00Z",
                0,
                "Reduce wall-clock variance on the long-form board.",
                e0,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?,?)",
        [
            (e0, "v0", None, 1, "2026-05-16T04:05:00Z"),
            (e0, "v1", "v0", 1, "2026-05-16T04:15:00Z"),
            (e0, "v1a", "v0", 0, "2026-05-16T04:17:00Z"),
            (e0, "v2", "v1", 1, "2026-05-16T04:25:00Z"),
        ],
    )
    # Tournaments — v0->v1 (promoted), v0->v1a (rejected), v1->v2 (promoted).
    for parent, child, decision in (
        ("v0", "v1", "promoted"),
        ("v0", "v1a", "rejected"),
        ("v1", "v2", "promoted"),
    ):
        conn.execute(
            "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"{e0}:{parent}->{child}",
                e0,
                parent,
                child,
                decision,
                0.5,
                0.4 if decision == "promoted" else 0.6,
                -0.1 if decision == "promoted" else 0.1,
                None if decision == "promoted" else "worse drift",
                "2026-05-16T04:30:00Z",
            ),
        )

    # Runs + loss_profiles + judge_losses. Each gen runs entry_alpha once.
    # judge_losses populated for two judges: critic_A and critic_B.
    judge_rows = []
    for gid, drift in (("v0", 0.5), ("v1", 0.3), ("v1a", 0.7), ("v2", 0.2)):
        run_id = f"run_{gid}"
        # tournament_id mirrors the ingester convention.
        if gid == "v0":
            tournament_id = None
        elif gid == "v1":
            tournament_id = f"{e0}:v0->v1"
        elif gid == "v1a":
            tournament_id = f"{e0}:v0->v1a"
        else:
            tournament_id = f"{e0}:v1->v2"
        conn.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, e0, gid, "entry_alpha", "", "", 0, 100, tournament_id),
        )
        conn.execute(
            "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, e0, gid, "entry_alpha", drift, 1, 100, 0, "{}", tournament_id),
        )
        # Each gen drives both judges; loss inverts roughly with drift.
        judge_rows.append((run_id, "critic_A", drift * 0.6, drift * 0.8, 0.75))
        judge_rows.append((run_id, "critic_B", drift * 0.4, drift * 0.5, 0.25))
    conn.executemany(
        "INSERT INTO judge_losses VALUES(?,?,?,?,?)",
        judge_rows,
    )
    # Also drop a per-run loss.json on disk so the entry → run id
    # resolver in the by-entry per-judge endpoint can recover the run.
    workspace_root = path.parent
    for gid in ("v0", "v1", "v1a", "v2"):
        run_dir = workspace_root / "epochs" / e0 / "generations" / gid / "runs" / "entry_alpha"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "loss.json").write_text(
            json.dumps({"run_id": f"run_{gid}", "entry_id": "entry_alpha"}),
            encoding="utf-8",
        )
    conn.commit()
    conn.close()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static_phase1"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    (d / "console.js").write_text("// app", encoding="utf-8")
    return d


@pytest.fixture
def phase1_client(phase1_workspace: Path, static_dir: Path) -> TestClient:
    app = create_app(phase1_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# build_workspace_identity
# ---------------------------------------------------------------------------


def test_workspace_identity_exposes_structured_fields(phase1_workspace: Path) -> None:
    ident = build_workspace_identity(WorkspacePaths(phase1_workspace))
    assert ident["root"] == str(phase1_workspace)
    assert ident["adk_entrypoint"] == "kossel_run:root_agent"
    assert isinstance(ident["source_roots"], list) and len(ident["source_roots"]) == 1
    assert ident["instance_id"] == "phase1-test"
    assert ident["created_at"] == "2026-05-16T04:00:00Z"
    # Contract paths point at the live epoch's files.
    assert "epochs/2026-05-16_e0/board.jsonl" in ident["board_path"]
    assert "epochs/2026-05-16_e0/brief.md" in ident["brief_path"]
    assert "epochs/2026-05-16_e0/scoring.json" in ident["scoring_path"]


def test_workspace_identity_mutation_point_count(phase1_workspace: Path) -> None:
    ident = build_workspace_identity(WorkspacePaths(phase1_workspace))
    # The fixture's source_a/a.py has exactly one ``# zicato:mutable``
    # marker; the enumerator must surface it on the L0 identity card.
    assert ident["mutation_point_count"] >= 1


def test_mutation_point_count_is_off_the_per_request_hot_path(
    phase1_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enumeration runs ONCE across a burst of identity builds.

    ``/api/environment`` folds this in, and the dashboard hits that endpoint on
    every heartbeat — many times a second. The enumerator opens and AST-parses
    every file under every source root, so a per-request walk put a whole-tree
    parse behind each heartbeat.
    """
    from zicato.query import judge_view

    judge_view._MUTATION_COUNT_CACHE.clear()

    calls = {"n": 0}
    import zicato.mutation.enumerator as enumerator

    inner = enumerator.enumerate_mutations

    def counting(roots):
        calls["n"] += 1
        return inner(roots)

    monkeypatch.setattr(enumerator, "enumerate_mutations", counting)

    counts = [
        build_workspace_identity(WorkspacePaths(phase1_workspace))["mutation_point_count"]
        for _ in range(20)
    ]
    assert calls["n"] == 1
    # The MEASURED value is served every time, not just on the walk.
    assert counts == [counts[0]] * 20
    assert counts[0] >= 1


def test_mutation_point_count_is_re_walked_once_the_ttl_lapses(
    phase1_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staleness is BOUNDED — a cache that never expires would lie forever."""
    from zicato.query import judge_view

    judge_view._MUTATION_COUNT_CACHE.clear()
    monkeypatch.setattr(judge_view, "_MUTATION_COUNT_TTL_S", 0.0)

    calls = {"n": 0}
    import zicato.mutation.enumerator as enumerator

    inner = enumerator.enumerate_mutations

    def counting(roots):
        calls["n"] += 1
        return inner(roots)

    monkeypatch.setattr(enumerator, "enumerate_mutations", counting)

    for _ in range(3):
        build_workspace_identity(WorkspacePaths(phase1_workspace))
    assert calls["n"] == 3


def test_workspace_identity_degrades_when_config_absent(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    ident = build_workspace_identity(WorkspacePaths(ws))
    # All fields are present; missing inputs degrade to None / empty list.
    assert ident["root"] == str(ws)
    assert ident["adk_entrypoint"] is None
    assert ident["source_roots"] == []
    assert ident["mutation_point_count"] == 0


# ---------------------------------------------------------------------------
# build_workspace_view — parent_epoch_id edge
# ---------------------------------------------------------------------------


def test_workspace_view_includes_parent_epoch_id(phase1_workspace: Path) -> None:
    view = build_workspace_view(WorkspacePaths(phase1_workspace))
    rows = {r["epoch_id"]: r for r in view["epochs"]}
    # e1 was seeded with parent_epoch_id = e0; the L0 view must surface
    # that so the lineage column can render an arrow between rows.
    assert rows["2026-05-17_e1"]["parent_epoch_id"] == "2026-05-16_e0"
    # The root epoch has no parent.
    assert rows["2026-05-16_e0"]["parent_epoch_id"] is None


def test_workspace_view_prefers_config_goal_over_brief(phase1_workspace: Path) -> None:
    view = build_workspace_view(WorkspacePaths(phase1_workspace))
    row = next(r for r in view["epochs"] if r["epoch_id"] == "2026-05-16_e0")
    assert row["goal"] == "Tighten the planner to reduce drift on multi-turn boards."


# ---------------------------------------------------------------------------
# build_epoch_view — goal field
# ---------------------------------------------------------------------------


def test_build_epoch_view_surfaces_frozen_goal(phase1_workspace: Path) -> None:
    view = build_epoch_view(WorkspacePaths(phase1_workspace))
    assert view["epoch_id"] == "2026-05-16_e0"
    assert view["goal"] == "Tighten the planner to reduce drift on multi-turn boards."


# ---------------------------------------------------------------------------
# Per-judge / per-entry helpers
# ---------------------------------------------------------------------------


E0 = "2026-05-16_e0"


def _served(payload: dict[str, object]) -> str:
    """The payload as an endpoint writes it — values and insertion key order."""
    return json.dumps(payload, separators=(",", ":"))


@pytest.fixture
def unindexed_workspace(tmp_path: Path) -> Path:
    """A workspace with no ``index.db`` at all."""
    ws = tmp_path / "unindexed" / ".zicato"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
def workspace_without_judge_losses(tmp_path: Path) -> Path:
    """A workspace whose ``index.db`` exists but carries no ``judge_losses``.

    The degrade note fires when the index QUERY raises, which a missing
    table does and a missing file does not — a missing file reads as zero
    rows. The two degrade to different payloads, so both are pinned.
    """
    ws = tmp_path / "no_judge_losses" / ".zicato"
    ws.mkdir(parents=True)
    conn = sqlite3.connect(ws / "index.db")
    conn.execute("CREATE TABLE runs(run_id TEXT)")
    conn.commit()
    conn.close()
    return ws


def test_build_per_judge_trend_returns_judge_by_generation(
    phase1_workspace: Path, unindexed_workspace: Path
) -> None:
    # ``generations`` is the promoted spine v0 → v1 → v2, so the rejected
    # sibling v1a is absent from it — but present in every judge's
    # by_generation map, which is not spine-restricted.
    assert _served(build_per_judge_trend(WorkspacePaths(phase1_workspace), E0)) == (
        '{"epoch_id":"2026-05-16_e0","generations":["v0","v1","v2"],"judges":['
        '{"judge_name":"critic_A","by_generation":'
        '{"v0":0.3,"v1":0.18,"v1a":0.42,"v2":0.12}},'
        '{"judge_name":"critic_B","by_generation":'
        '{"v0":0.2,"v1":0.12,"v1a":0.27999999999999997,"v2":0.08000000000000002}}]}'
    )
    # The spine still renders without an index; only the judges drop out.
    assert _served(build_per_judge_trend(WorkspacePaths(unindexed_workspace), E0)) == (
        '{"epoch_id":"2026-05-16_e0","generations":[],"judges":[],'
        '"note":"index not built; run zicato repair index"}'
    )


def test_build_per_judge_for_generation_returns_totals(
    phase1_workspace: Path, unindexed_workspace: Path, workspace_without_judge_losses: Path
) -> None:
    # ``run_count`` sits between raw_loss and weight; this is the only one
    # of the five readers that carries it.
    assert _served(build_per_judge_for_generation(WorkspacePaths(phase1_workspace), E0, "v1")) == (
        '{"epoch_id":"2026-05-16_e0","generation_id":"v1","judges":['
        '{"judge_name":"critic_A","weighted_loss":0.18,"raw_loss":0.24,'
        '"run_count":1,"weight":0.75},'
        '{"judge_name":"critic_B","weighted_loss":0.12,"raw_loss":0.15,'
        '"run_count":1,"weight":0.25}]}'
    )
    assert _served(
        build_per_judge_for_generation(WorkspacePaths(unindexed_workspace), E0, "v1")
    ) == ('{"epoch_id":"2026-05-16_e0","generation_id":"v1","judges":[]}')
    assert _served(
        build_per_judge_for_generation(WorkspacePaths(workspace_without_judge_losses), E0, "v1")
    ) == (
        '{"epoch_id":"2026-05-16_e0","generation_id":"v1","judges":[],'
        '"note":"index not built; run zicato repair index"}'
    )


def test_build_per_entry_uses_tournament_id_fk(phase1_workspace: Path) -> None:
    payload = build_per_entry_for_generation(
        WorkspacePaths(phase1_workspace), "2026-05-16_e0", "v1"
    )
    # FK composed from epoch + parent_generation_id (v0) → child (v1).
    assert payload["tournament_id"] == "2026-05-16_e0:v0->v1"
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["entry_id"] == "entry_alpha"
    assert entry["drift_loss"] is not None


def test_build_per_entry_v0_has_no_tournament_id(phase1_workspace: Path) -> None:
    # v0 is a root generation with no parent → no tournament round attached.
    payload = build_per_entry_for_generation(
        WorkspacePaths(phase1_workspace), "2026-05-16_e0", "v0"
    )
    assert payload["tournament_id"] is None
    # The fallback walks loss_profiles_for_generation, so the row still
    # surfaces.
    assert len(payload["entries"]) == 1


def test_build_per_judge_comparison_picks_primary_driver(
    phase1_workspace: Path, unindexed_workspace: Path, workspace_without_judge_losses: Path
) -> None:
    assert _served(
        build_per_judge_comparison(WorkspacePaths(phase1_workspace), E0, "v1", "v2")
    ) == (
        '{"epoch_id":"2026-05-16_e0","champion":"v1","challenger":"v2","judges":['
        '{"judge_name":"critic_A","champion_weighted_loss":0.18,'
        '"challenger_weighted_loss":0.12,"delta":-0.06},'
        '{"judge_name":"critic_B","champion_weighted_loss":0.12,'
        '"challenger_weighted_loss":0.08000000000000002,"delta":-0.03999999999999998}],'
        '"primary_driver":"critic_A"}'
    )
    assert _served(
        build_per_judge_comparison(WorkspacePaths(unindexed_workspace), E0, "v1", "v2")
    ) == (
        '{"epoch_id":"2026-05-16_e0","champion":"v1","challenger":"v2",'
        '"judges":[],"primary_driver":null}'
    )
    assert _served(
        build_per_judge_comparison(WorkspacePaths(workspace_without_judge_losses), E0, "v1", "v2")
    ) == (
        '{"epoch_id":"2026-05-16_e0","champion":"v1","challenger":"v2",'
        '"judges":[],"primary_driver":null,'
        '"note":"index not built; run zicato repair index"}'
    )


def test_per_judge_comparison_signs_a_one_sided_judge(phase1_workspace: Path) -> None:
    """A judge that fired on only one side still yields a signed delta.

    A challenger-only judge reads as its own loss and a champion-only judge
    as the negation of its loss, so a judge that appeared or disappeared
    between the two generations is scored rather than dropped.
    """
    conn = sqlite3.connect(phase1_workspace / "index.db")
    conn.executemany(
        "INSERT INTO judge_losses VALUES(?,?,?,?,?)",
        [("run_v2", "critic_new", 0.5, 0.6, 0.5), ("run_v1", "critic_gone", 0.9, 1.0, 0.5)],
    )
    conn.commit()
    conn.close()
    payload = build_per_judge_comparison(WorkspacePaths(phase1_workspace), E0, "v1", "v2")
    rows = {row["judge_name"]: row["delta"] for row in payload["judges"]}
    assert rows["critic_new"] == 0.5
    assert rows["critic_gone"] == -0.9
    # The largest absolute delta names the driver, one-sided rows included.
    assert payload["primary_driver"] == "critic_gone"


def test_build_per_judge_for_run_returns_rows(
    phase1_workspace: Path, unindexed_workspace: Path, workspace_without_judge_losses: Path
) -> None:
    assert _served(build_per_judge_for_run(WorkspacePaths(phase1_workspace), "run_v1")) == (
        '{"run_id":"run_v1","judges":['
        '{"judge_name":"critic_A","weighted_loss":0.18,"raw_loss":0.24,"weight":0.75},'
        '{"judge_name":"critic_B","weighted_loss":0.12,"raw_loss":0.15,"weight":0.25}]}'
    )
    assert _served(build_per_judge_for_run(WorkspacePaths(unindexed_workspace), "run_v1")) == (
        '{"run_id":"run_v1","judges":[]}'
    )
    assert _served(
        build_per_judge_for_run(WorkspacePaths(workspace_without_judge_losses), "run_v1")
    ) == ('{"run_id":"run_v1","judges":[],"note":"index not built; run zicato repair index"}')


def test_build_per_judge_for_entry_decodes_the_loss_file(phase1_workspace: Path) -> None:
    paths = WorkspacePaths(phase1_workspace)
    # v1's loss.json carries no per_judge_loss block, so this reader falls
    # through to the run-keyed reader and answers in its shape.
    assert _served(build_per_judge_for_entry(paths, E0, "v1", "entry_alpha")) == (
        '{"run_id":"run_v1","judges":['
        '{"judge_name":"critic_A","weighted_loss":0.18,"raw_loss":0.24,"weight":0.75},'
        '{"judge_name":"critic_B","weighted_loss":0.12,"raw_loss":0.15,"weight":0.25}]}'
    )
    # No loss.json for that entry at all: the requested id echoes back.
    assert _served(build_per_judge_for_entry(paths, E0, "v1", "absent_entry")) == (
        '{"run_id":"absent_entry","judges":[]}'
    )
    # With a block present, rows decode from the FILE. The reducer writes
    # it and the reader does not schema-check it: a missing or non-numeric
    # field coerces to null, and a nameless or non-mapping row is dropped.
    _write_json(
        phase1_workspace
        / "epochs"
        / E0
        / "generations"
        / "v2"
        / "runs"
        / "entry_alpha"
        / "loss.json",
        {
            "run_id": "run_v2",
            "per_judge_loss": [
                {"judge_name": "critic_A", "weighted_loss": 0.12, "raw_loss": 0.16, "weight": 0.75},
                {"judge_name": "critic_B", "weighted_loss": None, "raw_loss": "x", "weight": 0.25},
                {"judge_name": "", "weighted_loss": 1.0},
                "not a mapping",
            ],
        },
    )
    assert _served(build_per_judge_for_entry(paths, E0, "v2", "entry_alpha")) == (
        '{"run_id":"run_v2","judges":['
        '{"judge_name":"critic_A","weighted_loss":0.12,"raw_loss":0.16,"weight":0.75},'
        '{"judge_name":"critic_B","weighted_loss":null,"raw_loss":null,"weight":0.25}]}'
    )


# ---------------------------------------------------------------------------
# Endpoint shapes
# ---------------------------------------------------------------------------


def test_endpoint_per_judge_trend(phase1_client: TestClient) -> None:
    r = phase1_client.get("/api/epoch/2026-05-16_e0/per-judge-trend")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == "2026-05-16_e0"
    assert body["generations"] == ["v0", "v1", "v2"]
    assert isinstance(body["judges"], list)


def test_endpoint_per_judge_for_generation(phase1_client: TestClient) -> None:
    r = phase1_client.get("/api/generation/2026-05-16_e0/v1/per-judge")
    assert r.status_code == 200
    body = r.json()
    assert body["generation_id"] == "v1"
    assert any(j["judge_name"] == "critic_A" for j in body["judges"])


def test_endpoint_per_entry_for_generation(phase1_client: TestClient) -> None:
    r = phase1_client.get("/api/generation/2026-05-16_e0/v1/per-entry")
    assert r.status_code == 200
    body = r.json()
    assert body["tournament_id"] == "2026-05-16_e0:v0->v1"
    assert any(e["entry_id"] == "entry_alpha" for e in body["entries"])


def test_endpoint_per_judge_comparison(phase1_client: TestClient) -> None:
    r = phase1_client.get("/api/round/2026-05-16_e0/v1/v2/per-judge-comparison")
    assert r.status_code == 200
    body = r.json()
    assert body["champion"] == "v1"
    assert body["challenger"] == "v2"
    assert body["primary_driver"] in {"critic_A", "critic_B"}


def test_endpoint_per_judge_for_run(phase1_client: TestClient) -> None:
    r = phase1_client.get("/api/run/run_v1/per-judge")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run_v1"
    assert any(j["judge_name"] == "critic_A" for j in body["judges"])


def test_endpoint_per_judge_for_run_by_entry(
    phase1_client: TestClient, phase1_workspace: Path
) -> None:
    r = phase1_client.get("/api/run/2026-05-16_e0/v1/entry_alpha/per-judge")
    assert r.status_code == 200
    body = r.json()
    # The entry's loss.json carries run_id "run_v1"; the endpoint
    # resolves it and returns the same shape as the run_id-keyed route.
    assert body["run_id"] == "run_v1"


def test_per_judge_by_entry_agrees_across_its_two_sources(
    phase1_client: TestClient, phase1_workspace: Path
) -> None:
    """The by-entry route answers from ``loss.json`` OR the index, never differently.

    ``build_per_judge_for_entry`` prefers the run directory's own
    ``per_judge_loss`` block and falls back to the ``judge_losses`` index
    when the block is absent. Which source answered must not be visible in
    the response: the fixture's v1 loss.json carries no block, so the first
    read comes from the index; writing the SAME rows into the file must
    leave the response byte-identical.
    """
    url = "/api/run/2026-05-16_e0/v1/entry_alpha/per-judge"
    from_index = phase1_client.get(url).json()
    assert [row["judge_name"] for row in from_index["judges"]] == ["critic_A", "critic_B"]

    loss_path = (
        phase1_workspace
        / "epochs"
        / "2026-05-16_e0"
        / "generations"
        / "v1"
        / "runs"
        / "entry_alpha"
        / "loss.json"
    )
    _write_json(
        loss_path,
        {
            "run_id": "run_v1",
            "entry_id": "entry_alpha",
            "per_judge_loss": from_index["judges"],
        },
    )
    from_file = phase1_client.get(url).json()
    assert from_file == from_index


def test_environment_workspace_is_structured(phase1_client: TestClient) -> None:
    """``/api/environment`` now nests workspace as an identity object."""
    r = phase1_client.get("/api/environment")
    assert r.status_code == 200
    ws = r.json().get("workspace")
    assert isinstance(ws, dict)
    assert ws.get("adk_entrypoint") == "kossel_run:root_agent"
    assert isinstance(ws.get("mutation_point_count"), int)


def test_endpoint_rejects_unsafe_ids(phase1_client: TestClient) -> None:
    """Path-traversal ids degrade rather than 500."""
    r = phase1_client.get("/api/generation/..%2Fbad/v1/per-judge")
    # Starlette may match the path with the dot segments; the handler
    # itself returns an empty payload for an unsafe id.
    assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Epoch DISPLAY ordering — fleet cards + sidebar + Overview table must be in
# chronological (created_at) order, NOT directory-name order.
# ---------------------------------------------------------------------------


@pytest.fixture
def timestamp_ordered_workspace(tmp_path: Path) -> Path:
    """A workspace whose epoch NAME order diverges from created_at order.

    Mirrors the live bug: an epoch named ``e2`` sorts FIRST alphabetically
    ('e' < 't') but was created LAST, while two ``t1_live*`` epochs sort
    after it by name yet precede it chronologically. Correct display order
    is by ``created_at`` — so ``e2`` (newest) comes LAST, not first.
    """
    ws = tmp_path / ".zicato"
    (ws / "epochs").mkdir(parents=True)
    _write(ws / "current_epoch", "e2")

    # (dir-name, created_at) — name order: e2, t1_live, t1_live2
    #                          time order: t1_live, t1_live2, e2
    for eid, created_at, goal in (
        ("t1_live", "2026-06-01T00:00:00Z", "First live tournament epoch."),
        ("t1_live2", "2026-06-02T00:00:00Z", "Second live tournament epoch."),
        ("e2", "2026-06-03T00:00:00Z", "Newest epoch — created last."),
    ):
        epoch_dir = ws / "epochs" / eid
        _write(epoch_dir / "brief.md", f"# brief\n\n## Goal\n\n{goal}\n")
        _write_json(
            epoch_dir / "config.json",
            {"contract_hash": "h", "closed": False, "goal": goal, "created_at": created_at},
        )
    return ws


def test_workspace_view_orders_epochs_by_timestamp_not_name(
    timestamp_ordered_workspace: Path,
) -> None:
    """Fleet cards + sidebar tree (both fed by build_workspace_view) order
    epochs chronologically. Against the old name-sort the alphabetically
    first ``e2`` led; it must now trail as the newest epoch.
    """
    view = build_workspace_view(WorkspacePaths(timestamp_ordered_workspace))
    chronological = ["t1_live", "t1_live2", "e2"]
    assert [r["epoch_id"] for r in view["epochs"]] == chronological
    # The flat sparkline is appended in the same iteration and must agree.
    assert [s["epoch_id"] for s in view["sparkline"]] == chronological


def test_epochs_summary_orders_epochs_by_timestamp_not_name(
    timestamp_ordered_workspace: Path,
) -> None:
    """The Overview epochs table (build_epochs_summary) orders chronologically,
    so the newest-but-alphabetically-first ``e2`` lands last.
    """
    summary = build_epochs_summary(WorkspacePaths(timestamp_ordered_workspace))
    assert [r["epoch_id"] for r in summary] == ["t1_live", "t1_live2", "e2"]
