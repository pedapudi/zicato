"""The per-epoch Pareto frontier RECORD (docs/design/PARETO-FRONTIER.md).

The promote gate keeps one generation per round and picks it by a weighted
sum, so a challenger that wins on an under-weighted axis is rejected and
forgotten. These pin the record that remembers it — and, just as important,
pin that the record stays a RECORD: nothing here touches the gate, selection,
the proposer, or the champion pointer.

Four layers:

* the pure dominance algebra (the margin band, the negative-weight axis, the
  incomparable cases);
* admission, which is what keeps a degenerate cut-everything candidate off
  the record;
* the champion re-evaluation a promotion triggers, and the retire-never-delete
  discipline;
* the real settle path, driven through ``evolve_once`` / ``evolve_n_rounds``
  with the orchestrator suite's stubbed harness, plus the index projection.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.test_orchestrator import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.core import DriftCount, ExpectationResult, LossProfile, MetricCount, ScoringWeights
from zicato.epoch.pareto import (
    FrontierCandidate,
    FrontierMember,
    ParetoFrontier,
    RetiredMember,
    axis_values,
    beats_on,
    dominates,
    frontier_axes,
    frontier_path,
    load_frontier,
    record_frontier,
    save_frontier,
    update_frontier,
)

_MARGIN = 0.01


def _weights(**kwargs: Any) -> ScoringWeights:
    return ScoringWeights(promote_margin=_MARGIN, **kwargs)


def _agg(
    *,
    drift: float = 0.0,
    cost: float = 0.0,
    rubric: float = 0.0,
    schema: float = 0.0,
    latency: float = 0.0,
    scalar: float | None = None,
) -> dict[str, Any]:
    """An aggregate-shaped dict whose namespace values are already WEIGHTED.

    Which is the on-the-wire form: ``aggregate_namespaced_metrics`` folds each
    namespace's signed weight in before returning, so every value here is in
    scalar points and lower is better on all of them.
    """
    body: dict[str, Any] = {
        "namespace_aggregates": {
            "drift:": drift,
            "cost:": cost,
            "latency:": latency,
            "rubric:": rubric,
            "schema:": schema,
        }
    }
    if scalar is not None:
        body["scalar"] = scalar
    return body


def _candidate(gid: str, **kwargs: Any) -> FrontierCandidate:
    placebo = bool(kwargs.pop("is_placebo", False))
    return FrontierCandidate(generation_id=gid, aggregate=_agg(**kwargs), is_placebo=placebo)


# ---------------------------------------------------------------------------
# The axes
# ---------------------------------------------------------------------------


def test_frontier_axes_are_the_non_zero_namespace_weights() -> None:
    """``output:`` is excluded: a zero weight has no direction to optimise."""
    assert frontier_axes(_weights()) == ("cost:", "drift:", "latency:", "rubric:", "schema:")


def test_an_operator_zeroed_axis_drops_out() -> None:
    weights = _weights(
        namespace_weights={"drift:": 1.0, "cost:": 0.0, "rubric:": -1.0},
    )
    assert frontier_axes(weights) == ("drift:", "rubric:")


def test_axis_values_omit_a_missing_or_non_finite_measurement() -> None:
    """A missing measurement is not a measurement of zero.

    Defaulting it would invent a dominance relation out of an absence.
    """
    axes = ("cost:", "drift:")
    partial = {"namespace_aggregates": {"drift:": 1.0}}
    assert axis_values(partial, axes) == {"drift:": 1.0}
    poisoned = {"namespace_aggregates": {"drift:": float("nan"), "cost:": 2.0}}
    assert axis_values(poisoned, axes) == {"cost:": 2.0}


def test_a_negative_weight_axis_reads_lower_is_better_after_folding() -> None:
    """The rubric axis, through the REAL aggregator.

    ``rubric:`` carries a negative weight, so a HIGHER raw rubric score must
    come out as a LOWER (better) axis value. This is the property that lets
    every comparison in this module skip the sign entirely.
    """
    from zicato.tournament.scoring import aggregate_generation_score

    weights = _weights()

    def _rubric_loss(quality: float) -> LossProfile:
        return LossProfile(
            run_id="r",
            entry_id="e",
            generation_id="g",
            epoch_id="ep",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1,
            wall_clock_budget_exceeded=False,
            expectation_result=ExpectationResult(kind="predicate", passed=True),
            drift_loss=0.0,
            pass_fail=True,
            metric_counts=(MetricCount(name="rubric:quality", severity="", count=quality),),
        )

    axes = frontier_axes(weights)
    better = axis_values(aggregate_generation_score([_rubric_loss(0.9)], weights), axes)
    worse = axis_values(aggregate_generation_score([_rubric_loss(0.5)], weights), axes)
    assert better["rubric:"] == pytest.approx(-0.9)
    assert worse["rubric:"] == pytest.approx(-0.5)
    # Lower is better, uniformly — so the higher-quality run has the lower value.
    assert better["rubric:"] < worse["rubric:"]
    assert dominates(better, worse, margin=_MARGIN)
    assert not dominates(worse, better, margin=_MARGIN)


# ---------------------------------------------------------------------------
# Dominance
# ---------------------------------------------------------------------------


def test_dominance_needs_a_margin_sized_win_somewhere() -> None:
    left = {"drift:": 1.0, "cost:": 1.0}
    exact = {"drift:": 1.0, "cost:": 1.0 + _MARGIN}
    assert dominates(left, exact, margin=_MARGIN)
    just_short = {"drift:": 1.0, "cost:": 1.0 + _MARGIN / 2}
    assert not dominates(left, just_short, margin=_MARGIN)


def test_a_margin_sized_loss_on_any_axis_blocks_dominance() -> None:
    left = {"drift:": 1.0, "cost:": 0.0}
    right = {"drift:": 1.0 + _MARGIN, "cost:": _MARGIN}
    assert dominates(left, right, margin=_MARGIN)
    # Same win on cost, but now a margin-sized loss on drift.
    left_worse_on_drift = {"drift:": 1.0 + _MARGIN, "cost:": 0.0}
    right_two = {"drift:": 1.0, "cost:": _MARGIN}
    assert not dominates(left_worse_on_drift, right_two, margin=_MARGIN)


def test_everything_inside_the_band_is_a_tie_both_ways() -> None:
    """A candidate a hair better everywhere dominates nothing.

    ``promote_margin`` is the width at which the loop has already agreed a
    difference is not noise, so inside it the two are tied.
    """
    left = {"drift:": 1.0, "cost:": 1.0}
    right = {"drift:": 1.0 + _MARGIN / 2, "cost:": 1.0 + _MARGIN / 2}
    assert not dominates(left, right, margin=_MARGIN)
    assert not dominates(right, left, margin=_MARGIN)


def test_a_trade_off_pair_dominates_neither_way() -> None:
    cheap = {"drift:": 2.0, "cost:": 0.5}
    accurate = {"drift:": 0.5, "cost:": 2.0}
    assert not dominates(cheap, accurate, margin=_MARGIN)
    assert not dominates(accurate, cheap, margin=_MARGIN)


def test_identical_candidates_dominate_neither_way() -> None:
    same = {"drift:": 1.0, "cost:": 1.0}
    assert not dominates(same, dict(same), margin=_MARGIN)


def test_a_zero_margin_degrades_to_strict_pareto_dominance() -> None:
    """``promote_margin`` is not validated positive, so zero must still work.

    At ``margin == 0`` a bare ``>= margin`` is satisfied by an exact tie,
    which would make the relation reflexive (every candidate dominating
    itself) and symmetric on identical points — neither of which a partial
    order may be — while a tie on the WORSE limb would veto the textbook
    dominant case. Zero must mean strict Pareto dominance, nothing else.
    """
    same = {"drift:": 1.0, "cost:": 1.0}
    assert not dominates(same, dict(same), margin=0.0)
    assert not dominates(dict(same), same, margin=0.0)
    assert beats_on(same, dict(same), margin=0.0) == ()

    # Strictly better on one axis, exactly tied on the other: dominant.
    better = {"drift:": 0.5, "cost:": 1.0}
    assert dominates(better, same, margin=0.0)
    assert not dominates(same, better, margin=0.0)
    assert beats_on(better, same, margin=0.0) == ("drift:",)


def test_a_zero_margin_keeps_no_information_ties_off_the_record() -> None:
    """The admission rule at ``margin == 0``, end to end.

    A candidate identical to the champion carries nothing the champion does
    not already carry, which is admission rule 5 — and it must hold at every
    margin, not only the default.
    """
    weights = ScoringWeights(promote_margin=0.0)
    champion = _candidate("v0", drift=1.0, cost=1.0)
    update = update_frontier(
        ParetoFrontier(epoch_id="e0"),
        champion=champion,
        candidates=[_candidate("v1", drift=1.0, cost=1.0)],
        weights=weights,
        round_index=1,
    )
    assert update.admitted == ()
    assert update.frontier.members == ()
    # A genuine strict win on one axis still lands.
    real = update_frontier(
        ParetoFrontier(epoch_id="e0"),
        champion=champion,
        candidates=[_candidate("v2", drift=1.0, cost=0.5)],
        weights=weights,
        round_index=1,
    )
    assert real.admitted == ("v2",)


def test_no_shared_axis_is_incomparable_not_dominant() -> None:
    assert not dominates({"drift:": 0.0}, {"cost:": 9.0}, margin=_MARGIN)
    assert not dominates({"cost:": 9.0}, {"drift:": 0.0}, margin=_MARGIN)


def test_beats_on_names_only_margin_sized_wins_sorted() -> None:
    candidate = {"cost:": 0.0, "drift:": 1.0, "schema:": 5.0}
    champion = {"cost:": 1.0, "drift:": 1.0, "schema:": 5.0 + _MARGIN / 2}
    assert beats_on(candidate, champion, margin=_MARGIN) == ("cost:",)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _empty(epoch_id: str = "e0") -> ParetoFrontier:
    return ParetoFrontier(epoch_id=epoch_id)


def test_a_challenger_that_wins_on_an_under_weighted_axis_is_admitted() -> None:
    """The whole point: the scalar rejected it, the record keeps it."""
    champion = _candidate("v0", drift=1.0, cost=1.0, scalar=2.0)
    cheap = _candidate("v1", drift=3.0, cost=0.5, scalar=3.5)
    update = update_frontier(
        _empty(),
        champion=champion,
        candidates=[cheap],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ("v1",)
    assert update.changed
    member = update.frontier.members[0]
    assert member.generation_id == "v1"
    assert member.beats_champion_on == ("cost:",)
    assert member.champion_generation_id == "v0"
    assert member.round_admitted == 1
    assert member.scalar == pytest.approx(3.5)


def test_a_candidate_that_beats_the_champion_nowhere_is_not_admitted() -> None:
    """Strictly worse carries no information the champion does not carry."""
    update = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=2.0)],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()
    assert update.frontier.members == ()
    assert not update.changed


def test_a_monotonicity_regressing_candidate_is_refused() -> None:
    """The control that keeps a cut-everything candidate off the record.

    This challenger halves cost — a genuine, margin-sized win — but its
    rubric collapsed. ``namespace_monotonicity`` guards ``rubric:`` by
    default, so it is refused however good the cost number is.
    """
    champion = _candidate("v0", cost=1.0, rubric=-0.9)
    gutted = _candidate("v1", cost=0.5, rubric=-0.5)
    update = update_frontier(
        _empty(),
        champion=champion,
        candidates=[gutted],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()
    assert update.frontier.members == ()


def test_a_schema_regressing_candidate_is_refused() -> None:
    """The other default-on monotonicity namespace: introduced failures."""
    update = update_frontier(
        _empty(),
        champion=_candidate("v0", cost=1.0, schema=0.0),
        candidates=[_candidate("v1", cost=0.0, schema=5.0)],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()


def test_a_placebo_arm_is_refused() -> None:
    """A random-baseline arm is a calibration probe, never a candidate.

    It is a no-op re-emission of the champion, so without this check it
    would sit on the record forever as a permanent tie. The multi-challenger
    path fields it INSIDE the slate, so this is load-bearing.
    """
    update = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v9", drift=3.0, cost=0.5, is_placebo=True)],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()


def test_an_unmeasured_candidate_is_refused() -> None:
    """No finite axis value means nothing settled — nothing to record."""
    update = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[FrontierCandidate(generation_id="v1", aggregate={})],
        weights=_weights(),
        round_index=1,
    )
    assert update.admitted == ()


def test_a_candidate_an_existing_member_dominates_is_not_admitted() -> None:
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=0.2)],
        weights=weights,
        round_index=1,
    )
    # v2 also beats the champion on cost, but v1 already beats v2 on both.
    second = update_frontier(
        first.frontier,
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v2", drift=4.0, cost=0.5)],
        weights=weights,
        round_index=2,
    )
    assert second.admitted == ()
    assert [m.generation_id for m in second.frontier.members] == ["v1"]


def test_an_admission_retires_the_members_it_dominates() -> None:
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=4.0, cost=0.5)],
        weights=weights,
        round_index=1,
    )
    second = update_frontier(
        first.frontier,
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v2", drift=3.0, cost=0.2)],
        weights=weights,
        round_index=2,
    )
    assert second.admitted == ("v2",)
    assert second.retired == ("v1",)
    assert [m.generation_id for m in second.frontier.members] == ["v2"]
    retired = second.frontier.retired[0]
    assert retired.member.generation_id == "v1"
    assert retired.reason == "dominated_by:v2"
    assert retired.round_retired == 2


def test_the_champion_is_a_reference_never_a_member() -> None:
    update = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v0", drift=1.0, cost=1.0)],
        weights=_weights(),
        round_index=1,
    )
    assert [m.generation_id for m in update.frontier.members] == []


def test_admission_is_idempotent_across_a_re_settle() -> None:
    weights = _weights()
    champion = _candidate("v0", drift=1.0, cost=1.0)
    candidate = _candidate("v1", drift=3.0, cost=0.5)
    first = update_frontier(
        _empty(), champion=champion, candidates=[candidate], weights=weights, round_index=1
    )
    again = update_frontier(
        first.frontier, champion=champion, candidates=[candidate], weights=weights, round_index=1
    )
    assert again.admitted == ()
    assert not again.changed
    assert [m.generation_id for m in again.frontier.members] == ["v1"]


# ---------------------------------------------------------------------------
# Champion re-evaluation on promotion
# ---------------------------------------------------------------------------


def test_a_promotion_retires_the_member_that_became_champion() -> None:
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=0.5)],
        weights=weights,
        round_index=1,
    )
    # v1 wins a later round and becomes the champion.
    second = update_frontier(
        first.frontier,
        champion=_candidate("v1", drift=3.0, cost=0.5),
        candidates=[],
        weights=weights,
        round_index=2,
    )
    assert second.retired == ("v1",)
    assert second.frontier.members == ()
    assert second.frontier.retired[0].reason == "promoted"


def test_a_promotion_retires_members_the_new_champion_dominates() -> None:
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=0.5)],
        weights=weights,
        round_index=1,
    )
    # v2 promotes and is better than v1 on BOTH axes.
    second = update_frontier(
        first.frontier,
        champion=_candidate("v2", drift=0.5, cost=0.4),
        candidates=[],
        weights=weights,
        round_index=2,
    )
    assert second.retired == ("v1",)
    assert second.frontier.retired[0].reason == "dominated_by_champion"


def test_a_promotion_retires_members_that_regress_against_it() -> None:
    """A new champion can raise the quality bar a member no longer clears."""
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", cost=1.0, rubric=-0.5),
        candidates=[_candidate("v1", cost=0.5, rubric=-0.5)],
        weights=weights,
        round_index=1,
    )
    assert first.admitted == ("v1",)
    second = update_frontier(
        first.frontier,
        champion=_candidate("v2", cost=0.9, rubric=-0.9),
        candidates=[],
        weights=weights,
        round_index=2,
    )
    assert second.retired == ("v1",)
    assert second.frontier.retired[0].reason == "monotonicity_regression"


def test_a_promotion_keeps_a_member_it_does_not_dominate() -> None:
    weights = _weights()
    first = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=0.2)],
        weights=weights,
        round_index=1,
    )
    second = update_frontier(
        first.frontier,
        champion=_candidate("v2", drift=0.5, cost=0.9),
        candidates=[],
        weights=weights,
        round_index=2,
    )
    assert second.retired == ()
    assert [m.generation_id for m in second.frontier.members] == ["v1"]
    # Its provenance still names the champion it was admitted against.
    assert second.frontier.members[0].champion_generation_id == "v0"
    assert second.frontier.champion_generation_id == "v2"


def test_retired_members_accumulate_and_are_never_deleted() -> None:
    weights = _weights()
    frontier = _empty()
    frontier = update_frontier(
        frontier,
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=4.0, cost=0.5)],
        weights=weights,
        round_index=1,
    ).frontier
    frontier = update_frontier(
        frontier,
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v2", drift=3.0, cost=0.2)],
        weights=weights,
        round_index=2,
    ).frontier
    frontier = update_frontier(
        frontier,
        champion=_candidate("v3", drift=0.1, cost=0.1),
        candidates=[],
        weights=weights,
        round_index=3,
    ).frontier
    assert frontier.members == ()
    assert [(r.member.generation_id, r.round_retired) for r in frontier.retired] == [
        ("v1", 2),
        ("v2", 3),
    ]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_an_absent_record_reads_as_an_empty_frontier(tmp_path: Path) -> None:
    """Never an error on a workspace that predates the feature."""
    frontier = load_frontier(tmp_path, "e0")
    assert frontier.members == ()
    assert frontier.retired == ()
    assert frontier.epoch_id == "e0"


def test_the_record_round_trips_through_disk(tmp_path: Path) -> None:
    member = FrontierMember(
        generation_id="v1",
        round_admitted=3,
        champion_generation_id="v0",
        axis_values={"cost:": 0.5, "drift:": 3.0},
        beats_champion_on=("cost:",),
        scalar=3.5,
    )
    original = ParetoFrontier(
        epoch_id="e0",
        axes=("cost:", "drift:"),
        margin=_MARGIN,
        champion_generation_id="v0",
        updated_round=3,
        members=(member,),
        retired=(RetiredMember(member=member, round_retired=4, reason="dominated_by:v2"),),
    )
    save_frontier(tmp_path, "e0", original)
    assert load_frontier(tmp_path, "e0") == original


def test_a_round_that_moves_nothing_leaves_the_file_untouched(tmp_path: Path) -> None:
    """The record's mtime means "something happened", not "a round ran"."""
    weights = _weights()
    record_frontier(
        tmp_path,
        "e0",
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=3.0, cost=0.5)],
        weights=weights,
        round_index=1,
    )
    path = frontier_path(tmp_path, "e0")
    before = path.read_bytes()
    update = record_frontier(
        tmp_path,
        "e0",
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v2", drift=9.0, cost=9.0)],
        weights=weights,
        round_index=2,
    )
    assert not update.changed
    assert path.read_bytes() == before


