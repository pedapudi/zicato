"""The index's write statements against the schema they are derived from.

:mod:`zicato.index.ingest` builds every insert and upsert from a
:class:`zicato.index.schema.Table` descriptor rather than from a hand-written
column list. Two properties keep that safe, and each has a test here:

* Every writer supplies exactly the columns of the table it addresses, minus
  the ones it declares another writer owns. A column added to the DDL without
  a decision about each writer fails a test here rather than reaching a
  database.
* The three ways a re-ingest deliberately departs from "overwrite every
  column" survive. Each is load-bearing: without them a second projection of
  the same run erases a link the first one resolved, or nulls a column a
  different writer filled in.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from zicato.index import ingest
from zicato.index.schema import Table, apply_schema, table_columns

# Every descriptor the ingest path writes through, paired with the table it
# addresses. Two descriptors share ``tournaments``: one writes the
# per-challenger crowning duel, the other the whole round's field.
DESCRIPTORS: tuple[Table, ...] = (
    ingest._EPOCHS,
    ingest._GENERATIONS,
    ingest._RUNS,
    ingest._LOSS_PROFILES,
    ingest._JUDGE_LOSSES,
    ingest._METRIC_COUNTS,
    ingest._EXPERIMENTS,
    ingest._PATCHES,
    ingest._CROWNING_TOURNAMENTS,
    ingest._FIELD_TOURNAMENTS,
    ingest._REFLECTIONS,
    ingest._JUDGE_SCORECARDS,
    ingest._PARETO_FRONTIER,
    ingest._INGEST_CURSORS,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    """An empty in-memory index at the current schema version."""
    connection = sqlite3.connect(":memory:")
    apply_schema(connection)
    return connection


@pytest.mark.parametrize("table", DESCRIPTORS, ids=lambda t: t.name)
def test_every_descriptor_names_columns_the_table_has(table: Table) -> None:
    """Each column a descriptor singles out is a column of the table it addresses."""
    columns = set(table_columns(table.name))
    assert set(table.columns) == columns - set(table.written_elsewhere)
    for declared in (
        table.key,
        table.preserved_when_incoming_null,
        table.preserved_when_already_set,
        table.set_on_insert_only,
        table.written_elsewhere,
    ):
        assert set(declared) <= columns


def _keywords_per_descriptor() -> dict[str, list[set[str]]]:
    """Collect the keyword names every descriptor call in the ingest module supplies.

    Read off the source rather than by running the writers, so a writer no
    test happens to exercise is covered the same as one every test hits.
    """
    found: dict[str, list[set[str]]] = {}
    for node in ast.walk(ast.parse(Path(ingest.__file__).read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"bind", "upsert_row"}:
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        names = {kw.arg for kw in node.keywords if kw.arg is not None}
        found.setdefault(node.func.value.id, []).append(names)
    return found


@pytest.mark.parametrize("table", DESCRIPTORS, ids=lambda t: t.name)
def test_every_writer_supplies_exactly_its_descriptor_s_columns(table: Table) -> None:
    """A column added to the DDL fails here until each writer is taught to supply it.

    This is the safety argument for deriving the statements: a descriptor
    takes its columns from the DDL, so the one thing left that can fall out of
    step is the value each writer passes for them.
    """
    variable = next(name for name, value in vars(ingest).items() if value is table)
    call_sites = _keywords_per_descriptor().get(variable, [])
    assert call_sites, f"{variable} is declared but never written through"
    for keywords in call_sites:
        assert keywords == set(table.columns)


def test_the_generations_writer_leaves_the_ratings_columns_to_the_elo_fold() -> None:
    """The Elo triple is updated by :mod:`zicato.index.elo`, so ingest never writes it."""
    assert ingest._GENERATIONS.written_elsewhere == ("elo", "elo_se", "elo_games")
    assert "elo" not in ingest._GENERATIONS.insert


def test_the_field_tournament_writer_leaves_the_champion_eval_pair_alone() -> None:
    """A field-level row has no crowning duel, so it omits that duel's two columns."""
    assert ingest._FIELD_TOURNAMENTS.written_elsewhere == (
        "champion_eval_mode",
        "champion_run_ref",
    )
    assert "champion_eval_mode" not in ingest._FIELD_TOURNAMENTS.insert


def test_a_statement_rejects_values_that_do_not_name_its_columns() -> None:
    """Binding by keyword is what makes a forgotten column loud instead of silent."""
    with pytest.raises(KeyError):
        ingest._EPOCHS.bind(epoch_id="e1")


