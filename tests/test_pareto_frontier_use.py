"""The frontier's finer axes, its confirm-before-use step, and the operator table.

Three subjects (docs/design/PARETO-FRONTIER.md §2.1, §6.1, §7):

* the dimension axes inside each weighted namespace, in the scalar points the
  namespace axes already use, checked against sums worked by hand;
* ``confirm_member`` / ``confirm_frontier_member``, which re-measure a member
  against the current champion on fresh train draws before anything uses it;
* the trade-off table printed at epoch close and written into ``analysis.md``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import zicato.tournament.worker_execution as _tournament_worker_execution
from tests._runtime_builders import prepare_tournament_epoch, runtime_config
from tests.test_pareto_frontier import _drive_round
from zicato.core import BoardEntry, Generation, LossProfile, MetricCount, ScoringWeights
from zicato.core.loss import JudgeLoss
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose
from zicato.core.workspace import run_id_for_unit
from zicato.epoch.pareto import (
    DIMENSION_AGGREGATES_KEY,
    FRONTIER_DIMENSION_CAP,
    FrontierCandidate,
    FrontierMember,
    ParetoFrontier,
    axis_values,
    confirm_member,
    frontier_axes,
    frontier_from_dict,
    frontier_to_dict,
    load_frontier,
    render_frontier_tradeoffs,
    save_frontier,
    update_frontier,
)
from zicato.evolve.pareto import confirm_frontier_member, frontier_aggregates
from zicato.tournament.scoring import aggregate_dimension_metrics, aggregate_generation_score

_MARGIN = 0.01
_NAMESPACES = ("cost:", "drift:", "failure:", "judge:", "latency:", "rubric:", "schema:")


def _weights(**kwargs: Any) -> ScoringWeights:
    return ScoringWeights(promote_margin=_MARGIN, **kwargs)


def _loss(entry_id: str, generation_id: str = "v0", **metrics: float) -> LossProfile:
    """One started run reporting ``rubric:`` / ``cost:`` metrics by keyword."""
    return LossProfile(
        run_id=f"r-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e0",
        metric_counts=tuple(
            MetricCount(name=name.replace("__", ":"), severity="info", count=value)
            for name, value in metrics.items()
        ),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=True,
        execution_started=True,
    )


def _agg(namespaces: dict[str, float], dimensions: dict[str, float] | None = None) -> dict:
    body: dict[str, Any] = {"namespace_aggregates": namespaces, "scalar": sum(namespaces.values())}
    if dimensions is not None:
        body[DIMENSION_AGGREGATES_KEY] = dimensions
    return body


# ---------------------------------------------------------------------------
# Dimension contributions, in scalar points
# ---------------------------------------------------------------------------


def test_dimensions_split_each_namespace_aggregate_into_scalar_points() -> None:
    """Worked by hand: weight times the per-run mean, per metric.

    rubric: weighs -1.0 and cost: 0.001. Over two runs, ``rubric:tone`` is
    0.8 and 0.4 (mean 0.6, so -0.6) and ``rubric:structure`` is 1.0 and 0.0
    (mean 0.5, so -0.5); ``cost:tokens`` is 1000 and 3000 (mean 2000, so 2.0).
    """
    weights = _weights()
    losses = [
        _loss("a", rubric__tone=0.8, rubric__structure=1.0, cost__tokens=1000),
        _loss("b", rubric__tone=0.4, rubric__structure=0.0, cost__tokens=3000),
    ]
    dimensions = aggregate_dimension_metrics(losses, weights)
    assert dimensions["rubric:tone"] == pytest.approx(-0.6)
    assert dimensions["rubric:structure"] == pytest.approx(-0.5)
    assert dimensions["cost:tokens"] == pytest.approx(2.0)
    namespaces = aggregate_generation_score(losses, weights)["namespace_aggregates"]
    for namespace in ("rubric:", "cost:", "failure:"):
        total = sum(v for name, v in dimensions.items() if name.startswith(namespace))
        assert total == pytest.approx(namespaces[namespace])


def test_dimensions_carry_judges_and_failures_and_leave_out_drift_and_zero_weights() -> None:
    weights = _weights(task_failure_weight=10.0)
    loss = replace(
        _loss("a", drift__off_topic=3.0, output__chars=500.0),
        task_failure_ratio=0.5,
        per_judge_loss=(
            JudgeLoss(judge_name="tone", weight=2.0, raw_loss=0.25, weighted_loss=0.5),
        ),
    )
    dimensions = aggregate_dimension_metrics([loss], weights)
    # judge: weighs 1.0 and the per-judge weight is already inside weighted_loss.
    assert dimensions["judge:tone"] == pytest.approx(0.5)
    # failure: weighs 1.0; a 0.5 failure ratio at task_failure_weight 10.
    assert dimensions["failure:tasks"] == pytest.approx(5.0)
    assert dimensions["failure:not_completed"] == 0.0
    assert not any(name.startswith(("drift:", "output:", "runtime:")) for name in dimensions)


def test_a_run_that_never_started_is_left_out_of_the_dimension_mean() -> None:
    losses = [
        _loss("a", cost__tokens=1000, cost__calls=1),
        replace(_loss("b", cost__tokens=9000, cost__calls=9), execution_started=False),
    ]
    assert aggregate_dimension_metrics(losses, _weights())["cost:tokens"] == pytest.approx(1.0)
    assert aggregate_dimension_metrics([], _weights()) == {}


# ---------------------------------------------------------------------------
# The axis set
# ---------------------------------------------------------------------------


def test_dimension_axes_follow_the_namespaces_by_contract_weight_then_name() -> None:
    """Judges rank by their per-judge weight; a lone dimension adds no axis."""
    weights = _weights(per_judge_weights={"tone": 3.0, "facts": 0.5})
    champion = _agg(
        {},
        {
            "judge:facts": 0.0,
            "judge:tone": 0.0,
            "rubric:b": 0.0,
            "rubric:a": 0.0,
            "cost:tokens": 0.0,  # the only cost: dimension IS the cost: axis
            "failure:tasks": 0.0,
            "failure:not_completed": 0.0,
            "output:chars": 0.0,  # zero-weight namespace
        },
    )
    assert frontier_axes(weights, champion) == (
        *_NAMESPACES,
        "failure:not_completed",  # 1.0 * 50
        "failure:tasks",  # 1.0 * 10
        "judge:tone",  # 1.0 * 3
        "rubric:a",  # |-1.0| * 1
        "rubric:b",
        "judge:facts",  # 1.0 * 0.5
    )
    # No reference aggregate, or one that carries no dimensions: namespaces only.
    assert frontier_axes(weights) == _NAMESPACES
    assert frontier_axes(weights, _agg({})) == _NAMESPACES


def test_the_dimension_axes_are_capped() -> None:
    champion = _agg({}, {f"rubric:m{i:02d}": 0.0 for i in range(20)})
    axes = frontier_axes(_weights(), champion)
    assert axes[: len(_NAMESPACES)] == _NAMESPACES
    assert axes[len(_NAMESPACES) :] == tuple(
        f"rubric:m{i:02d}" for i in range(FRONTIER_DIMENSION_CAP)
    )


def test_a_dimension_one_side_lacks_is_omitted_not_zero_filled() -> None:
    weights = _weights()
    champion = _agg({"rubric:": -1.0}, {"rubric:a": -0.5, "rubric:b": -0.5})
    candidate = _agg({"rubric:": -1.0}, {"rubric:a": -0.5})
    axes = frontier_axes(weights, champion)
    assert "rubric:b" in axis_values(champion, axes)
    assert "rubric:b" not in axis_values(candidate, axes)
    # A zero-filled rubric:b would read as 0.5 points WORSE than the champion;
    # an omitted one creates no relation, so nothing is admitted or refused on it.
    update = update_frontier(
        ParetoFrontier(epoch_id="e0"),
        champion=FrontierCandidate("v0", champion),
        candidates=[FrontierCandidate("v1", candidate)],
        weights=weights,
        round_index=1,
    )
    assert update.admitted == ()


def test_a_dimension_separates_candidates_the_namespace_cannot() -> None:
    """Two rubric metrics move in opposite directions and the namespace ties."""
    weights = _weights()
    champion = _agg({"rubric:": -1.0}, {"rubric:a": -0.5, "rubric:b": -0.5})
    trader = _agg({"rubric:": -1.0}, {"rubric:a": -0.8, "rubric:b": -0.2})

    def _settle(strip: bool) -> Any:
        def view(agg: dict) -> dict:
            return {k: v for k, v in agg.items() if not strip or k != DIMENSION_AGGREGATES_KEY}

        return update_frontier(
            ParetoFrontier(epoch_id="e0"),
            champion=FrontierCandidate("v0", view(champion)),
            candidates=[FrontierCandidate("v1", view(trader))],
            weights=weights,
            round_index=1,
        )

    assert _settle(strip=True).admitted == ()
    update = _settle(strip=False)
    assert update.admitted == ("v1",)
    member = update.frontier.members[0]
    assert member.beats_champion_on == ("rubric:a",)
    assert member.beats_champion_by == pytest.approx({"rubric:a": 0.3})
    assert "rubric:b" in update.frontier.axes


def test_a_dimension_win_does_not_excuse_a_namespace_regression() -> None:
    """Admission still runs the gate's namespace-monotonicity rule, unchanged."""
    champion = _agg({"rubric:": -1.0}, {"rubric:a": -0.5, "rubric:b": -0.5})
    gutted = _agg({"rubric:": -0.9}, {"rubric:a": -0.9, "rubric:b": 0.0})
    update = update_frontier(
        ParetoFrontier(epoch_id="e0"),
        champion=FrontierCandidate("v0", champion),
        candidates=[FrontierCandidate("v1", gutted)],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()


def test_a_settled_round_records_the_dimension_axes_of_the_default_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real settle path: the failure pair joins the namespaces.

    The stub harness reports one ``cost:`` metric, which is the ``cost:`` axis
    itself, and every run carries both ``failure:`` members.
    """
    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    frontier = load_frontier(workspace, epoch_id)
    assert frontier.axes == (*_NAMESPACES, "failure:not_completed", "failure:tasks")
    member = frontier.members[0]
    assert member.beats_champion_on == ("cost:",)
    assert member.axis_values["failure:tasks"] == 0.0
    # The score file the gate and the cache read does not carry the dimensions.
    gen_score = workspace / "epochs" / epoch_id / "generations" / "v1" / "gen_score.json"
    assert DIMENSION_AGGREGATES_KEY not in json.loads(gen_score.read_text())


def test_a_record_written_before_dimensions_and_confirmation_still_reads() -> None:
    body = {
        "format_version": 1,
        "epoch_id": "e0",
        "axes": ["cost:", "drift:"],
        "margin": 0.01,
        "champion_generation_id": "v0",
        "updated_round": 3,
        "members": [
            {
                "generation_id": "v2",
                "round_admitted": 3,
                "champion_generation_id": "v0",
                "scalar": 3.5,
                "axis_values": {"cost:": 0.5, "drift:": 3.0},
                "beats_champion_on": ["cost:"],
            }
        ],
        "retired": [],
    }
    frontier = frontier_from_dict(body, epoch_id="e0")
    member = frontier.members[0]
    assert member.confirmed_round is None
    assert member.beats_champion_by == {}
    # Rewritten unchanged: the new keys appear only once they hold something.
    assert frontier_to_dict(frontier) == body
    # And it still takes part in dominance on the axes it shares with a newcomer.
    update = update_frontier(
        frontier,
        champion=FrontierCandidate("v0", _agg({"cost:": 1.0, "drift:": 1.0})),
        candidates=[FrontierCandidate("v5", _agg({"cost:": 0.2, "drift:": 2.0}))],
        weights=_weights(),
        round_index=4,
    )
    assert update.admitted == ("v5",)
    assert update.retired == ("v2",)


# ---------------------------------------------------------------------------
# confirm_member — pure
# ---------------------------------------------------------------------------


def _record_with_member() -> ParetoFrontier:
    return ParetoFrontier(
        epoch_id="e0",
        axes=("cost:", "drift:"),
        margin=_MARGIN,
        champion_generation_id="v0",
        updated_round=2,
        members=(
            FrontierMember(
                generation_id="v1",
                round_admitted=2,
                champion_generation_id="v0",
                axis_values={"cost:": 0.5, "drift:": 3.0, "rubric:": -1.0},
                beats_champion_on=("cost:",),
                scalar=2.5,
                beats_champion_by={"cost:": 0.5},
            ),
        ),
    )


def _confirm(member: dict, champion: dict, *, champion_id: str = "v4") -> Any:
    return confirm_member(
        _record_with_member(),
        champion=FrontierCandidate(champion_id, champion),
        member=FrontierCandidate("v1", member),
        weights=_weights(),
        round_index=7,
    )


def test_a_member_that_still_beats_the_current_champion_is_refreshed() -> None:
    update = _confirm(
        _agg({"cost:": 0.6, "drift:": 2.0, "rubric:": -1.0}),
        _agg({"cost:": 0.9, "drift:": 1.0, "rubric:": -1.0}),
    )
    assert (update.confirmed, update.retired, update.changed) == (("v1",), (), True)
    member = update.frontier.members[0]
    assert member.champion_generation_id == "v4"
    assert member.confirmed_round == 7
    assert member.round_admitted == 2
    assert member.axis_values == {"cost:": 0.6, "drift:": 2.0, "rubric:": -1.0}
    assert member.beats_champion_on == ("cost:",)
    assert member.beats_champion_by == pytest.approx({"cost:": 0.3})
    assert member.scalar == pytest.approx(1.6)
    assert update.frontier.updated_round == 7


def test_a_member_that_no_longer_beats_the_champion_is_retired_unconfirmed() -> None:
    """Its cost win was inside the margin on the fresh draw."""
    update = _confirm(
        _agg({"cost:": 0.895, "drift:": 2.0, "rubric:": -1.0}),
        _agg({"cost:": 0.9, "drift:": 1.0, "rubric:": -1.0}),
    )
    assert (update.confirmed, update.retired) == ((), ("v1",))
    assert update.frontier.members == ()
    retired = update.frontier.retired[0]
    assert (retired.reason, retired.round_retired) == ("unconfirmed", 7)
    # The retired entry keeps the values it was admitted on, as evidence.
    assert retired.member.axis_values["cost:"] == 0.5


def test_a_member_that_now_regresses_a_monotonic_namespace_is_retired_unconfirmed() -> None:
    update = _confirm(
        _agg({"cost:": 0.1, "drift:": 2.0, "rubric:": -0.5}),
        _agg({"cost:": 0.9, "drift:": 1.0, "rubric:": -1.0}),
    )
    assert update.retired == ("v1",)
    assert update.frontier.retired[0].reason == "unconfirmed"


def test_confirming_a_generation_that_is_not_a_member_changes_nothing() -> None:
    frontier = _record_with_member()
    update = confirm_member(
        frontier,
        champion=FrontierCandidate("v4", _agg({"cost:": 0.9})),
        member=FrontierCandidate("v3", _agg({"cost:": 0.1})),
        weights=_weights(),
        round_index=7,
    )
    assert not update.changed
    assert update.frontier is frontier


# ---------------------------------------------------------------------------
# confirm_frontier_member — the measurement
# ---------------------------------------------------------------------------

_TRAIN = ("a", "b", "c")


def _board() -> list[BoardEntry]:
    entries = [
        BoardEntry(id=entry_id, kind="single_turn", wall_clock_budget_seconds=60, input="x")
        for entry_id in _TRAIN
    ]
    entries.append(
        BoardEntry(
            id="held",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="x",
            tags=("holdout",),
        )
    )
    return entries


class _Rig:
    """A workspace with one frontier member and a counting board-unit stub."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        tokens: dict[tuple[str, str], int],
        unstarted: tuple[str, str] | None = None,
    ) -> None:
        self.root = tmp_path
        self.board = _board()
        self.config = runtime_config(tmp_path)
        self.weights = _weights()
        self.epoch_id = prepare_tournament_epoch(tmp_path, self.config, self.board, self.weights)
        self.runs: list[tuple[str, str, str]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        save_frontier(
            tmp_path, self.epoch_id, replace(_record_with_member(), epoch_id=self.epoch_id)
        )

        async def fake_run_single(*, generation: Any, entry: Any, epoch_id: str, **_: Any) -> Any:
            draw = MeasurementDraw.from_context(entry.context)
            purpose = str(draw.purpose)
            self.runs.append((generation.id, entry.id, purpose))
            return replace(
                _loss(entry.id, generation.id, cost__tokens=tokens[generation.id, purpose]),
                run_id=run_id_for_unit(generation.id, entry.id, draw, epoch_id=epoch_id),
                epoch_id=epoch_id,
                execution_started=False if (generation.id, entry.id) == unstarted else None,
            )

        monkeypatch.setattr(_tournament_worker_execution, "_run_single", fake_run_single)

    def emit(self, token: str, payload: dict[str, Any]) -> None:
        self.events.append((token, payload))

    def confirm(self, champion_id: str, round_index: int = 5) -> Any:
        def generation(generation_id: str) -> Generation:
            return Generation(
                id=generation_id,
                epoch_id=self.epoch_id,
                parent_id=None,
                snapshot_root=self.root / f"snap_{generation_id}",
                created_at="2026-01-01T00:00:00Z",
            )

        return asyncio.run(
            confirm_frontier_member(
                adapter=object(),
                champion=generation(champion_id),
                member=generation("v1"),
                board=self.board,
                weights=self.weights,
                config=self.config,
                workspace_root=self.root,
                epoch_id=self.epoch_id,
                round_index=round_index,
                round_log=self,
            )
        )

    def ran(self, generation_id: str) -> list[str]:
        return sorted(entry for gid, entry, _purpose in self.runs if gid == generation_id)


def test_confirmation_measures_train_entries_under_its_own_purpose_and_reports_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    confirmation = str(MeasurementPurpose.CONFIRMATION)
    rig = _Rig(
        monkeypatch,
        tmp_path,
        tokens={("v0", confirmation): 1000, ("v1", confirmation): 400},
    )
    with caplog.at_level("INFO", logger="zicato.orchestrator"):
        member = rig.confirm("v0")

    # The holdout entry never ran, and nothing ran as a tournament draw.
    assert rig.ran("v0") == list(_TRAIN)
    assert rig.ran("v1") == list(_TRAIN)
    assert {purpose for _g, _e, purpose in rig.runs} == {confirmation}

    assert member is not None
    assert member.confirmed_round == 5
    assert member.champion_generation_id == "v0"
    assert member.beats_champion_by == pytest.approx({"cost:": 0.6})
    assert load_frontier(tmp_path, rig.epoch_id).members == (member,)

    assert rig.events == [
        (
            "frontier_updated",
            {"retired": (), "confirmed": ("v1",), "size": 1, "units_fresh": 6, "units_cached": 0},
        )
    ]
    assert "v1 against v0: confirmed; 6 board unit(s) run, 0 reused" in caplog.text


def test_a_pair_is_paid_for_once_and_a_new_champion_costs_only_its_own_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    confirmation = str(MeasurementPurpose.CONFIRMATION)
    rig = _Rig(
        monkeypatch,
        tmp_path,
        tokens={
            ("v0", confirmation): 1000,
            ("v1", confirmation): 400,
            ("v4", confirmation): 700,
        },
    )
    assert rig.confirm("v0") is not None
    assert len(rig.runs) == 6

    # Confirming the same pair again runs nothing and writes nothing.
    assert rig.confirm("v0", round_index=6).confirmed_round == 5
    assert len(rig.runs) == 6
    assert len(rig.events) == 1

    # A promotion later: the member is re-measured against the new champion
    # from the member's cached draw, so only the champion's units run.
    member = rig.confirm("v4", round_index=8)
    assert rig.ran("v4") == list(_TRAIN)
    assert len(rig.runs) == 9
    assert member.champion_generation_id == "v4"
    assert member.confirmed_round == 8
    assert member.beats_champion_by == pytest.approx({"cost:": 0.3})
    assert rig.events[-1][1]["units_fresh"] == 3
    assert rig.events[-1][1]["units_cached"] == 3


def test_a_member_whose_win_was_a_lucky_draw_is_retired_and_not_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The record says v1 is 0.5 cheaper; the fresh draw says it is not cheaper."""
    confirmation = str(MeasurementPurpose.CONFIRMATION)
    rig = _Rig(
        monkeypatch,
        tmp_path,
        tokens={("v0", confirmation): 1000, ("v1", confirmation): 1000},
    )
    assert rig.confirm("v0") is None
    frontier = load_frontier(tmp_path, rig.epoch_id)
    assert frontier.members == ()
    assert [(r.member.generation_id, r.reason) for r in frontier.retired] == [("v1", "unconfirmed")]
    assert rig.events[0][1]["retired"] == ("v1",)
    assert rig.events[0][1]["units_fresh"] == 6


def test_an_incomplete_measurement_leaves_the_record_and_still_reports_its_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One member unit never started, so the pair was not measured."""
    confirmation = str(MeasurementPurpose.CONFIRMATION)
    rig = _Rig(
        monkeypatch,
        tmp_path,
        tokens={("v0", confirmation): 1000, ("v1", confirmation): 1000},
        unstarted=("v1", "b"),
    )
    before = load_frontier(tmp_path, rig.epoch_id)
    assert rig.confirm("v0") is None
    assert load_frontier(tmp_path, rig.epoch_id) == before
    assert rig.events == [
        (
            "frontier_updated",
            {"retired": (), "confirmed": (), "size": 1, "units_fresh": 6, "units_cached": 0},
        )
    ]


def test_a_failed_or_absent_confirmation_never_raises_and_leaves_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = _Rig(monkeypatch, tmp_path, tokens={})  # every board unit raises KeyError
    before = load_frontier(tmp_path, rig.epoch_id)
    assert rig.confirm("v0") is None
    assert load_frontier(tmp_path, rig.epoch_id) == before

    # A generation that is not on the record costs nothing.
    save_frontier(tmp_path, rig.epoch_id, replace(before, members=()))
    rig.runs.clear()
    assert rig.confirm("v0") is None
    assert rig.runs == []


def test_frontier_aggregates_withholds_dimensions_from_an_incomplete_side() -> None:
    """A side whose profiles do not cover what its aggregate scored gets none."""
    weights = _weights()
    left = {e: _loss(e, "v0", cost__tokens=10, cost__calls=1) for e in ("a", "b")}
    right = {e: _loss(e, "v1", cost__tokens=20, cost__calls=2) for e in ("a",)}

    class _Result:
        parent_generation_id = "v0"
        child_generation_id = "v1"
        parent_agg = aggregate_generation_score(list(left.values()), weights)
        child_agg = aggregate_generation_score(list(right.values()), weights)
        per_entry_losses = {"a": (left["a"], right["a"])}

    aggregates = frontier_aggregates(_Result(), weights)
    assert DIMENSION_AGGREGATES_KEY not in aggregates["v0"]
    assert aggregates["v1"][DIMENSION_AGGREGATES_KEY]["cost:tokens"] == pytest.approx(0.02)
    assert aggregates["v0"] is _Result.parent_agg


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------


def test_the_tradeoff_table_names_each_win_its_size_and_how_to_adopt() -> None:
    confirmed = replace(_record_with_member().members[0], generation_id="v3", confirmed_round=6)
    table = render_frontier_tradeoffs(
        replace(_record_with_member(), members=(*_record_with_member().members, confirmed))
    )
    assert "## Trade-off candidates" in table
    assert "| `v1` | `cost:` (0.5) | 2.5000 | `v0` | no (selection draws) |" in table
    assert "| `v3` | `cost:` (0.5) | 2.5000 | `v0` | round 6 |" in table
    assert "control/promote/<generation>" in table
    assert "operator_override" in table
    assert render_frontier_tradeoffs(ParetoFrontier(epoch_id="e0")) == ""


def test_epoch_close_prints_the_table_and_analysis_md_carries_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zicato.cli.commands.epoch import epoch_grp

    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    result = CliRunner().invoke(epoch_grp, ["close", epoch_id, "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "## Trade-off candidates" in result.output
    assert "| `v1` | `cost:` (0.5) |" in result.output

    analysis = (workspace / "epochs" / epoch_id / "analysis.md").read_text()
    assert "## Trade-off candidates" in analysis
    assert "| `v1` | `cost:` (0.5) |" in analysis


def test_an_unreadable_record_does_not_cost_the_epoch_its_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zicato.epoch.lifecycle import close_epoch
    from zicato.epoch.pareto import frontier_path

    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    frontier_path(workspace, epoch_id).write_text(json.dumps({"format_version": 999}))
    analysis = close_epoch(workspace, epoch_id).read_text()
    assert "Trade-off candidates" not in analysis