def test_a_future_format_version_is_refused_not_misread(tmp_path: Path) -> None:
    from zicato.epoch._storage import RecordFormatError

    path = frontier_path(tmp_path, "e0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format_version": 99, "members": []}))
    with pytest.raises(RecordFormatError):
        load_frontier(tmp_path, "e0")


# ---------------------------------------------------------------------------
# The analytical-index projection
# ---------------------------------------------------------------------------


def test_the_index_schema_carries_the_frontier_table() -> None:
    from zicato.index.schema import SCHEMA_VERSION, apply_schema, read_schema_version

    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    assert SCHEMA_VERSION >= 13
    assert read_schema_version(conn) >= 13
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pareto_frontier" in tables
    conn.close()


def test_a_v12_database_gains_the_table_in_place() -> None:
    """v13 adds a WHOLE table, so the migration needs no column ALTER."""
    from zicato.index.schema import apply_schema, read_schema_version

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    apply_schema(conn)
    assert read_schema_version(conn) >= 13
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pareto_frontier" in tables
    conn.close()


def test_the_index_projection_is_derived_from_the_workspace_record(tmp_path: Path) -> None:
    """Files are canonical; the table is a pure projection of them."""
    from zicato.index.ingest import ingest_pareto_frontier
    from zicato.index.schema import apply_schema

    weights = _weights()
    frontier = update_frontier(
        _empty(),
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v1", drift=4.0, cost=0.5, scalar=4.5)],
        weights=weights,
        round_index=1,
    ).frontier
    frontier = update_frontier(
        frontier,
        champion=_candidate("v0", drift=1.0, cost=1.0),
        candidates=[_candidate("v2", drift=3.0, cost=0.2, scalar=3.2)],
        weights=weights,
        round_index=2,
    ).frontier
    save_frontier(tmp_path, "e0", frontier)

    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(db_path)
    apply_schema(conn)
    conn.commit()
    conn.close()

    ingest_pareto_frontier(tmp_path, db_path, "e0")
    conn = sqlite3.connect(db_path)
    rows = {
        gid: (status, reason)
        for gid, status, reason in conn.execute(
            "SELECT generation_id, status, retired_reason FROM pareto_frontier "
            "WHERE epoch_id = 'e0'"
        )
    }
    assert rows == {"v2": ("member", None), "v1": ("retired", "dominated_by:v2")}
    beats = conn.execute(
        "SELECT beats_champion_on_json FROM pareto_frontier WHERE generation_id = 'v2'"
    ).fetchone()[0]
    assert json.loads(beats) == ["cost:"]
    conn.close()

    # Idempotent: a re-ingest reproduces the file, never duplicates it.
    ingest_pareto_frontier(tmp_path, db_path, "e0")
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pareto_frontier").fetchone()[0] == 2
    conn.close()


