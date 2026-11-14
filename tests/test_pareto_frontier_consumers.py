"""The two opt-in consumers of the Pareto frontier record.

* ``experimental.recombine_pool: "frontier"`` — the recombination slot draws
  its two parents from frontier members confirmed against the current
  champion (docs/design/PROPOSER.md §2.6.2).
* ``experimental.frontier_steering`` — the proposer is told which scored
  dimensions no confirmed member improves (docs/design/PARETO-FRONTIER.md §6.3).

Both are off by default, and each section pins the off behaviour first.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import zicato.tournament.worker_execution as _tournament_worker_execution
from tests._contract_pins import deterministic_weights
from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
)
from tests._runtime_builders import (
    prepare_tournament_epoch,
    runtime_config,
    seed_tournament_generations,
)
from tests.test_matchup_grid_signal import EPOCH, _write_loss
from tests.test_pareto_frontier import _install_costed_run_single
from tests.test_recombine_engine import _candidate
from zicato.core import BoardEntry, Generation, LossProfile, MetricCount
from zicato.core.measurement import MeasurementDraw
from zicato.core.types import (
    Experiment,
    ExperimentalConfig,
    HypothesisSpec,
    OutcomeRecord,
    Patch,
    ProposerQualityConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.core.workspace import run_id_for_unit
from zicato.epoch import journal
from zicato.epoch.pareto import FrontierMember, ParetoFrontier, load_frontier, save_frontier
from zicato.epoch.recombine import RECOMBINE_CONFIRM_MAX, eligible_parents, rank_pairs
from zicato.evolve import round_context
from zicato.evolve.pareto import frontier_confirmer, unimproved_dimensions
from zicato.evolve.round_context import (
    _build_candidate_history,
    _build_frontier_recombination_pair,
)
from zicato.proposer.prompts import (
    FRONTIER_STEERING_MAX_DIMENSIONS,
    render_frontier_steering_block,
)
from zicato.proposer.recombine import mint_recombined_experiment

_MARGIN = 0.01


def _weights(**experimental: Any) -> ScoringWeights:
    return ScoringWeights(
        promote_margin=_MARGIN,
        experimental=ExperimentalConfig(recombine=True, **experimental),
        proposer_quality=ProposerQualityConfig(best_of_n=3),
    )


def _experiment(
    epoch_id: str,
    gid: str,
    *,
    mutation_id: str,
    parent: str | None = "v0",
    decision: str = "rejected",
    recombined_from: tuple[str, ...] = (),
) -> Experiment:
    return Experiment(
        id=f"experiment-{gid}",
        epoch_id=epoch_id,
        generation_id=gid,
        parent_generation_id=parent,
        proposed_at="2026-01-01T00:00:00Z",
        round_index=int(gid[1:]),
        hypothesis=HypothesisSpec(
            core_idea=f"idea of {gid}",
            modulating=(mutation_id,),
            why="trade one axis for another",
            expected_pass_rate_delta="positive",
        ),
        patches=(
            Patch(
                id=f"patch-{gid}",
                mutation_id=mutation_id,
                op="replace",
                new_content=f"content of {gid}",
                new_numeric=None,
                new_enum=None,
                rationale="trade",
            ),
        ),
        outcome=OutcomeRecord(
            ran_at="2026-01-01T00:00:00Z",
            pass_rate_delta=0.0,
            drift_loss_delta=0.5,
            scalar_score_delta=0.5,
            tournament_decision=decision,
        ),
        recombined_from=recombined_from,
    )


def _member(
    gid: str, *wins: str, confirmed: int | None = None, champion: str = "v0"
) -> FrontierMember:
    return FrontierMember(
        generation_id=gid,
        round_admitted=int(gid[1:]),
        champion_generation_id=champion,
        axis_values=dict.fromkeys(wins, 0.0),
        beats_champion_on=tuple(sorted(wins)),
        scalar=1.0,
        confirmed_round=confirmed,
    )


def _seed(
    root: Path,
    epoch_id: str,
    members: list[FrontierMember],
    experiments: list[Experiment],
) -> dict[str, Any]:
    """Experiments and a frontier record; returns the builder's keyword arguments."""
    for experiment in experiments:
        journal.write_experiment(root, epoch_id, experiment.generation_id, experiment)
    save_frontier(root, epoch_id, ParetoFrontier(epoch_id=epoch_id, members=tuple(members)))
    return dict(
        weights=_weights(recombine_pool="frontier"),
        workspace_root=root,
        epoch_id=epoch_id,
        parent_id="v0",
        train_entry_ids=frozenset({"retrieval", "formatting"}),
        mutations=[SimpleNamespace(id=f"m{i}") for i in range(1, 9)],
    )


