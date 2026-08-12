"""The bulk per-generation scalar read — equivalence + no-N+1 pins.

Three readers walk every generation in the workspace and each used to issue one
``loss_profiles`` query per generation: ``build_score_trajectory`` (served on
``/api/environment``, which the client refetches on every SSE beat),
``build_workspace_view`` and the meta-loop ledger (both served on
``/api/workspace``). ``_mean_drift_loss_by_generation`` replaces all of them
with ONE read.

The load-bearing risk is the aggregate itself. ``_mean_drift_loss_per_generation``
is a TWO-STAGE fold — per board entry, average that entry's ``drift_loss``
across every run of it; then take the mean of those per-entry means — because a
generation is re-scored whenever it serves as a later round's champion, so the
index holds several rows for one ``(generation_id, entry_id)`` pair. A flat
``AVG(drift_loss)`` over the rows would silently shift every scalar on every
chart, and it is INDISTINGUISHABLE from the correct fold unless the fixture
holds a generation with an UNEVEN number of runs per entry.

The existing reader-parity fixture writes exactly one run per
``(generation, entry)``, where the flat mean and the two-stage fold agree, so it
cannot catch that bug. Hence the fixture here: ``g_rescored`` gives entry ``t1``
three runs and entry ``t2`` one, with values chosen so the two folds differ.
``test_flat_average_would_be_wrong`` asserts that difference, so the fixture is
proven able to fail before any equivalence claim rests on it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zicato.query.gate_view import (
    _mean_drift_loss_by_generation,
    _mean_drift_loss_per_generation,
)

EPOCH = "e0"
OTHER_EPOCH = "e1"

#: (generation_id, entry_id, drift_loss) rows. ``g_rescored`` is the case the
#: two-stage fold exists for: t1 ran three times, t2 once.
#:
#:   two-stage:  t1 mean = (0.9 + 0.3 + 0.3)/3 = 0.5 ; t2 mean = 0.1
#:               scalar  = (0.5 + 0.1)/2 = 0.30
#:   flat mean:  (0.9 + 0.3 + 0.3 + 0.1)/4      = 0.40   <- would be WRONG
_ROWS: tuple[tuple[str, str, float | None], ...] = (
    # a plain generation: one run per entry (the folds agree here)
    ("g0", "t1", 0.40),
    ("g0", "t2", 0.20),
    # the re-scored generation: UNEVEN runs per entry (the folds diverge)
    ("g_rescored", "t1", 0.90),
    ("g_rescored", "t1", 0.30),
    ("g_rescored", "t1", 0.30),
    ("g_rescored", "t2", 0.10),
    # a generation whose only row carries a NULL drift_loss -> (None, 0)
    ("g_null", "t1", None),
    # a generation with a NULL row AND a real one: the NULL is skipped, so the
    # entry count is 1, never 2.
    ("g_mixed", "t1", 0.50),
    ("g_mixed", "t2", None),
)

TWO_STAGE_RESCORED = 0.30
FLAT_MEAN_RESCORED = 0.40


@pytest.fixture
def index_db(tmp_path: Path) -> Path:
    """A minimal index carrying only what the scalar fold reads."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE loss_profiles(run_id TEXT PRIMARY KEY, epoch_id TEXT,
            generation_id TEXT, entry_id TEXT, drift_loss REAL, pass_fail INTEGER,
            runtime_ms INTEGER, wall_clock_budget_exceeded INTEGER, loss_json TEXT,
            tournament_id TEXT);
        CREATE INDEX idx_loss_gen ON loss_profiles(epoch_id, generation_id);
        CREATE TABLE epochs(epoch_id TEXT PRIMARY KEY, contract_hash TEXT,
            created_at TEXT, closed INTEGER, goal TEXT, parent_epoch_id TEXT);
        """
    )
    for i, (gid, entry, loss) in enumerate(_ROWS):
        conn.execute(
            "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"run{i}", EPOCH, gid, entry, loss, 1, 100, 0, "{}", None),
        )
    # A second epoch reusing the SAME generation id — the bulk mapping must key
    # on (epoch_id, generation_id), never on generation_id alone.
    conn.execute(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("run_other", OTHER_EPOCH, "g0", "t1", 0.77, 1, 100, 0, "{}", None),
    )
    # A row with a NULL epoch_id: the per-generation query matches with
    # ``epoch_id = ?`` and SQL equality never matches NULL, so this row is
    # invisible there and must stay invisible in the bulk read.
    conn.execute(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("run_null_epoch", None, "g_orphan", "t1", 0.99, 1, 100, 0, "{}", None),
    )
    conn.commit()
    conn.close()
    return db


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


ALL_GENS = ("g0", "g_rescored", "g_null", "g_mixed", "g_orphan", "g_absent")


def test_flat_average_would_be_wrong(index_db: Path) -> None:
    """The fixture CAN fail: a flat row mean disagrees with the two-stage fold.

    Without this, `test_bulk_matches_per_generation` would pass against a
    naive ``AVG(drift_loss)`` implementation and prove nothing.
    """
    with _open(index_db) as conn:
        flat = conn.execute(
            "SELECT AVG(drift_loss) FROM loss_profiles "
            "WHERE epoch_id = ? AND generation_id = ? AND drift_loss IS NOT NULL",
            (EPOCH, "g_rescored"),
        ).fetchone()[0]
        scalar, entries = _mean_drift_loss_per_generation(conn, EPOCH, "g_rescored")

    assert scalar == pytest.approx(TWO_STAGE_RESCORED)
    assert flat == pytest.approx(FLAT_MEAN_RESCORED)
    assert scalar != pytest.approx(flat), (
        "fixture cannot distinguish the two-stage fold from a flat mean — "
        "the equivalence test below would be vacuous"
    )
    assert entries == 2, "entry_count counts DISTINCT entries, not rows"


def test_bulk_matches_per_generation(index_db: Path) -> None:
    """The bulk read returns EXACTLY what the per-generation query returns.

    Exact equality, not approx: the bulk path folds in Python reusing the same
    helper, so a float difference would mean the accumulation order drifted.
    """
    with _open(index_db) as conn:
        bulk = _mean_drift_loss_by_generation(conn)
        for epoch in (EPOCH, OTHER_EPOCH):
            for gid in ALL_GENS:
                expected = _mean_drift_loss_per_generation(conn, epoch, gid)
                assert (
                    bulk.get((epoch, gid), (None, 0)) == expected
                ), f"bulk disagrees with per-generation for ({epoch}, {gid})"


def test_bulk_skips_null_epoch_rows(index_db: Path) -> None:
    """A NULL ``epoch_id`` row is invisible to both paths."""
    with _open(index_db) as conn:
        bulk = _mean_drift_loss_by_generation(conn)
        assert _mean_drift_loss_per_generation(conn, None, "g_orphan") == (None, 0)
    assert not any(key[0] is None for key in bulk), "a NULL epoch_id leaked into the mapping"


def test_bulk_keys_on_epoch_and_generation(index_db: Path) -> None:
    """The same generation id under two epochs stays two distinct entries."""
    with _open(index_db) as conn:
        bulk = _mean_drift_loss_by_generation(conn)
    assert bulk[(EPOCH, "g0")][0] == pytest.approx(0.30)  # (0.40 + 0.20) / 2
    assert bulk[(OTHER_EPOCH, "g0")][0] == pytest.approx(0.77)


def test_null_only_generation_is_absent_not_zero(index_db: Path) -> None:
    """A generation whose only row is NULL must read ``(None, 0)``, never 0.0."""
    with _open(index_db) as conn:
        bulk = _mean_drift_loss_by_generation(conn)
    assert bulk.get((EPOCH, "g_null"), (None, 0)) == (None, 0)
    # and a mixed generation counts only the non-NULL entry
    assert bulk[(EPOCH, "g_mixed")] == (0.50, 1)


def test_bulk_read_is_one_query_regardless_of_generation_count(tmp_path: Path) -> None:
    """The N+1 pin: statement count must NOT grow with the generation count.

    This is the regression that reopens the performance bug — a future edit that
    moves the scalar read back inside a per-generation loop fails here.
    """
    db = tmp_path / "many.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE loss_profiles(run_id TEXT PRIMARY KEY, epoch_id TEXT,
            generation_id TEXT, entry_id TEXT, drift_loss REAL, pass_fail INTEGER,
            runtime_ms INTEGER, wall_clock_budget_exceeded INTEGER, loss_json TEXT,
            tournament_id TEXT);
        CREATE INDEX idx_loss_gen ON loss_profiles(epoch_id, generation_id);
        """
    )
    n_gens = 400
    conn.executemany(
        "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(f"r{i}", EPOCH, f"g{i}", "t1", 0.5, 1, 100, 0, "{}", None) for i in range(n_gens)],
    )
    conn.commit()
    conn.close()

    statements: list[str] = []
    ro = _open(db)
    ro.set_trace_callback(statements.append)
    try:
        bulk = _mean_drift_loss_by_generation(ro)
    finally:
        ro.set_trace_callback(None)
        ro.close()

    assert len(bulk) == n_gens, "every generation must still be present"
    assert len(statements) == 1, (
        f"expected ONE statement for {n_gens} generations, got {len(statements)} — "
        "the per-generation N+1 is back"
    )