def test_a_reingest_keeps_a_link_it_can_no_longer_resolve(conn: sqlite3.Connection) -> None:
    """``COALESCE`` on re-ingest: a null second pass keeps the first pass's value.

    The tournament link and the match tag are resolved from files that a later
    projection may no longer be able to read. A null must leave the stored
    value standing, while every other column takes the new value.
    """
    ingest._upsert_run(
        conn,
        run_id="r1",
        epoch_id="e1",
        generation_id="v1",
        entry_id="t1",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        aborted=False,
        runtime_ms=10,
        tournament_id="e1:v0->v1",
        match_id="rung0_m2",
    )
    ingest._upsert_run(
        conn,
        run_id="r1",
        epoch_id="e1",
        generation_id="v1",
        entry_id="t1",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:09Z",
        aborted=True,
        runtime_ms=99,
        tournament_id=None,
        match_id=None,
    )
    row = conn.execute("SELECT * FROM runs WHERE run_id = 'r1'").fetchone()
    columns = table_columns("runs")
    values = dict(zip(columns, row, strict=True))
    assert values["tournament_id"] == "e1:v0->v1"
    assert values["match_id"] == "rung0_m2"
    assert values["runtime_ms"] == 99
    assert values["aborted"] == 1


def test_a_reingest_leaves_the_ratings_columns_where_the_fold_put_them(
    conn: sqlite3.Connection,
) -> None:
    """Columns another writer owns are untouched by a re-ingest of the row."""
    ingest._upsert_generation(
        conn,
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        promoted=False,
        created_at="2026-01-01T00:00:00Z",
        round_index=3,
    )
    conn.execute(
        "UPDATE generations SET elo = 1520.0, elo_se = 30.0, elo_games = 4 "
        "WHERE epoch_id = 'e1' AND generation_id = 'v1'"
    )
    # A second pass that knows the promotion but not the birth round.
    ingest._upsert_generation(
        conn,
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        promoted=True,
        created_at="2026-01-01T00:00:00Z",
        round_index=None,
    )
    row = conn.execute(
        "SELECT elo, elo_se, elo_games, round_index, promoted FROM generations"
    ).fetchone()
    assert row == (1520.0, 30.0, 4, 3, 1)


def test_a_field_row_never_clears_a_crowning_row_s_champion_provenance(
    conn: sqlite3.Connection,
) -> None:
    """The two ``tournaments`` writers do not overwrite each other's columns.

    Both keyed on ``tournament_id``, so the ids differ in practice. The
    invariant under test is the statement shape: the field writer's insert
    omits the champion-eval pair, so were the two ever to land on one id, the
    field row could not null what the crowning row recorded.
    """
    shared_id = "e1:v0->v1"
    conn.execute(
        "INSERT INTO tournaments(tournament_id, champion_eval_mode, champion_run_ref) "
        "VALUES(?, ?, ?)",
        (shared_id, "fast", "epochs/e1/generations/v0"),
    )
    ingest._upsert_field_tournament(
        conn,
        {
            "tournament_id": shared_id,
            "epoch_id": "e1",
            "competitors": ["v0", "v1", "v2"],
            "decision": "promoted",
            "structure": "swiss",
            "rounds": [],
            "standings": [],
            "field_status": [{"generation_id": "v1"}],
        },
    )
    row = conn.execute(
        "SELECT champion_eval_mode, champion_run_ref, structure FROM tournaments"
    ).fetchone()
    assert row == ("fast", "epochs/e1/generations/v0", "swiss")


def test_a_crowning_row_keeps_the_field_status_the_settled_round_wrote(
    conn: sqlite3.Connection,
) -> None:
    """``field_status_json`` is preserved once set, whichever writer set it.

    The per-experiment crowning record carries no proposing outcomes, so it
    writes an empty list. Reversing the ``COALESCE`` would erase the settled
    round's field status every time the experiment is re-projected.
    """
    from zicato.testing.fixtures import make_experiment, make_outcome_record

    experiment = make_experiment(
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        outcome=make_outcome_record(tournament_decision="promoted"),
    )
    ingest._upsert_tournament(conn, experiment)
    settled = '[{"generation_id": "v1", "status": "proposed"}]'
    conn.execute("UPDATE tournaments SET field_status_json = ?", (settled,))
    ingest._upsert_tournament(conn, experiment)
    assert conn.execute("SELECT field_status_json FROM tournaments").fetchone()[0] == settled