def test_an_epoch_with_no_record_projects_no_rows(tmp_path: Path) -> None:
    from zicato.index.ingest import ingest_pareto_frontier

    db_path = tmp_path / "index.db"
    ingest_pareto_frontier(tmp_path, db_path, "e0")
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pareto_frontier").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# The real settle path
# ---------------------------------------------------------------------------


def _install_costed_run_single(
    monkeypatch: pytest.MonkeyPatch,
    *,
    drift_by_gen: dict[str, float],
    tokens_by_gen: dict[str, int],
) -> None:
    """Re-stub ``runner._run_single`` with a per-generation COST signal.

    The orchestrator suite's stub varies only ``drift_loss``, which moves one
    axis. The frontier is about candidates that trade one axis for another, so
    this adds ``tokens_spent`` — which ``LossProfile.unified_metrics`` lifts
    into the ``cost:`` namespace, exactly as the real reducer does.
    """
    import zicato.tournament.runner as _runner_mod

    async def _fake_run_single(
        *,
        adapter: Any,
        generation: Any,
        entry: Any,
        weights: Any,
        config: Any,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, side, match_id, workspace_root
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=(
                ExpectationResult(kind="predicate", passed=True)
                if entry.expectation is not None
                else None
            ),
            drift_loss=drift_by_gen.get(generation.id, 0.0),
            pass_fail=True,
            tokens_spent=tokens_by_gen.get(generation.id, 0),
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _fake_run_single)


