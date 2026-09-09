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
literal — ask the layout for it. Schema-refusal tests can construct an
incompatible database directly. A test of exact record bytes writes those
bytes explicitly.

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

from zicato.core.experiment import ExpectedMetricMovement, MetricMovementActual
from zicato.core.types import ScoringWeights
from zicato.epoch._storage import RECORD_FORMAT_VERSION
from zicato.epoch.journal import experiment_body
from zicato.epoch.lifecycle import _config_from_dict, _config_to_dict
from zicato.epoch.lineage import decode_lineage
from zicato.index.schema import apply_schema, table_columns
from zicato.testing import (
    make_epoch_config,
    make_experiment,
    make_hypothesis_spec,
    make_outcome_record,
)
from zicato.tournament.scoring import decode_gen_score
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


def write_tournament(root: Path, data: Mapping[str, Any]) -> None:
    """Publish a live tournament using the same event writer as execution."""
    from zicato.runtime.lock import acquire_workspace_lock
    from zicato.runtime.state import ActiveTournament, write_active_tournament

    envelope = dict.fromkeys(
        ("tournament_id", "parent_generation_id", "child_generation_id", "epoch_id", "started_at"),
        "",
    )
    with acquire_workspace_lock(root, "reader-fixture") as writer:
        write_active_tournament(writer, ActiveTournament.from_dict({**envelope, **data}))


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
    record = decode_lineage({"format_version": RECORD_FORMAT_VERSION, **lineage})
    return write_json(layout.lineage_path, record.to_dict(), indent=indent)


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

    ``config`` overrides a complete epoch fixture with the requested paths
    and scoring. Its synthetic contract hash is for reader tests; execution
    tests prepare an epoch through ``new_epoch``. Other artifacts are written
    only when supplied. ``current`` selects the epoch in the workspace marker.
    """
    directory = layout.epoch_dir(epoch_id)
    directory.mkdir(parents=True, exist_ok=True)
    settings = _config_to_dict(
        make_epoch_config(
            id=epoch_id,
            name=epoch_id,
            created_at=DEFAULT_CREATED_AT,
            board_path=layout.board(epoch_id),
            brief_path=layout.brief(epoch_id),
            scoring=ScoringWeights.from_json(dict(scoring or {})),
        )
    )
    settings.update(config or {})
    write_json(
        layout.epoch_config(epoch_id), _config_to_dict(_config_from_dict(settings)), indent=indent
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


def read_experiment_record(path: Path) -> dict[str, Any]:
    """Read a proposal together with the outcome stored in its committed round."""
    from zicato.epoch.journal import read_experiment_body

    body = read_experiment_body(path.parents[4], path.parents[2].name, path.parent.name)
    if body is None:
        raise FileNotFoundError(path)
    return body


def experiment_record(
    generation_id: str,
    *,
    epoch_id: str,
    parent_generation_id: str | None = None,
    proposed_at: str = DEFAULT_CREATED_AT,
    decision: str | None = None,
    outcome: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a complete experiment fixture through the canonical serializer.

    The supplied hypothesis and outcome values override empty fixture values.
    A generation without an outcome carries explicit null. Invalid records
    belong in a test's direct ``write_json`` call.
    """
    hypothesis = dict(extra.pop("hypothesis", {}))
    hypothesis["expected_metric_movements"] = tuple(
        ExpectedMetricMovement(**movement)
        for movement in hypothesis.get("expected_metric_movements", ())
    )
    outcome_record = None
    if outcome is not None or decision is not None:
        values = dict(outcome or {})
        values.setdefault("tournament_decision", decision)
        values["metric_movements"] = tuple(
            MetricMovementActual(**movement) for movement in values.get("metric_movements", ())
        )
        outcome_record = make_outcome_record(
            **{
                "ran_at": "",
                "pass_rate_delta": 0.0,
                "drift_loss_delta": 0.0,
                "scalar_score_delta": 0.0,
                **values,
            }
        )
    body = experiment_body(
        make_experiment(
            id=extra.pop("id", f"exp_{epoch_id}_{generation_id}"),
            epoch_id=epoch_id,
            generation_id=generation_id,
            parent_generation_id=parent_generation_id,
            proposed_at=proposed_at,
            round_index=extra.pop("round_index", 0),
            hypothesis=make_hypothesis_spec(
                **{
                    "core_idea": "",
                    "modulating": (),
                    "why": "",
                    "expected_pass_rate_delta": "",
                    **hypothesis,
                }
            ),
            patches=(),
            outcome=outcome_record,
        )
    )
    body.update(extra)
    return body


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
    values = dict(experiment or {})
    values.setdefault("epoch_id", epoch_id)
    values.setdefault("generation_id", generation_id)
    if values["epoch_id"] != epoch_id or values["generation_id"] != generation_id:
        raise ValueError("experiment fixture coordinates differ from the generation path")
    write_json(
        layout.experiment(epoch_id, generation_id), experiment_record(**values), indent=indent
    )
    if gen_score is not None:
        score = decode_gen_score(
            {"format_version": RECORD_FORMAT_VERSION, "generation_id": generation_id, **gen_score},
            generation_id=generation_id,
        )
        write_json(layout.gen_score(epoch_id, generation_id), score.to_dict(), indent=indent)
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
) -> Path:
    """Build the workspace's ``index.db`` at the current schema and fill it.

    ``tables`` maps a table name to the rows to insert, each row a mapping
    of column name to value. Unnamed columns stay ``NULL``, which is what a
    writer that never learned a value leaves behind. A column name the
    schema does not declare raises :class:`KeyError`, so a fixture cannot
    quietly seed a field the readers will never select.

    Returns the database path.
    """
    db_path = layout.index_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)
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