class _Confirm:
    """A scripted ``confirm``: upholds a member, optionally with new wins, or retires it."""

    def __init__(self, members: list[FrontierMember], **results: Any) -> None:
        self.members = {m.generation_id: m for m in members}
        self.results = results
        self.calls: list[str] = []

    async def __call__(self, gid: str) -> FrontierMember | None:
        self.calls.append(gid)
        result = self.results.get(gid, True)
        if result is None:
            return None
        member = self.members[gid]
        wins = member.beats_champion_on if result is True else tuple(result)
        return replace(member, beats_champion_on=wins, confirmed_round=9)


def _select(kwargs: dict[str, Any], confirm: Any) -> Any:
    return asyncio.run(_build_frontier_recombination_pair(**kwargs, confirm=confirm))


@pytest.fixture(autouse=True)
def _forget_logged_epochs() -> None:
    round_context._NO_FRONTIER_PAIR_LOGGED.clear()


# ---------------------------------------------------------------------------
# The pure selector
# ---------------------------------------------------------------------------


def test_without_wins_the_selector_is_the_rejects_selector() -> None:
    """``wins=None`` and a required champion are what every caller passed before."""
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",))
    assert rank_pairs([a, b]) == rank_pairs([a, b], wins=None) == (a, b)
    stale = _candidate("v3", parent="v1", mutation_ids=("m3",))
    assert eligible_parents([stale], champion_id="v4", manifest_ids=frozenset({"m3"})) == []
    assert eligible_parents([stale], champion_id=None, manifest_ids=frozenset({"m3"})) == [stale]


def test_a_frontier_pair_needs_disjoint_non_empty_wins() -> None:
    a = _candidate("v1", mutation_ids=("m1",), improved=())
    b = _candidate("v2", mutation_ids=("m2",), improved=())
    cost, rubric = frozenset({"cost:"}), frozenset({"rubric:a"})
    assert rank_pairs([a, b], wins={"v1": cost, "v2": rubric}) == (a, b)
    assert rank_pairs([a, b], wins={"v1": cost, "v2": cost}) is None
    assert rank_pairs([a, b], wins={"v1": cost | rubric, "v2": rubric}) is None
    assert rank_pairs([a, b], wins={"v1": cost, "v2": frozenset()}) is None
    assert rank_pairs([a, b], wins={"v1": cost}) is None
    # Predicates #5 and #7 still hold.
    tried = frozenset({frozenset({"v1", "v2"})})
    assert rank_pairs([a, b], wins={"v1": cost, "v2": rubric}, tried_pairs=tried) is None
    overlapping = replace(b, patch_mutation_ids=frozenset({"m1"}))
    assert rank_pairs([a, overlapping], wins={"v1": cost, "v2": rubric}) is None


def test_frontier_pairs_rank_by_axes_won_then_by_cross_regression() -> None:
    a = _candidate("v1", mutation_ids=("m1",), improved=())
    b = _candidate("v2", mutation_ids=("m2",), improved=())
    c = _candidate("v3", mutation_ids=("m3",), improved=(), regressed=("e7",))
    d = _candidate("v4", mutation_ids=("m4",), improved=())
    wins = {
        "v1": frozenset({"cost:"}),
        "v2": frozenset({"rubric:a"}),
        "v3": frozenset({"rubric:a", "judge:tone"}),
        "v4": frozenset({"rubric:b"}),
    }
    # (v1, v3) wins three axes between them; every other pair wins two.
    assert rank_pairs([a, b, c, d], wins=wins) == (a, c)
    # Among the two-axis pairs, the per-entry cross-regression penalty decides:
    # v3 regressed an entry, so a pair without it comes first.
    two_axis = {**wins, "v3": frozenset({"judge:tone"})}
    assert rank_pairs([b, c, d], wins=two_axis) == (b, d)


