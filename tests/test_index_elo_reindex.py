"""End-to-end: ``rebuild_index`` populates the read-only Elo columns.

The Elo analytics fold (FUNCTIONALITY-RECOMMENDATIONS.md §5) is wired
into the rebuild path AFTER the tournaments are ingested. These tests
build a small real workspace (lineage + per-generation experiments with
resolved gauntlet outcomes), run ``rebuild_index``, and assert the
``generations.elo`` / ``generations.elo_games`` columns are populated and
ordered (a consistently-promoted lineage rises). Elo is read-only — it
never gates promotion; the rest of the index is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from zicato.core.types import Generation, ScoringWeights
from zicato.epoch.journal import write_experiment
from zicato.epoch.lifecycle import new_epoch
from zicato.epoch.lineage import append_to_lineage
from zicato.index.ingest import rebuild_index
from zicato.index.query import elo_for_epoch, generations_for_epoch
from zicato.testing.fixtures import make_experiment, make_outcome_record


def _board_and_rubric(tmp_path: Path) -> tuple[Path, Path]:
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", ' '"wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    return board, rubric


def _gen(epoch_id: str, gid: str, parent: str | None, created: str, promoted: bool) -> Generation:
    return Generation(
        id=gid,
        epoch_id=epoch_id,
        parent_id=parent,
        snapshot_root=Path(f"/tmp/snap/{gid}"),
        created_at=created,
        promoted=promoted,
    )


def _build_chain_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A four-generation promoted chain v0 -> v1 -> v2 -> v3.

    Each non-seed child has a resolved experiment whose gauntlet outcome is
    ``promoted`` (the child beats the incumbent). The Elo fold should rank
    the lineage strictly increasing: each promotion is a win over the prior
    champion, so a later generation seeds from (and then beats) its parent.
    """
    ws = tmp_path / ".zicato"
    board, rubric = _board_and_rubric(tmp_path)
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    eid = cfg.id

    chain = ["v0", "v1", "v2", "v3"]
    append_to_lineage(ws, eid, _gen(eid, "v0", None, "2026-01-01T00:00:00Z", True), None)
    for i in range(1, len(chain)):
        child, parent = chain[i], chain[i - 1]
        append_to_lineage(
            ws,
            eid,
            _gen(eid, child, parent, f"2026-01-0{i + 1}T00:00:00Z", True),
            parent,
        )
        exp = make_experiment(
            epoch_id=eid,
            generation_id=child,
            parent_generation_id=parent,
            outcome=make_outcome_record(
                tournament_decision="promoted",
                scalar_score_delta=0.2,
                ran_at=f"2026-01-0{i + 1}T01:00:00Z",
            ),
        )
        write_experiment(ws, eid, child, exp)
    return ws, eid


def test_reindex_populates_elo_columns(tmp_path: Path) -> None:
    ws, eid = _build_chain_workspace(tmp_path)
    db = rebuild_index(ws)

    rows = {r["generation_id"]: r for r in elo_for_epoch(db, eid)}
    assert set(rows) == {"v0", "v1", "v2", "v3"}

    # Every generation has a non-null rating (the seed v0 via its prior, the
    # children via their games).
    for gid in ("v0", "v1", "v2", "v3"):
        assert rows[gid]["elo"] is not None

    # Each promotion is one game for both sides.
    # v0 plays only its child's promotion duel; v3 plays only its own.
    assert rows["v0"]["elo_games"] == 1  # lost to v1
    assert rows["v1"]["elo_games"] == 2  # beat v0, lost to v2
    assert rows["v2"]["elo_games"] == 2  # beat v1, lost to v3
    assert rows["v3"]["elo_games"] == 1  # beat v2


def test_reindex_elo_ordering_rewards_the_consistent_winner(tmp_path: Path) -> None:
    ws, eid = _build_chain_workspace(tmp_path)
    db = rebuild_index(ws)
    rows = {r["generation_id"]: float(r["elo"]) for r in elo_for_epoch(db, eid)}
    # A consistently-promoted lineage rises: the latest champion outranks the
    # generation it deposed, which outranks the original seed.
    assert rows["v3"] > rows["v0"]
    assert rows["v1"] > rows["v0"]


def test_reindex_elo_is_idempotent(tmp_path: Path) -> None:
    ws, eid = _build_chain_workspace(tmp_path)
    db = rebuild_index(ws)
    first = {r["generation_id"]: (r["elo"], r["elo_games"]) for r in elo_for_epoch(db, eid)}
    rebuild_index(ws)
    second = {r["generation_id"]: (r["elo"], r["elo_games"]) for r in elo_for_epoch(db, eid)}
    assert first == second


def test_generations_for_epoch_surfaces_elo_columns(tmp_path: Path) -> None:
    # The lineage selector also carries the additive elo columns so a
    # dashboard lineage view can show a strength number per generation.
    ws, eid = _build_chain_workspace(tmp_path)
    db = rebuild_index(ws)
    gens = generations_for_epoch(db, eid)
    assert gens
    for row in gens:
        keys = row.keys()
        assert "elo" in keys
        assert "elo_games" in keys


def test_empty_workspace_reindex_has_no_elo_rows(tmp_path: Path) -> None:
    # A never-run workspace folds cleanly to zero ratings (no tournaments,
    # no games) — the fold must not raise on an empty ledger.
    ws = tmp_path / ".zicato"
    ws.mkdir()
    db = rebuild_index(ws)
    assert elo_for_epoch(db, "nope") == []
