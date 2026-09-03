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
from typing import Any

from tests._workspace_support import (
    experiment_record,
    seed_index,
    workspace,
    write_epoch,
    write_generation,
)
from zicato.query import WorkspacePaths, build_lineage_view, build_tournament_structure
from zicato.query.ratings import RATING_FIELDS, null_rating, rating_by_generation
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-06-01_e0"
TOURN = f"{EPOCH}:v0->v1"


def _workspace(tmp_path: Path) -> WorkspaceLayout:
    """A minimal two-generation workspace (directory-derived lineage)."""
    layout = workspace(tmp_path)
    write_epoch(
        layout,
        EPOCH,
        config={"id": EPOCH, "created_at": "2026-06-01T00:00:00Z", "closed": False},
    )
    for gid, parent, decision in (("v0", None, None), ("v1", "v0", "promoted")):
        write_generation(
            layout,
            EPOCH,
            gid,
            experiment=experiment_record(
                gid,
                parent_generation_id=parent,
                proposed_at=f"2026-06-01T00:0{0 if gid == 'v0' else 5}:00Z",
                decision=decision,
            ),
        )
    return layout


def _generation_rows(*, with_se_column: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "epoch_id": EPOCH,
            "generation_id": "v0",
            "parent_generation_id": None,
            "promoted": 1,
            "created_at": "2026-06-01T00:00:00Z",
            "round_index": 0,
            "elo": 1466.0,
            "elo_se": 122.5,
            "elo_games": 1,
        },
        {
            "epoch_id": EPOCH,
            "generation_id": "v1",
            "parent_generation_id": "v0",
            "promoted": 1,
            "created_at": "2026-06-01T00:05:00Z",
            "round_index": 0,
            "elo": 1534.0,
            "elo_se": 122.5,
            "elo_games": 1,
        },
        # An unplayed leaf: rated NULL by the fold (zero settled duels).
        {
            "epoch_id": EPOCH,
            "generation_id": "v2",
            "parent_generation_id": "v1",
            "promoted": 0,
            "created_at": "2026-06-01T00:09:00Z",
            "round_index": 1,
            "elo": None,
            "elo_se": None,
            "elo_games": None,
        },
    ]
    if with_se_column:
        return rows
    # A pre-v12 index has no elo_se column at all, and the unplayed leaf
    # postdates it, so the older fixture carries the two rated rows only.
    return [{k: v for k, v in row.items() if k != "elo_se"} for row in rows[:2]]


def _build_index(layout: WorkspaceLayout, *, with_se_column: bool = True) -> None:
    """A real-schema index carrying rated generations + one structure row."""
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
    seed_index(
        layout,
        {
            "generations": _generation_rows(with_se_column=with_se_column),
            "tournaments": [
                {
                    "tournament_id": TOURN,
                    "epoch_id": EPOCH,
                    "parent_generation_id": "v0",
                    "child_generation_id": "v1",
                    "decision": "promoted",
                    "parent_scalar": 0.5,
                    "child_scalar": 0.4,
                    "delta_scalar": -0.1,
                    "rejection_reason": "",
                    "ran_at": "2026-06-01T00:10:00Z",
                    "structure": "swiss",
                    "structure_params_json": "{}",
                    "competitors_json": json.dumps(
                        [
                            {"generation_id": "v0", "seed": 1, "role": "champion"},
                            {"generation_id": "v1", "seed": 2, "role": "challenger"},
                        ]
                    ),
                    "rounds_json": json.dumps(rounds),
                    "standings_json": json.dumps(standings),
                }
            ],
        },
        without_columns=() if with_se_column else (("generations", "elo_se"),),
    )


# ---------------------------------------------------------------------------
# rating_by_generation — the shared best-effort join
# ---------------------------------------------------------------------------


def test_rating_map_reads_the_triple(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)
    _build_index(layout)
    ratings = rating_by_generation(WorkspacePaths(layout.root), EPOCH)
    assert ratings[(EPOCH, "v1")] == {"elo": 1534.0, "elo_se": 122.5, "elo_games": 1}
    # The unplayed leaf reads present-but-null (NULL cells, not absence).
    assert ratings[(EPOCH, "v2")] == null_rating()


def test_rating_map_degrades_without_an_index(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)  # no index.db at all
    assert rating_by_generation(WorkspacePaths(layout.root), EPOCH) == {}