def _drive_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    drift_by_gen: dict[str, float],
    tokens_by_gen: dict[str, int],
) -> tuple[Path, str, Any]:
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen=drift_by_gen,
        canned_pass_by_gen=dict.fromkeys(drift_by_gen, True),
    )
    _install_costed_run_single(monkeypatch, drift_by_gen=drift_by_gen, tokens_by_gen=tokens_by_gen)

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response() for _ in range(6)]),
        )
    )
    return workspace, epoch_id, outcome


def test_a_rejected_but_nondominated_challenger_lands_on_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The issue's whole case, through the real gauntlet settle path.

    v1 costs half what the champion does but drifts more. The weighted sum
    outvotes the cost win, so the gate rejects it — and the record keeps it.
    """
    workspace, epoch_id, outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    assert outcome.tournament_decision == "rejected"

    frontier = load_frontier(workspace, epoch_id)
    assert [m.generation_id for m in frontier.members] == ["v1"]
    member = frontier.members[0]
    assert member.beats_champion_on == ("cost:",)
    assert member.champion_generation_id == "v0"
    assert member.axis_values["cost:"] == pytest.approx(0.5)
    assert member.axis_values["drift:"] == pytest.approx(3.0)

    # The champion pointer is untouched — the record decides nothing.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists() or marker.read_text().strip() == "v0"


def test_a_dominated_challenger_does_not_land_on_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Worse on drift AND worse on cost — nothing to remember."""
    workspace, epoch_id, outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 2000},
    )
    assert outcome.tournament_decision == "rejected"
    assert load_frontier(workspace, epoch_id).members == ()
    # Nothing moved, so nothing was written.
    assert not frontier_path(workspace, epoch_id).exists()


