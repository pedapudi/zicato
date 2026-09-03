"""Composable builders for the ``.zicato/`` inputs read-side tests fabricate.

A reader test needs a workspace tree, and often an analytical index, before
it can assert anything. Written by hand, each such fixture re-spells two
things the production code declares exactly once: where a file lives
(``epochs/<id>/generations/<gen>/runs/<entry>/loss.json`` and its siblings,
declared by :class:`zicato.workspace.WorkspaceLayout`) and what columns the
index carries (declared by :mod:`zicato.index.schema`). A re-spelling cannot
be updated by the change that moves the file or adds the column, so it drifts
silently and the reader keeps passing against a fixture that no longer
resembles what the writers produce.

The builders here take those two facts from their one authority. Every path
comes off a :class:`~zicato.workspace.WorkspaceLayout`; every table comes
from :func:`zicato.index.schema.apply_schema`, and every column name a
seeded row supplies is checked against
:func:`zicato.index.schema.table_columns` before the insert runs.

The default for a new read-side test
------------------------------------

Compose these builders. Do not hand-write ``CREATE TABLE`` for the current
schema, and do not join ``"epochs"`` or any other layout segment into a path
literal — ask the layout for it. A test that needs an index which PREDATES a
column states that as :func:`seed_index`'s ``without_columns``, so the
fixture is the real schema minus the named column rather than a second,
unmaintained copy of the DDL.

Two cases legitimately fall outside this. A test whose SUBJECT is the schema
migration itself writes the historical DDL it migrates from, because that
DDL no longer exists anywhere else. A test whose subject is one file's exact
bytes writes those bytes directly.

The builders are deliberately shallow: each writes what it is given and
supplies a minimal default only where every caller wants the same one. A
test whose subject is one of these values passes it explicitly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from zicato.index.schema import apply_schema, table_columns
from zicato.workspace import WorkspaceLayout
from zicato.workspace.config_io import CONFIG_FILENAME

#: The creation timestamp an epoch gets when a test does not care which.
DEFAULT_CREATED_AT = "2026-06-01T00:00:00Z"

# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------


def write_json(path: Path, data: Any, *, indent: int | None = None) -> Path:
    """Write ``data`` as JSON, creating parent directories on demand.

    Compact by default. Pass ``indent`` when the file's exact bytes are
    themselves pinned by a golden.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    """Write ``text`` verbatim, creating parent directories on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """Write one compact JSON object per line, newline-terminated."""
    body = "".join(f"{json.dumps(row)}\n" for row in rows)
    return write_text(path, body)


# ---------------------------------------------------------------------------
# Workspace tree
# ---------------------------------------------------------------------------


def workspace(tmp_path: Path, *, name: str = ".zicato") -> WorkspaceLayout:
    """Create an empty workspace root under ``tmp_path`` and return its layout.

    ``name`` is the root directory's name; the default matches what an
    initialised project carries. Every other builder here takes the returned
    layout, so a test never needs the root path's spelling.
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return WorkspaceLayout.from_root(root)


def write_workspace_config(
    layout: WorkspaceLayout, config: Mapping[str, Any], *, indent: int | None = None
) -> Path:
    """Write the workspace-level ``config.json`` (adapter entrypoint and knobs)."""
    return write_json(layout.root / CONFIG_FILENAME, dict(config), indent=indent)


def write_lineage(
    layout: WorkspaceLayout, lineage: Mapping[str, Any], *, indent: int | None = None
) -> Path:
    """Write the workspace-level ``lineage.json`` cross-epoch generation record."""
    return write_json(layout.lineage_path, dict(lineage), indent=indent)


