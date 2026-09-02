"""The analysis report and the query layer aggregate the canonical files alike.

Four quantities are computed from the same files under ``epochs/{id}/`` by
both the analysis-report gatherer and a query reader: a generation's
per-judge weighted-loss totals, the board's entries, the cumulative scalar
along a lineage, and the folded round records. Each is defined once, in
:mod:`zicato.workspace.aggregates`, and these tests measure the two consumers
against each other on one fixture workspace so a second definition cannot
reappear unnoticed.

The workspace is built with the canonical writers — the board serialiser, the
journal's experiment writer, the reducer's loss-profile writer, and the round
log's append path — so the records under test are shaped exactly as a real
run leaves them.

The last test pins the other half of the contract: the query readers that
answer from ``index.db`` still answer from ``index.db``, so a workspace that
was never indexed still reports nothing rather than falling back to the files
by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.board.jsonl import save_board
from zicato.core.drift_kinds import DriftKind, DriftSeverity
from zicato.core.epoch import Generation
from zicato.core.experiment import (
    Experiment,
    HypothesisSpec,
    OutcomeRecord,
)
from zicato.core.loss import JudgeLoss, LossProfile
from zicato.core.types import Expectation, ExpectationKind, JudgeMode, JudgeSpec, OutputScope
from zicato.epoch.analysis import _scalar_trajectory
from zicato.epoch.journal import write_experiment
from zicato.epoch.round_log import (
    DecisionRecorded,
    ExperimentMinted,
    GateEvaluated,
    PatchesApplied,
    RoundClosed,
    RoundLog,
    RoundOpened,
    fold_round_record,
)
from zicato.query.eval_view import _load_board_entries
from zicato.query.execution_plan import _read_round_events
from zicato.query.judge_view import build_per_judge_for_entry, build_per_judge_for_generation
from zicato.query.paths import WorkspacePaths
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import make_board_entry
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-09-01_parity"
ENTRIES = ("t1", "t2")
GENERATIONS = ("v0", "v1", "v2")

#: The judge attribution each run records: one named judge, plus the
#: reducer's catch-all bucket for drift it could not pair with a judge. The
#: bucket is what makes the two consumers' presentation differ, so every
#: fixture run carries one.
_JUDGE_LOSSES = {
    "t1": (
        JudgeLoss(judge_name="clarity", raw_loss=2.0, weight=1.5, weighted_loss=3.0),
        JudgeLoss(judge_name="", raw_loss=1.0, weight=1.0, weighted_loss=1.0),
    ),
    "t2": (
        JudgeLoss(judge_name="clarity", raw_loss=1.0, weight=1.5, weighted_loss=1.5),
        JudgeLoss(judge_name="brevity", raw_loss=4.0, weight=0.5, weighted_loss=2.0),
    ),
}

#: Each generation's recorded change in scalar score, and whether it was
#: promoted. ``v0`` seeds the lineage and has no outcome.
_DELTAS = {"v1": -0.25, "v2": 0.10}


def _board() -> list:
    return [
        make_board_entry(
            id="t1",
            weight=1.0,
            tags=("smoke",),
            expectation=Expectation(
                kind=ExpectationKind.EXPECTED_TEXT,
                spec="hello",
                reads=OutputScope.FINAL,
            ),
            judges=(
                JudgeSpec(
                    name="clarity",
                    mode=JudgeMode.INLINE,
                    body="Is the answer clear?",
                    severity=DriftSeverity.WARNING,
                ),
            ),
        ),
        make_board_entry(id="t2", weight=2.0, tags=()),
    ]


def _experiment(generation_id: str, parent_id: str | None) -> Experiment:
    outcome = None
    if generation_id in _DELTAS:
        outcome = OutcomeRecord(
            ran_at="2026-09-01T01:00:00Z",
            drift_movements=(),
            metric_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=_DELTAS[generation_id],
            scalar_score_delta=_DELTAS[generation_id],
            tournament_decision="promoted" if generation_id == "v1" else "rejected",
        )
    return Experiment(
        id=f"exp_{EPOCH}_{generation_id}",
        epoch_id=EPOCH,
        generation_id=generation_id,
        parent_generation_id=parent_id,
        proposed_at=f"2026-09-01T0{GENERATIONS.index(generation_id)}:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=f"change {generation_id}",
            modulating=("instruction",),
            why="parity fixture",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=(),
        outcome=outcome,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """One epoch: three generations, two runs each, and one settled round."""
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    layout.epoch_dir(EPOCH).mkdir(parents=True, exist_ok=True)
    (root / "current_epoch").write_text(EPOCH, encoding="utf-8")
    layout.epoch_config(EPOCH).write_text(
        json.dumps({"id": EPOCH, "name": "parity", "created_at": "2026-09-01T00:00:00Z"}),
        encoding="utf-8",
    )
    save_board(_board(), layout.board(EPOCH), disable_drift=(DriftKind.OFF_TOPIC,))

    for index, generation_id in enumerate(GENERATIONS):
        parent = None if index == 0 else "v0"
        write_experiment(root, EPOCH, generation_id, _experiment(generation_id, parent))
        for entry_id in ENTRIES:
            write_loss_profile(
                LossProfile(
                    run_id=f"{generation_id}:{entry_id}",
                    entry_id=entry_id,
                    generation_id=generation_id,
                    epoch_id=EPOCH,
                    drift_counts=(),
                    plan_revisions=0,
                    task_failure_ratio=0.0,
                    runtime_ms=1000,
                    wall_clock_budget_exceeded=False,
                    expectation_result=None,
                    drift_loss=0.25,
                    pass_fail=True,
                    per_judge_loss=_JUDGE_LOSSES[entry_id],
                ),
                layout.loss(EPOCH, generation_id, entry_id),
            )

    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened(contract_hash="hash-parity"))
    log.append(ExperimentMinted(experiment_id=f"exp_{EPOCH}_v1"))
    log.append(PatchesApplied(generation_id="v1"))
    log.append(
        GateEvaluated(
            rule_fired="",
            decision="promote",
            champion_scalar=0.5,
            challenger_scalar=0.25,
            margin_required=0.01,
        )
    )
    log.append(DecisionRecorded(decision="promote", provenance={"gate": "margin"}))
    log.append(RoundClosed())
    return root


def test_per_judge_totals_match_the_query_layer(workspace: Path) -> None:
    """The report's per-judge totals are the query rows, summed over the runs."""
    paths = WorkspacePaths(workspace)
    data = gather_epoch_report_data(workspace, EPOCH)

    for generation in data.generations:
        query_totals: dict[str, float] = {}
        for entry_id in ENTRIES:
            served = build_per_judge_for_entry(paths, EPOCH, generation.generation_id, entry_id)
            for row in served["judges"]:
                name = row["judge_name"]
                query_totals[name] = query_totals.get(name, 0.0) + row["weighted_loss"]

        named = {
            name: total
            for name, total in generation.per_judge_loss_totals
            if name != "(unattributed)"
        }
        assert named == query_totals
        assert named == {"brevity": 2.0, "clarity": 4.5}