def test_the_round_log_records_the_frontier_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zicato.epoch.round_log import RoundLog, fold_round_record

    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    rounds = sorted((workspace / "epochs" / epoch_id / "rounds").iterdir())
    assert len(rounds) == 1
    record = fold_round_record(RoundLog(workspace, epoch_id, int(rounds[0].name)).read())
    assert len(record.frontier_updates) == 1
    event = record.frontier_updates[0]
    assert event.admitted == ("v1",)
    assert event.retired == ()
    assert event.size == 1


def test_a_promotion_retires_a_newly_dominated_member_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round 1 records the cheap loser; round 2's champion dominates it."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    drift_by_gen = {"v0": 1.0, "v1": 3.0, "v2": 0.5}
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen=drift_by_gen,
        canned_pass_by_gen=dict.fromkeys(drift_by_gen, True),
    )
    _install_costed_run_single(
        monkeypatch,
        drift_by_gen=drift_by_gen,
        tokens_by_gen={"v0": 1000, "v1": 500, "v2": 400},
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response() for _ in range(20)]),
            max_consecutive_rejections=3,
        )
    )
    assert [o.tournament_decision for o in outcomes] == ["rejected", "promoted"]

    frontier = load_frontier(workspace, epoch_id)
    assert frontier.members == ()
    assert frontier.champion_generation_id == "v2"
    assert [(r.member.generation_id, r.reason) for r in frontier.retired] == [
        ("v1", "dominated_by_champion")
    ]