def write_epoch(
    layout: WorkspaceLayout,
    epoch_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    board: Sequence[Any] | None = None,
    scoring: Mapping[str, Any] | None = None,
    brief: str | None = None,
    contract_components: Mapping[str, Any] | None = None,
    current: bool = False,
    indent: int | None = None,
) -> Path:
    """Write one epoch's directory and return it.

    ``config`` is written verbatim when given; omitted, it defaults to the
    id, :data:`DEFAULT_CREATED_AT`, and an open epoch. Every other artifact
    is written only when supplied, so the tree holds what the test's readers
    actually open and nothing else. ``current`` also points the workspace's
    ``current_epoch`` marker at this epoch.
    """
    directory = layout.epoch_dir(epoch_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(
        layout.epoch_config(epoch_id),
        dict(config)
        if config is not None
        else {"id": epoch_id, "created_at": DEFAULT_CREATED_AT, "closed": False},
        indent=indent,
    )
    if board is not None:
        write_jsonl(layout.board(epoch_id), board)
    if scoring is not None:
        write_json(layout.scoring(epoch_id), dict(scoring), indent=indent)
    if brief is not None:
        write_text(layout.brief(epoch_id), brief)
    if contract_components is not None:
        write_json(layout.contract_components(epoch_id), dict(contract_components), indent=indent)
    if current:
        set_current_epoch(layout, epoch_id)
    return directory


def set_current_epoch(layout: WorkspaceLayout, epoch_id: str, *, newline: bool = False) -> Path:
    """Point the workspace's ``current_epoch`` marker at one epoch.

    ``newline`` terminates the marker, which some writers do and some do
    not; readers strip either way.
    """
    return write_text(layout.current_epoch_marker, f"{epoch_id}\n" if newline else epoch_id)


def experiment_record(
    generation_id: str,
    *,
    parent_generation_id: str | None = None,
    proposed_at: str = DEFAULT_CREATED_AT,
    decision: str | None = None,
    outcome: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The ``experiment.json`` body for one generation.

    The outcome block is omitted entirely for a generation that has not
    settled, which is how the writers leave an in-flight round. Pass
    ``decision`` for the common settled case, or ``outcome`` for a full
    block; ``extra`` adds any further top-level key. The outcome lands last,
    after ``extra``, mirroring the order the writers produce.
    """
    record: dict[str, Any] = {
        "generation_id": generation_id,
        "parent_generation_id": parent_generation_id,
        "proposed_at": proposed_at,
    }
    record.update(extra)
    if outcome is not None:
        record["outcome"] = dict(outcome)
    elif decision is not None:
        record["outcome"] = {"decision": decision}
    return record


def write_generation(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    *,
    experiment: Mapping[str, Any] | None = None,
    gen_score: Mapping[str, Any] | None = None,
    indent: int | None = None,
) -> Path:
    """Write one generation's directory and return it.

    ``experiment`` defaults to :func:`experiment_record` for a parentless,
    unsettled generation. ``gen_score`` is written only when supplied.
    """
    directory = layout.generation_dir(epoch_id, generation_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(
        layout.experiment(epoch_id, generation_id),
        dict(experiment) if experiment is not None else experiment_record(generation_id),
        indent=indent,
    )
    if gen_score is not None:
        write_json(layout.gen_score(epoch_id, generation_id), dict(gen_score), indent=indent)
    return directory


def write_run(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    loss: Mapping[str, Any] | None = None,
    events: Sequence[Any] | None = None,
    indent: int | None = None,
) -> Path:
    """Write one board entry's run directory and return it.

    Each artifact is written only when supplied: a run whose events were
    never captured has no ``events.jsonl``, and a reader that tolerates
    that absence should be tested against a tree that reproduces it.
    """
    directory = layout.run_dir(epoch_id, generation_id, entry_id)
    directory.mkdir(parents=True, exist_ok=True)
    if loss is not None:
        write_json(layout.loss(epoch_id, generation_id, entry_id), dict(loss), indent=indent)
    if events is not None:
        write_jsonl(layout.events(epoch_id, generation_id, entry_id), events)
    return directory


# ---------------------------------------------------------------------------
# Analytical index
# ---------------------------------------------------------------------------


def seed_index(
    layout: WorkspaceLayout,
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    without_columns: Sequence[tuple[str, str]] = (),
) -> Path:
    """Build the workspace's ``index.db`` at the current schema and fill it.

    ``tables`` maps a table name to the rows to insert, each row a mapping
    of column name to value. Unnamed columns stay ``NULL``, which is what a
    writer that never learned a value leaves behind. A column name the
    schema does not declare raises :class:`KeyError`, so a fixture cannot
    quietly seed a field the readers will never select.

    ``without_columns`` names ``(table, column)`` pairs to drop after the
    schema is applied, reproducing an index written before that column
    existed. The readers detect an absent column through
    ``PRAGMA table_info``, so a dropped column exercises the same degrade
    path a genuinely older database takes, without a second copy of the
    older DDL.

    Returns the database path.
    """
    db_path = layout.index_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)
        for table, column in without_columns:
            if column not in table_columns(table):
                raise KeyError(f"table {table!r} has no column {column!r} to drop")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        for table, rows in tables.items():
            declared = table_columns(table)
            for row in rows:
                unknown = sorted(set(row) - set(declared))
                if unknown:
                    raise KeyError(f"table {table!r} has no column(s) {unknown}")
                names = list(row)
                placeholders = ",".join("?" for _ in names)
                conn.execute(
                    f"INSERT INTO {table}({','.join(names)}) VALUES({placeholders})",
                    [row[name] for name in names],
                )
        conn.commit()
    finally:
        conn.close()
    return db_path