def test_rating_map_tolerates_a_pre_v12_index(tmp_path: Path) -> None:
    # elo/elo_games present, elo_se column absent (v10/v11): the SE reads
    # None; the older cells still surface.
    layout = _workspace(tmp_path)
    _build_index(layout, with_se_column=False)
    ratings = rating_by_generation(WorkspacePaths(layout.root), EPOCH)
    assert ratings[(EPOCH, "v1")] == {"elo": 1534.0, "elo_se": None, "elo_games": 1}


# ---------------------------------------------------------------------------
# elo_for_epoch / generations_for_epoch — the index selectors
# ---------------------------------------------------------------------------


def test_elo_for_epoch_carries_elo_se(tmp_path: Path) -> None:
    from zicato.index.query import elo_for_epoch  # noqa: PLC0415

    layout = _workspace(tmp_path)
    _build_index(layout)
    rows = {r["generation_id"]: r for r in elo_for_epoch(layout.index_db_path, EPOCH)}
    assert rows["v1"]["elo_se"] == 122.5
    # Tolerant of NULL: the unplayed leaf reads present-but-null.
    assert rows["v2"]["elo_se"] is None


def test_elo_for_epoch_tolerates_a_pre_v12_index(tmp_path: Path) -> None:
    # The elo_se column is absent (v10/v11 index): the selector emits
    # NULL AS elo_se, so the field is present-but-null on every row.
    from zicato.index.query import elo_for_epoch  # noqa: PLC0415

    layout = _workspace(tmp_path)
    _build_index(layout, with_se_column=False)
    rows = elo_for_epoch(layout.index_db_path, EPOCH)
    assert rows
    for r in rows:
        assert "elo_se" in r.keys()  # noqa: SIM118 — sqlite3.Row has no __contains__
        assert r["elo_se"] is None


def test_generations_for_epoch_carries_elo_se(tmp_path: Path) -> None:
    from zicato.index.query import generations_for_epoch  # noqa: PLC0415

    layout = _workspace(tmp_path)
    _build_index(layout)
    rows = {r["generation_id"]: r for r in generations_for_epoch(layout.index_db_path, EPOCH)}
    assert rows["v1"]["elo_se"] == 122.5


# ---------------------------------------------------------------------------
# build_lineage_view — the gens feed
# ---------------------------------------------------------------------------


def test_lineage_nodes_carry_the_rating_triple(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)
    _build_index(layout)
    view = build_lineage_view(WorkspacePaths(layout.root), EPOCH)
    nodes = {n["generation_id"]: n for n in view["generations"]}
    assert nodes["v1"]["elo"] == 1534.0
    assert nodes["v1"]["elo_se"] == 122.5
    assert nodes["v1"]["elo_games"] == 1
    assert nodes["v0"]["elo"] == 1466.0


def test_lineage_nodes_null_triple_without_an_index(tmp_path: Path) -> None:
    # DQ3: the index is absent — every node carries the PRESENT null triple
    # (keys on the wire, values null) and the reader never raises.
    layout = _workspace(tmp_path)
    view = build_lineage_view(WorkspacePaths(layout.root), EPOCH)
    assert view["generations"], "fixture lineage should not be empty"
    for node in view["generations"]:
        for field in RATING_FIELDS:
            assert field in node
            assert node[field] is None


# ---------------------------------------------------------------------------
# build_tournament_structure — the standings
# ---------------------------------------------------------------------------


def test_standings_carry_the_rating_triple(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)
    _build_index(layout)
    st = build_tournament_structure(WorkspacePaths(layout.root), EPOCH, TOURN)
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
    layout = _workspace(tmp_path)
    _build_index(layout)
    conn = sqlite3.connect(layout.index_db_path)
    conn.execute("UPDATE generations SET elo = NULL, elo_se = NULL, elo_games = NULL")
    conn.commit()
    conn.close()
    st = build_tournament_structure(WorkspacePaths(layout.root), EPOCH, TOURN)
    for s in st["standings"]:
        for field in RATING_FIELDS:
            assert field in s
            assert s[field] is None


def test_standings_null_triple_without_an_index(tmp_path: Path) -> None:
    # DQ3 end-to-end: no index at all — the structure degrades down the
    # resolution chain (loss_files) and its standings still carry the
    # present null triple; nothing raises.
    layout = _workspace(tmp_path)
    st = build_tournament_structure(WorkspacePaths(layout.root), EPOCH, TOURN)
    assert st["standings"], "loss-files reconstruction should produce standings"
    for s in st["standings"]:
        for field in RATING_FIELDS:
            assert field in s
            assert s[field] is None