# ---------------------------------------------------------------------------
# The pool builder
# ---------------------------------------------------------------------------


def test_the_default_pool_never_touches_the_frontier(tmp_path: Path, monkeypatch) -> None:
    members = [_member("v1", "cost:"), _member("v2", "rubric:a")]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, "v1", mutation_id="m1"), _experiment(EPOCH, "v2", mutation_id="m2")],
    )
    monkeypatch.setattr(journal, "read_epoch_experiments", Mock(side_effect=AssertionError))
    confirm = Mock(side_effect=AssertionError)
    for experimental in ({}, {"recombine_pool": "rejects"}):
        kwargs["weights"] = _weights(**experimental)
        assert _select(kwargs, confirm) is None
    kwargs["weights"] = replace(
        _weights(recombine_pool="frontier"), experimental=ExperimentalConfig()
    )
    assert _select(kwargs, confirm) is None


def test_the_frontier_pool_never_falls_back_to_the_rejects(tmp_path: Path, caplog) -> None:
    """Two complementary rejects exist, the frontier is empty: nothing is minted."""
    kwargs = _seed(
        tmp_path,
        EPOCH,
        [],
        [_experiment(EPOCH, "v1", mutation_id="m1"), _experiment(EPOCH, "v2", mutation_id="m2")],
    )
    _write_loss(tmp_path, "v0", "retrieval", passes=False)
    _write_loss(tmp_path, "v0", "formatting", passes=False)
    _write_loss(tmp_path, "v1", "retrieval", passes=True)
    _write_loss(tmp_path, "v2", "formatting", passes=True)
    history = {k: v for k, v in kwargs.items()}
    rejects = replace(kwargs["weights"], experimental=ExperimentalConfig(recombine=True))
    assert _build_candidate_history(**{**history, "weights": rejects})[0] is not None
    assert _build_candidate_history(**history)[0] is None

    confirm = _Confirm([])
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        assert _select(kwargs, confirm) is None
        assert _select(kwargs, confirm) is None
    notes = [r for r in caplog.records if "no confirmed pair" in r.getMessage()]
    assert len(notes) == 1  # the second round of the epoch says nothing
    assert "rejects pool is not used" in notes[0].getMessage()
    assert confirm.calls == []


def test_a_pair_is_confirmed_before_it_is_returned(tmp_path: Path) -> None:
    members = [_member("v1", "cost:"), _member("v2", "rubric:a")]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, "v1", mutation_id="m1"), _experiment(EPOCH, "v2", mutation_id="m2")],
    )
    for entry in ("retrieval", "formatting", "withheld"):
        _write_loss(tmp_path, "v0", entry, passes=True)
        _write_loss(tmp_path, "v1", entry, passes=entry != "withheld")
        _write_loss(tmp_path, "v2", entry, passes=entry == "retrieval")
    confirm = _Confirm(members)
    pair = _select(kwargs, confirm)
    assert (pair.a_generation_id, pair.b_generation_id) == ("v1", "v2")
    assert confirm.calls == ["v1", "v2"]
    assert [p.mutation_id for p in (*pair.a_patches, *pair.b_patches)] == ["m1", "m2"]
    # The cross-regression count reads TRAIN entries only: v2 failed
    # ``formatting`` (train) and ``withheld`` (holdout); v1 failed ``withheld``.
    assert pair.combined_regressed_count == 1
    assert "withheld" not in repr(asdict(pair))
    # The mint is the ordinary one, and its child is proposed against the champion.
    child = mint_recombined_experiment(
        pair,
        epoch_id=EPOCH,
        parent_generation_id="v0",
        new_generation_id="v3",
        proposed_at="2026-01-02T00:00:00Z",
    )
    assert child.recombined_from == ("v1", "v2")
    assert child.parent_generation_id == "v0"