def test_the_live_index_sees_the_record_without_a_reindex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    conn = sqlite3.connect(workspace / "index.db")
    rows = list(
        conn.execute(
            "SELECT generation_id, status FROM pareto_frontier WHERE epoch_id = ?", (epoch_id,)
        )
    )
    conn.close()
    assert rows == [("v1", "member")]


def test_a_full_reindex_re_derives_the_table_from_the_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rebuild path, which is the one that has to survive a lost index."""
    from zicato.index.ingest import rebuild_index

    workspace, epoch_id, _outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    db_path = rebuild_index(workspace)
    conn = sqlite3.connect(db_path)
    rows = list(
        conn.execute(
            "SELECT generation_id, status FROM pareto_frontier WHERE epoch_id = ?", (epoch_id,)
        )
    )
    conn.close()
    assert rows == [("v1", "member")]


def test_a_recorder_failure_never_fails_a_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recording an observation must never cost a round its verdict."""
    import zicato.epoch.pareto as _pareto_mod

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("frontier exploded")

    monkeypatch.setattr(_pareto_mod, "record_frontier", _boom)
    _workspace, _epoch_id, outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    assert outcome.tournament_decision == "rejected"


def test_the_record_module_never_reaches_into_the_gate_decision() -> None:
    """A structural pin on "record-only".

    The frontier reads the gate's namespace-monotonicity helper; nothing in
    the decision path — the whole tournament, selection, and proposer trees —
    may read the frontier back. A dependency in that direction is what turns
    the record into a decision, which is registered as separate, gated work
    (PARETO-FRONTIER.md §8).

    Asserted on the parsed IMPORT GRAPH of every module in those trees, not
    on a substring of the source. A substring scan is wrong in both
    directions: it misses a module the enumeration forgot (both trees keep
    growing, and ``selection/strategies/`` is a whole subpackage), and it
    fires on PROSE — ``tournament/gate.py`` now names the record in a
    docstring, and passes a lowercase ``"pareto"`` scan only by the accident
    of a capital P.
    """
    roots = Path(__file__).resolve().parents[1] / "src" / "zicato"
    forbidden = {"zicato.epoch.pareto", "zicato.evolve.pareto"}
    scanned = 0
    for tree in ("tournament", "selection", "proposer"):
        for path in sorted((roots / tree).rglob("*.py")):
            scanned += 1
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    # ``level`` > 0 is a relative import, which cannot reach
                    # another top-level package from inside these trees.
                    names = {node.module or ""} if not node.level else set()
                else:
                    continue
                leaked = names & forbidden
                assert not leaked, f"{path} imports the frontier record: {sorted(leaked)}"
    # A guard against the enumeration silently matching nothing.
    assert scanned > 25, f"only {scanned} modules scanned — the trees moved"