def complete_round(
    root: Path,
    epoch_id: str,
    generation_ids: Sequence[str],
    *,
    primary_id: str | None,
    round_index: int | None = None,
    field_record: dict[str, Any] | None = None,
    gate_results: Sequence[dict[str, Any]] = (),
) -> None:
    """Publish fixture outcomes through the completed round record."""
    from dataclasses import replace

    from zicato.epoch.journal import read_experiment
    from zicato.epoch.settlement_receipt import (
        IndexProjection,
        SettlementCandidate,
        decode_settlement_receipt,
        new_settlement_receipt,
        write_settlement_receipt,
    )

    layout = WorkspaceLayout.from_root(root)
    experiments = [read_experiment(root, epoch_id, gid) for gid in generation_ids]
    index = experiments[0].round_index if round_index is None else round_index
    candidates = []
    comparisons = {gate["challenger"]: gate for gate in gate_results}
    for experiment in experiments:
        assert experiment.outcome is not None
        candidates.append(
            SettlementCandidate.from_outcome(
                experiment_id=experiment.id,
                generation_id=experiment.generation_id,
                created_at=experiment.proposed_at,
                parent_scalar=comparisons.get(experiment.generation_id, {})
                .get("parent_aggregate", {})
                .get("scalar"),
                child_scalar=comparisons.get(experiment.generation_id, {})
                .get("child_aggregate", {})
                .get("scalar"),
                outcome=experiment.outcome,
            )
        )
        path = layout.experiment(epoch_id, experiment.generation_id)
        body = json.loads(path.read_text())
        body["round_index"] = index
        body["outcome"] = None
        write_json(path, body)
    receipt = new_settlement_receipt(
        settlement_id=f"{index + 1:032x}",
        epoch_id=epoch_id,
        round_index=index,
        primary_id=primary_id,
        candidates=tuple(candidates),
        field_record=field_record,
    )
    body = receipt.to_dict()
    body["gate_results"] = list(gate_results)
    write_settlement_receipt(
        root,
        replace(
            decode_settlement_receipt(body),
            state="committed",
            index_projection=IndexProjection("succeeded", ""),
        ),
    )
    if layout.lineage_path.exists():
        body = json.loads(layout.lineage_path.read_text())
        for epoch in body["epochs"]:
            if epoch["id"] == epoch_id:
                for generation in epoch["generations"]:
                    if generation["id"] in generation_ids:
                        generation["round_index"] = index
                        generation["promoted"] = None
        write_json(layout.lineage_path, body)


def write_tournament_structure(
    root: Path,
    epoch_id: str,
    *,
    structure: str,
    competitors: Sequence[dict[str, Any]],
    rounds: Sequence[dict[str, Any]] = (),
    standings: Sequence[dict[str, Any]] = (),
    field_status: Sequence[dict[str, Any]] = (),
    structure_params: Mapping[str, Any] | None = None,
) -> None:
    """Publish declared tournament facts for tests of record readers."""
    from zicato.tournament.records import (
        decode_field_tournament_record,
        write_field_tournament_record,
    )

    challenger = next(row["generation_id"] for row in competitors if row["role"] == "challenger")
    champion = next(row["generation_id"] for row in competitors if row["role"] == "champion")
    write_field_tournament_record(
        root,
        epoch_id=epoch_id,
        first_challenger_id=challenger,
        record=decode_field_tournament_record(
            {
                "tournament_id": f"{epoch_id}:field:{challenger}",
                "epoch_id": epoch_id,
                "structure": structure,
                "structure_params": dict(structure_params or {}),
                "competitors": list(competitors),
                "rounds": list(rounds),
                "standings": list(standings),
                "field_status": list(field_status),
                "champion_generation_id": champion,
                "promoted_generation_id": challenger,
                "decision": "promoted",
                "state": "settled",
                "ran_at": DEFAULT_CREATED_AT,
                "reason": "",
            }
        ),
    )