def test_members_already_confirmed_against_this_champion_cost_nothing(tmp_path: Path) -> None:
    members = [_member("v1", "cost:", confirmed=4), _member("v2", "rubric:a", confirmed=5)]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, "v1", mutation_id="m1"), _experiment(EPOCH, "v2", mutation_id="m2")],
    )
    confirm = _Confirm(members)
    assert _select(kwargs, confirm) is not None
    assert confirm.calls == []
    # Confirmed against an EARLIER champion is not confirmed against this one.
    stale = [replace(m, champion_generation_id="v9") for m in members]
    save_frontier(tmp_path, EPOCH, ParetoFrontier(epoch_id=EPOCH, members=tuple(stale)))
    assert _select(kwargs, confirm) is not None
    assert confirm.calls == ["v1", "v2"]


def test_a_retired_member_drops_out_and_the_next_pair_is_tried(tmp_path: Path) -> None:
    members = [
        _member("v1", "cost:", "judge:tone"),
        _member("v2", "rubric:a"),
        _member("v3", "rubric:b"),
    ]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, f"v{i}", mutation_id=f"m{i}") for i in (1, 2, 3)],
    )
    confirm = _Confirm(members, v2=None)
    pair = _select(kwargs, confirm)
    assert (pair.a_generation_id, pair.b_generation_id) == ("v1", "v3")
    assert confirm.calls == ["v1", "v2", "v3"]


def test_a_pair_whose_confirmed_wins_overlap_is_not_minted(tmp_path: Path) -> None:
    """The record said cost: and rubric:a; the fresh draws say both win cost:."""
    members = [_member("v1", "cost:"), _member("v2", "rubric:a")]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, "v1", mutation_id="m1"), _experiment(EPOCH, "v2", mutation_id="m2")],
    )
    confirm = _Confirm(members, v2=("cost:",))
    assert _select(kwargs, confirm) is None
    assert confirm.calls == ["v1", "v2"]


def test_confirmation_spend_is_capped_per_round(tmp_path: Path, caplog) -> None:
    """Six members with pairwise disjoint wins, none of which survives."""
    gids = [f"v{i}" for i in range(1, 7)]
    members = [_member(gid, f"rubric:{gid}") for gid in gids]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [_experiment(EPOCH, gid, mutation_id=f"m{gid[1:]}") for gid in gids],
    )
    confirm = _Confirm(members, **dict.fromkeys(gids))
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        assert _select(kwargs, confirm) is None
    assert len(confirm.calls) == RECOMBINE_CONFIRM_MAX == 4
    assert f"{RECOMBINE_CONFIRM_MAX} confirmation(s) this round" in caplog.text


def test_a_member_whose_patch_would_overwrite_a_later_promotion_is_not_pooled(
    tmp_path: Path,
) -> None:
    """v3 was promoted over v0 by editing m1; v1 patches m1 from v0's tree."""
    members = [
        _member("v1", "cost:", champion="v3"),
        _member("v2", "rubric:a", champion="v3"),
        _member("v4", "judge:tone", champion="v3"),
        _member("v5", "rubric:b", champion="v3"),
    ]
    kwargs = _seed(
        tmp_path,
        EPOCH,
        members,
        [
            _experiment(EPOCH, "v1", mutation_id="m1"),
            _experiment(EPOCH, "v2", mutation_id="m2"),
            _experiment(EPOCH, "v3", mutation_id="m1", decision="promoted"),
            _experiment(EPOCH, "v4", mutation_id="m4", parent="v3"),
            # Proposed against a generation that is not on the champion's lineage.
            _experiment(EPOCH, "v5", mutation_id="m5", parent="v2"),
        ],
    )
    kwargs["parent_id"] = "v3"
    confirm = _Confirm(members)
    pair = _select(kwargs, confirm)
    assert (pair.a_generation_id, pair.b_generation_id) == ("v2", "v4")
    assert confirm.calls == ["v2", "v4"]


# ---------------------------------------------------------------------------
# The pool builder over the real confirmation
# ---------------------------------------------------------------------------


