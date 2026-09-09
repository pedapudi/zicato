"""Reader shapes for the visibility rating triple (``elo`` / ``elo_se`` /
``elo_games``).

The server-side join (DQ1 — the client never re-derives): the lineage/gens
feed (``build_lineage_view``) and the tournament standings
(``build_tournament_structure``) each attach the index-derived Bradley--Terry
rating to their rows, and ``elo_for_epoch`` / ``generations_for_epoch`` carry
``elo_se`` as a null field without independent measurement provenance.
Everything is best-effort by contract
(DQ3): an absent or incompatible index attaches the null triple — present
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
                epoch_id=EPOCH,
                parent_generation_id=parent,
                proposed_at=f"2026-06-01T00:0{0 if gid == 'v0' else 5}:00Z",
                decision=decision,
            ),
        )
    return layout


def _generation_rows() -> list[dict[str, Any]]:
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
    return rows


def _build_index(layout: WorkspaceLayout) -> None:
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
            "generations": _generation_rows(),
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
    )

    from tests._workspace_support import write_tournament_structure

    write_tournament_structure(
        layout.root,
        EPOCH,
        structure="swiss",
        competitors=[
            {"generation_id": "v0", "role": "champion"},
            {"generation_id": "v1", "role": "challenger"},
        ],
        rounds=rounds,
        standings=standings,
    )


# ---------------------------------------------------------------------------
# rating_by_generation — the shared best-effort join
# ---------------------------------------------------------------------------


def test_rating_map_reads_the_triple(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)
    _build_index(layout)
    ratings = rating_by_generation(WorkspacePaths(layout.root), EPOCH)
    assert ratings[(EPOCH, "v1")] == {"elo": 1534.0, "elo_se": None, "elo_games": 1}
    # The unplayed leaf reads present-but-null (NULL cells, not absence).
    assert ratings[(EPOCH, "v2")] == null_rating()


def test_rating_map_degrades_without_an_index(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)  # no index.db at all
    assert rating_by_generation(WorkspacePaths(layout.root), EPOCH) == {}


# ---------------------------------------------------------------------------
# elo_for_epoch / generations_for_epoch — the index selectors
# ---------------------------------------------------------------------------


def test_elo_for_epoch_suppresses_unproven_uncertainty(tmp_path: Path) -> None:
    from zicato.index.query import elo_for_epoch  # noqa: PLC0415

    layout = _workspace(tmp_path)
    _build_index(layout)
    rows = {r["generation_id"]: r for r in elo_for_epoch(layout.index_db_path, EPOCH)}
    assert rows["v1"]["elo_se"] is None
    # Tolerant of NULL: the unplayed leaf reads present-but-null.
    assert rows["v2"]["elo_se"] is None


def test_generations_for_epoch_suppresses_unproven_uncertainty(tmp_path: Path) -> None:
    from zicato.index.query import generations_for_epoch  # noqa: PLC0415

    layout = _workspace(tmp_path)
    _build_index(layout)
    rows = {r["generation_id"]: r for r in generations_for_epoch(layout.index_db_path, EPOCH)}
    assert rows["v1"]["elo_se"] is None


# ---------------------------------------------------------------------------
# build_lineage_view — the gens feed
# ---------------------------------------------------------------------------


def test_lineage_nodes_carry_the_rating_triple(tmp_path: Path) -> None:
    layout = _workspace(tmp_path)
    _build_index(layout)
    view = build_lineage_view(WorkspacePaths(layout.root), EPOCH)
    nodes = {n["generation_id"]: n for n in view["generations"]}
    assert nodes["v1"]["elo"] == 1534.0
    assert nodes["v1"]["elo_se"] is None
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
    assert st["source"] == "record"
    by_gid = {s["generation_id"]: s for s in st["standings"]}
    assert by_gid["v1"]["elo"] == 1534.0
    assert by_gid["v1"]["elo_se"] is None
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
    from tests._console_scenarios import CONSOLE_EPOCH, build_racing_round_settled_workspace

    root = build_racing_round_settled_workspace(tmp_path)
    WorkspaceLayout.from_root(root).index_db_path.unlink()
    st = build_tournament_structure(
        WorkspacePaths(root), CONSOLE_EPOCH, f"{CONSOLE_EPOCH}:field:v1"
    )
    assert st["source"] == "record"
    assert st["standings"]
    for s in st["standings"]:
        for field in RATING_FIELDS:
            assert field in s
            assert s[field] is None