def _drive_swiss_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    drift_by_gen: dict[str, float],
    tokens_by_gen: dict[str, int],
) -> tuple[Path, str, Any]:
    """One real 2-challenger Swiss round on the multi-challenger settle seam."""
    from tests.test_orchestrator_multi_challenger import (
        _bootstrap_swiss_workspace,
        _distinct_field_responses,
    )

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen=drift_by_gen,
        canned_pass_by_gen=dict.fromkeys(drift_by_gen, True),
    )
    _install_costed_run_single(monkeypatch, drift_by_gen=drift_by_gen, tokens_by_gen=tokens_by_gen)

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
        )
    )
    return workspace, epoch_id, outcome


def test_a_rejected_field_records_every_nondominated_slate_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The field settle seam: one slate, two candidates worth remembering.

    The whole slate loses the crowning duel, so the champion stands and the
    scalar keeps nobody. v1 beats the champion on cost AND drift; v2 beats it
    on cost alone — and the two do not dominate each other (v1 is far better
    on drift, v2 is cheaper). Both are recorded, which is exactly the shape a
    single scalar cannot express.
    """
    workspace, epoch_id, outcome = _drive_swiss_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 2.0, "v1": 0.5, "v2": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 900, "v2": 200},
    )
    assert outcome.tournament_decision == "rejected"

    frontier = load_frontier(workspace, epoch_id)
    assert frontier.champion_generation_id == "v0"
    assert [(m.generation_id, m.beats_champion_on) for m in frontier.members] == [
        ("v1", ("cost:", "drift:")),
        ("v2", ("cost:",)),
    ]
    # The two members are genuinely incomparable — neither displaced the other.
    assert frontier.retired == ()


def test_a_crowned_field_records_against_the_NEW_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both challengers beat the champion, so one is crowned.

    Whichever the structure crowns, the record is evaluated against THAT
    generation — the champion the round actually ended with — and the other
    challenger, which beats it on one axis, is what the record keeps. The
    deposed champion beats the new one nowhere, so it is not recorded.
    """
    workspace, epoch_id, outcome = _drive_swiss_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.0},
        tokens_by_gen={"v0": 1000, "v1": 900, "v2": 100},
    )
    assert outcome.tournament_decision == "promoted"
    crowned = outcome.proposed_generation_id
    assert crowned in ("v1", "v2")
    other = "v2" if crowned == "v1" else "v1"

    frontier = load_frontier(workspace, epoch_id)
    assert frontier.champion_generation_id == crowned
    assert [m.generation_id for m in frontier.members] == [other]
    assert frontier.members[0].champion_generation_id == crowned


def test_the_recorder_threads_the_placebo_arms_it_is_given(tmp_path: Path) -> None:
    """The field path fields the placebo INSIDE the slate, so it must be named.

    Without the exclusion a no-op re-emission of the champion would sit on
    the record forever as a permanent tie.
    """
    from zicato.evolve.pareto import record_round_frontier

    aggregates = {
        "v0": _agg(drift=1.0, cost=1.0),
        "v9": _agg(drift=3.0, cost=0.5),
    }
    record_round_frontier(
        workspace_root=tmp_path,
        epoch_id="e0",
        round_index=1,
        weights=_weights(),
        champion_generation_id="v0",
        aggregates=aggregates,
        placebo_generation_ids=["v9"],
    )
    assert load_frontier(tmp_path, "e0").members == ()

    # The SAME candidate, unmarked, is admitted — so the refusal is the
    # placebo marker doing the work, not the numbers.
    record_round_frontier(
        workspace_root=tmp_path,
        epoch_id="e0",
        round_index=1,
        weights=_weights(),
        champion_generation_id="v0",
        aggregates=aggregates,
    )
    assert [m.generation_id for m in load_frontier(tmp_path, "e0").members] == ["v9"]