def test_the_pool_confirms_through_the_real_measurement_on_train_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selection, the generation store, ``run_matchup`` and the record, together.

    v1 is cheaper than the champion and v2 scores a higher rubric, so the
    fresh draws uphold both and the pair is returned. v3's recorded judge win
    does not reproduce, so a pool of v1 and v3 retires v3 and mints nothing.
    """
    board = [
        BoardEntry(id=name, kind="single_turn", wall_clock_budget_seconds=60, input="x")
        for name in ("retrieval", "formatting")
    ]
    board.append(replace(board[0], id="withheld", tags=("holdout",)))
    config = runtime_config(tmp_path)
    weights = _weights(recombine_pool="frontier")
    epoch_id = prepare_tournament_epoch(tmp_path, config, board, weights)
    seed_tournament_generations(tmp_path, epoch_id, "directory", ("v0", "v1", "v2", "v3"))
    metrics = {
        "v0": {"cost:tokens": 1000, "rubric:quality": 0.5},
        "v1": {"cost:tokens": 400, "rubric:quality": 0.5},
        "v2": {"cost:tokens": 1000, "rubric:quality": 0.9},
        "v3": {"cost:tokens": 1000, "rubric:quality": 0.5},
    }
    runs: list[tuple[str, str, str]] = []

    async def fake_run_single(*, generation: Any, entry: Any, epoch_id: str, **_: Any) -> Any:
        draw = MeasurementDraw.from_context(entry.context)
        runs.append((generation.id, entry.id, str(draw.purpose)))
        return LossProfile(
            run_id=run_id_for_unit(generation.id, entry.id, draw, epoch_id=epoch_id),
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            metric_counts=tuple(
                MetricCount(name=name, severity="info", count=value)
                for name, value in metrics[generation.id].items()
            ),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=0.0,
            pass_fail=True,
        )

    monkeypatch.setattr(_tournament_worker_execution, "_run_single", fake_run_single)
    events: list[dict[str, Any]] = []
    champion = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=tmp_path / "unused",
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    confirm = frontier_confirmer(
        champion=champion,
        workspace_root=tmp_path,
        epoch_id=epoch_id,
        board=board,
        weights=weights,
        round_index=6,
        round_log=SimpleNamespace(emit=lambda _token, payload: events.append(payload)),
        adapter=object(),
        config=config,
    )
    members = [_member("v1", "cost:"), _member("v2", "rubric:"), _member("v3", "judge:")]
    kwargs = _seed(
        tmp_path,
        epoch_id,
        members[:2],
        [_experiment(epoch_id, f"v{i}", mutation_id=f"m{i}") for i in (1, 2, 3)],
    )

    pair = _select(kwargs, confirm)
    assert (pair.a_generation_id, pair.b_generation_id) == ("v1", "v2")
    assert {entry for _g, entry, _p in runs} == {"retrieval", "formatting"}
    assert {purpose for _g, _e, purpose in runs} == {"evidence_confirmation"}
    # The champion is measured once; the second confirmation reuses its draw.
    assert [(e["units_fresh"], e["units_cached"]) for e in events] == [(4, 0), (2, 2)]
    record = load_frontier(tmp_path, epoch_id)
    assert [(m.generation_id, m.confirmed_round) for m in record.members] == [("v1", 6), ("v2", 6)]

    # A member the fresh draws do not uphold is retired and never merged.
    save_frontier(
        tmp_path,
        epoch_id,
        replace(record, members=(record.members[0], members[2])),
    )
    assert _select(kwargs, confirm) is None
    retired = load_frontier(tmp_path, epoch_id).retired
    assert [(r.member.generation_id, r.reason) for r in retired] == [("v3", "unconfirmed")]


# ---------------------------------------------------------------------------
# Steering
# ---------------------------------------------------------------------------

_DIMENSIONS = ["judge:tone", "judge:facts", "rubric:a", "rubric:b", "cost:tokens"]


def _steering_record(root: Path, *members: FrontierMember) -> None:
    save_frontier(root, EPOCH, ParetoFrontier(epoch_id=EPOCH, members=members))


def test_only_members_confirmed_against_this_champion_count(tmp_path: Path) -> None:
    confirmed = replace(
        _member("v1", "rubric:a", "cost:", confirmed=3),
        axis_values={"rubric:a": 0.0, "rubric:b": 0.0, "cost:": 0.0},
    )
    _steering_record(
        tmp_path,
        confirmed,
        _member("v2", "judge:tone"),  # not confirmed
        _member("v3", "judge:facts", confirmed=2, champion="v9"),  # an earlier champion
    )
    # v1 wins rubric:a outright, and cost: stands for cost:tokens because v1
    # carries no separate axis for that metric.
    assert unimproved_dimensions(tmp_path, EPOCH, "v0", _DIMENSIONS) == (
        "judge:tone",
        "judge:facts",
        "rubric:b",
    )


def test_a_namespace_win_does_not_cover_a_dimension_the_member_measured(tmp_path: Path) -> None:
    member = replace(
        _member("v1", "rubric:", confirmed=3),
        axis_values={"rubric:": 0.0, "rubric:a": 0.0, "rubric:b": 0.0},
    )
    _steering_record(tmp_path, member)
    assert unimproved_dimensions(tmp_path, EPOCH, "v0", ["rubric:a", "rubric:b"]) == (
        "rubric:a",
        "rubric:b",
    )


def test_with_no_confirmed_member_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert unimproved_dimensions(tmp_path, EPOCH, "v0", _DIMENSIONS) == ()
    _steering_record(tmp_path, _member("v2", "judge:tone"))
    assert unimproved_dimensions(tmp_path, EPOCH, "v0", _DIMENSIONS) == ()
    (tmp_path / "epochs" / EPOCH / "pareto_frontier.json").write_text("{not json")
    assert unimproved_dimensions(tmp_path, EPOCH, "v0", _DIMENSIONS) == ()


def test_the_steering_block_names_dimensions_and_nothing_else() -> None:
    assert render_frontier_steering_block([]) == ""
    block = render_frontier_steering_block(["schema:x", "judge:y"])
    assert block == (
        "No candidate measured against the current champion has yet improved: "
        "schema:x, judge:y.\n"
        "A change that improves one of these covers ground the search has not."
    )
    many = [f"rubric:m{i:02d}" for i in range(20)]
    capped = render_frontier_steering_block(many)
    assert capped.count("rubric:") == FRONTIER_STEERING_MAX_DIMENSIONS == 8
    assert "rubric:m08" not in capped
    assert not any(ch.isdigit() for ch in block)  # no magnitude, count, round or id


def _steered_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, steering: bool
) -> tuple[str, Mock]:
    """One real round; returns the metric block the proposer context received."""
    import zicato.evolve.field as field_module
    import zicato.evolve.round_entry as round_entry

    base = deterministic_weights(
        promote_margin=_MARGIN, tournament_structure=TournamentStructure.gauntlet()
    )
    weights = replace(base, experimental=ExperimentalConfig(frontier_steering=steering))
    tmp_path.mkdir()
    workspace, epoch_id = bootstrap_workspace(tmp_path, weights=weights)
    install_stub_adapter_factory(monkeypatch)
    drift = {"v0": 1.0, "v1": 3.0}
    install_telemetry_stubs(
        monkeypatch, canned_loss_by_gen=drift, canned_pass_by_gen=dict.fromkeys(drift, True)
    )
    _install_costed_run_single(monkeypatch, drift_by_gen=drift, tokens_by_gen={"v0": 9, "v1": 5})
    save_frontier(
        workspace,
        epoch_id,
        ParetoFrontier(epoch_id=epoch_id, members=(_member("v7", "failure:", confirmed=1),)),
    )
    reader = Mock(wraps=round_entry.unimproved_dimensions)
    monkeypatch.setattr(round_entry, "unimproved_dimensions", reader)
    seen: list[str] = []
    original = field_module.evolve_field_round

    async def capture(prepared: Any, **kwargs: Any) -> Any:
        seen.append(prepared.metric_priorities)
        return await original(prepared, **kwargs)

    monkeypatch.setattr(field_module, "evolve_field_round", capture)
    run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    return seen[0], reader


def test_steering_off_leaves_the_prompt_block_untouched_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    off, reader = _steered_round(monkeypatch, tmp_path / "off", steering=False)
    assert reader.call_count == 0
    assert "No candidate measured" not in off

    on, reader = _steered_round(monkeypatch, tmp_path / "on", steering=True)
    assert reader.call_count == 1
    # Everything before the steering block is byte-for-byte the block with it off.
    head, _, steering = on.partition("\n\nNo candidate measured")
    assert head == off
    assert steering.startswith(" against the current champion has yet improved: ")
    assert "v7" not in on