def test_the_report_totals_the_bucket_the_per_entry_table_omits(workspace: Path) -> None:
    """Unattributed loss is totalled by the report and named there.

    The per-entry table names judges, so it drops the reducer's catch-all
    bucket; the report totals it under a label of its own. Both read the same
    rows, and this is the only difference between what they do with them.
    """
    paths = WorkspacePaths(workspace)
    data = gather_epoch_report_data(workspace, EPOCH)
    totals = dict(data.generations[0].per_judge_loss_totals)

    assert totals["(unattributed)"] == 1.0
    served = build_per_judge_for_entry(paths, EPOCH, "v0", "t1")
    assert [row["judge_name"] for row in served["judges"]] == ["clarity"]


def test_board_entries_match_the_query_layer(workspace: Path) -> None:
    """Both consumers see the same entries, in file order, with the same fields."""
    paths = WorkspacePaths(workspace)
    data = gather_epoch_report_data(workspace, EPOCH)
    entries = _load_board_entries(paths, EPOCH)

    assert [view.id for view in data.board_entries] == [entry.id for entry in entries]
    assert [view.weight for view in data.board_entries] == [entry.weight for entry in entries]
    assert [view.judges for view in data.board_entries] == [
        tuple(judge.name for judge in entry.judges) for entry in entries
    ]
    assert data.disable_drift == ("off_topic",)


def test_round_records_match_the_query_layer(workspace: Path) -> None:
    """The report folds the same round logs the execution plan reads."""
    paths = WorkspacePaths(workspace)
    data = gather_epoch_report_data(workspace, EPOCH)

    events, readable = _read_round_events(paths, EPOCH, 0)
    assert readable is True
    assert list(data.round_records) == [fold_round_record(events)]


def test_cumulative_scalars_match_the_lineage_renderer(workspace: Path) -> None:
    """The report's cumulative scalars are the ones the journal's tables show."""
    data = gather_epoch_report_data(workspace, EPOCH)
    generations = [
        Generation(
            id=generation_id,
            epoch_id=EPOCH,
            parent_id=None if index == 0 else "v0",
            snapshot_root=workspace,
            created_at="2026-09-01T00:00:00Z",
        )
        for index, generation_id in enumerate(GENERATIONS)
    ]
    experiments = [
        _experiment(generation_id, None if index == 0 else "v0")
        for index, generation_id in enumerate(GENERATIONS)
    ]

    rendered = _scalar_trajectory(generations, experiments)
    assert {g.generation_id: g.cumulative_scalar for g in data.generations} == rendered
    assert rendered == {"v0": 0.0, "v1": -0.25, "v2": 0.10}


def test_the_index_backed_per_judge_view_still_needs_the_index(workspace: Path) -> None:
    """A never-indexed workspace serves no judges, not a filesystem fallback.

    The report reaches these numbers through the canonical files, and the
    per-generation table reaches them through ``index.db``. Sharing the file
    readers must not quietly give this reader a second source: the runs are
    all on disk here, and this reader still reports nothing, because nothing
    has been indexed.
    """
    served = build_per_judge_for_generation(WorkspacePaths(workspace), EPOCH, "v1")

    assert served == {"epoch_id": EPOCH, "generation_id": "v1", "judges": []}
