"""Reader shapes for the visibility rating triple (``elo`` / ``elo_se`` /
``elo_games``).

The server-side join (DQ1 — the client never re-derives): the lineage/gens
feed (``build_lineage_view``) and the tournament standings
(``build_tournament_structure``) each attach the index-derived Bradley--Terry
rating to their rows, and ``elo_for_epoch`` / ``generations_for_epoch`` carry
``elo_se`` as an optional column. Everything is best-effort by contract
(DQ3): an absent / cold / pre-v10 index attaches the null triple — present
keys, ``None`` values (DQ2: one snake_case spelling on the wire) — and never
raises. The rating is visibility-only; nothing here feeds the gate.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zicato.query import WorkspacePaths, build_lineage_view, build_tournament_structure
from zicato.query.ratings import RATING_FIELDS, null_rating, rating_by_generation

EPOCH = "2026-06-01_e0"
TOURN = f"{EPOCH}:v0->v1"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _workspace(tmp_path: Path) -> Path:
    """A minimal two-generation workspace (directory-derived lineage)."""
    ws = tmp_path / ".zicato"
    _write_json(
        ws / "epochs" / EPOCH / "config.json",
        {"id": EPOCH, "created_at": "2026-06-01T00:00:00Z", "closed": False},
    )
    for gid, parent, decision in (("v0", None, None), ("v1", "v0", "promoted")):
        gdir = ws / "epochs" / EPOCH / "generations" / gid
        exp: dict[str, object] = {
            "generation_id": gid,
            "parent_generation_id": parent,
            "proposed_at": f"2026-06-01T00:0{0 if gid == 'v0' else 5}:00Z",
        }
        if decision:
            exp["outcome"] = {"decision": decision}
        _write_json(gdir / "experiment.json", exp)
    return ws


def _build_index(ws: Path, *, with_se_column: bool = True) -> None:
    """A hand-built index carrying rated generations + one structure row."""
    se_col = "elo_se REAL," if with_se_column else ""
    conn = sqlite3.connect(ws / "index.db")
    conn.executescript(
        f"""
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INTEGER, created_at TEXT,
            round_index INTEGER, elo REAL, {se_col} elo_games INTEGER,
            PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE tournaments(tournament_id TEXT PRIMARY KEY, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT,
            structure TEXT, structure_params_json TEXT, competitors_json TEXT,
            rounds_json TEXT, standings_json TEXT);
        """
    )
    if with_se_column:
        conn.executemany(
            "INSERT INTO generations VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z", 0, 1466.0, 122.5, 1),
                (EPOCH, "v1", "v0", 1, "2026-06-01T00:05:00Z", 0, 1534.0, 122.5, 1),
                # An unplayed leaf: rated NULL by the fold (zero settled duels).
                (EPOCH, "v2", "v1", 0, "2026-06-01T00:09:00Z", 1, None, None, None),
            ],
        )
    else:
        conn.executemany(
            "INSERT INTO generations VALUES(?,?,?,?,?,?,?,?)",
            [
                (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z", 0, 1466.0, 1),
                (EPOCH, "v1", "v0", 1, "2026-06-01T00:05:00Z", 0, 1534.0, 1),
            ],
        )
    standings = [
        {
            "generation_id": "v1",
            "rank": 1,
            "scalar": 0.4,
            "wins": 1,
            "losses": 0,
            "status": "champion",
        },
        {
            "generation_id": "v0",
            "rank": 2,
            "scalar": 0.5,
            "wins": 0,
            "losses": 1,
            "status": "eliminated",
        },
    ]
    rounds = [
        {
            "round_index": 0,
            "label": "Round 1",
            "matches": [
                {
                    "match_id": "r0m0",
                    "competitors": ["v0", "v1"],
                    "winner": "v1",
                    "decision": "promoted",
                    "delta_scalar": -0.1,
                    "bracket_slot": "",
                    "bye": False,
                },
            ],
        },
    ]
    conn.execute(
        "INSERT INTO tournaments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            TOURN,
            EPOCH,
            "v0",
            "v1",
            "promoted",
            0.5,
            0.4,
            -0.1,
            "",
            "2026-06-01T00:10:00Z",
            "swiss",
            "{}",
            json.dumps(
                [
                    {"generation_id": "v0", "seed": 1, "role": "champion"},
                    {"generation_id": "v1", "seed": 2, "role": "challenger"},
                ]
            ),
            json.dumps(rounds),
            json.dumps(standings),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# rating_by_generation — the shared best-effort join
# ---------------------------------------------------------------------------


def test_rating_map_reads_the_triple(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _build_index(ws)
    ratings = rating_by_generation(WorkspacePaths(ws), EPOCH)
    assert ratings[(EPOCH, "v1")] == {"elo": 1534.0, "elo_se": 122.5, "elo_games": 1}
    # The unplayed leaf reads present-but-null (NULL cells, not absence).
    assert ratings[(EPOCH, "v2")] == null_rating()


def test_rating_map_degrades_without_an_index(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)  # no index.db at all
    assert rating_by_generation(WorkspacePaths(ws), EPOCH) == {}


def test_rating_map_tolerates_a_pre_v12_index(tmp_path: Path) -> None:
    # elo/elo_games present, elo_se column absent (v10/v11): the SE reads
    # None; the older cells still surface.
    ws = _workspace(tmp_path)
    _build_index(ws, with_se_column=False)
    ratings = rating_by_generation(WorkspacePaths(ws), EPOCH)
    assert ratings[(EPOCH, "v1")] == {"elo": 1534.0, "elo_se": None, "elo_games": 1}


# ---------------------------------------------------------------------------
# elo_for_epoch / generations_for_epoch — the index selectors
# ---------------------------------------------------------------------------


def test_elo_for_epoch_carries_elo_se(tmp_path: Path) -> None:
    from zicato.index.query import elo_for_epoch  # noqa: PLC0415

    ws = _workspace(tmp_path)
    _build_index(ws)
    rows = {r["generation_id"]: r for r in elo_for_epoch(ws / "index.db", EPOCH)}
    assert rows["v1"]["elo_se"] == 122.5
    # Tolerant of NULL: the unplayed leaf reads present-but-null.
    assert rows["v2"]["elo_se"] is None


def test_elo_for_epoch_tolerates_a_pre_v12_index(tmp_path: Path) -> None:
    # The elo_se column is absent (v10/v11 index): the selector emits
    # NULL AS elo_se, so the field is present-but-null on every row.
    from zicato.index.query import elo_for_epoch  # noqa: PLC0415

    ws = _workspace(tmp_path)
    _build_index(ws, with_se_column=False)
    rows = elo_for_epoch(ws / "index.db", EPOCH)
    assert rows
    for r in rows:
        assert "elo_se" in r.keys()  # noqa: SIM118 — sqlite3.Row has no __contains__
        assert r["elo_se"] is None


def test_generations_for_epoch_carries_elo_se(tmp_path: Path) -> None:
    from zicato.index.query import generations_for_epoch  # noqa: PLC0415

    ws = _workspace(tmp_path)
    _build_index(ws)
    rows = {r["generation_id"]: r for r in generations_for_epoch(ws / "index.db", EPOCH)}
    assert rows["v1"]["elo_se"] == 122.5


# ---------------------------------------------------------------------------
# build_lineage_view — the gens feed
# ---------------------------------------------------------------------------


def test_lineage_nodes_carry_the_rating_triple(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _build_index(ws)
    view = build_lineage_view(WorkspacePaths(ws), EPOCH)
    nodes = {n["generation_id"]: n for n in view["generations"]}
    assert nodes["v1"]["elo"] == 1534.0
    assert nodes["v1"]["elo_se"] == 122.5
    assert nodes["v1"]["elo_games"] == 1
    assert nodes["v0"]["elo"] == 1466.0


def test_lineage_nodes_null_triple_without_an_index(tmp_path: Path) -> None:
    # DQ3: the index is absent — every node carries the PRESENT null triple
    # (keys on the wire, values null) and the reader never raises.
    ws = _workspace(tmp_path)
    view = build_lineage_view(WorkspacePaths(ws), EPOCH)
    assert view["generations"], "fixture lineage should not be empty"
    for node in view["generations"]:
        for field in RATING_FIELDS:
            assert field in node
            assert node[field] is None


# ---------------------------------------------------------------------------
# build_tournament_structure — the standings
# ---------------------------------------------------------------------------


def test_standings_carry_the_rating_triple(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _build_index(ws)
    st = build_tournament_structure(WorkspacePaths(ws), EPOCH, TOURN)
    assert st["source"] == "index"
    by_gid = {s["generation_id"]: s for s in st["standings"]}
    assert by_gid["v1"]["elo"] == 1534.0
    assert by_gid["v1"]["elo_se"] == 122.5
    assert by_gid["v1"]["elo_games"] == 1
    assert by_gid["v0"]["elo"] == 1466.0
    # The pre-existing standings fields are untouched by the enrichment.
    assert by_gid["v1"]["rank"] == 1
    assert by_gid["v1"]["scalar"] == 0.4


def test_standings_null_triple_on_a_cold_rating_fold(tmp_path: Path) -> None:
    # The structure row exists but the rating cells are NULL (a reindex that
    # predates any settled duel for these gens): present-but-null triple.
    ws = _workspace(tmp_path)
    _build_index(ws)
    conn = sqlite3.connect(ws / "index.db")
    conn.execute("UPDATE generations SET elo = NULL, elo_se = NULL, elo_games = NULL")
    conn.commit()
    conn.close()
    st = build_tournament_structure(WorkspacePaths(ws), EPOCH, TOURN)
    for s in st["standings"]:
        for field in RATING_FIELDS:
            assert field in s
            assert s[field] is None


def test_standings_null_triple_without_an_index(tmp_path: Path) -> None:
    # DQ3 end-to-end: no index at all — the structure degrades down the
    # resolution chain (loss_files) and its standings still carry the
    # present null triple; nothing raises.
    ws = _workspace(tmp_path)
    st = build_tournament_structure(WorkspacePaths(ws), EPOCH, TOURN)
    assert st["standings"], "loss-files reconstruction should produce standings"
    for s in st["standings"]:
        for field in RATING_FIELDS:
            assert field in s
            assert s[field] is None
