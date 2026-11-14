"""Tests for the report tables that state per-challenger and per-entry detail.

``analysis.md`` carries these as pipe tables: the per-board-entry Δ scalar,
the proposer's predictions beside the outcomes, each challenger's drift-kind
movements, the mutation-impact matrix, and the lineage. Each table is drawn
from an :class:`~zicato.analyzer.report_data.EpochReportData` view, so the
tests build that view directly.
"""

from __future__ import annotations

import pytest

from zicato.analyzer.report_data import BoardEntryView, EpochReportData, GenerationView
from zicato.analyzer.report_sections import (
    _hit_or_miss,
    _parse_expected_pass_rate_delta,
    _predicted_drift_delta_sum,
    render_challenger_drift_table,
    render_hypothesis_outcome_table,
    render_lineage_table,
    render_mutation_impact_table,
    render_per_board_delta_table,
)


def _gen(
    *,
    gid: str,
    parent: str = "",
    is_baseline: bool = False,
    decision: str = "promoted",
    drift_loss_delta: float = 0.0,
    pass_rate_delta: float = 0.0,
    metric_movements: tuple[dict[str, object], ...] = (),
    gen_score: dict[str, object] | None = None,
    expected_pass_rate_delta: str = "",
    expected_metric_movements: tuple[dict[str, str], ...] = (),
    patches: tuple[dict[str, str], ...] = (),
) -> GenerationView:
    return GenerationView(
        generation_id=gid,
        parent_generation_id=parent,
        is_baseline=is_baseline,
        proposed_at="2026-05-19T01:00:00Z",
        core_idea="",
        why="",
        risks="",
        modulating=(),
        expected_pass_rate_delta=expected_pass_rate_delta,
        expected_metric_movements=expected_metric_movements,
        decision=decision,
        rejection_reason="",
        scalar_score_delta=0.0,
        drift_loss_delta=drift_loss_delta,
        pass_rate_delta=pass_rate_delta,
        metric_movements=metric_movements,
        patches=patches,
        gen_score=gen_score or {},
    )


def _entry(eid: str) -> BoardEntryView:
    return BoardEntryView(
        id=eid,
        kind="single_turn",
        weight=1.0,
        tags=(),
        expectation_kind="predicate",
        expectation_spec="",
        judges=(),
        wall_clock_budget_seconds=60,
    )


def _data(
    generations: tuple[GenerationView, ...] = (),
    board_entries: tuple[BoardEntryView, ...] = (),
) -> EpochReportData:
    return EpochReportData(
        epoch_id="e1",
        epoch_name="e1",
        contract_hash="",
        created_at="",
        closed=False,
        closed_at="",
        brief_text="",
        journal_text="",
        board_entries=board_entries,
        disable_drift=(),
        scoring={},
        mutation_surface=(),
        generations=generations,
        span_start="",
        span_end="",
    )


def _rows(table: str) -> dict[str, list[str]]:
    """Body rows of a pipe table, keyed by their first cell without backticks."""
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table.splitlines()[2:]]
    return {row[0].strip("`"): row[1:] for row in rows}


def _header(table: str) -> list[str]:
    return [c.strip() for c in table.splitlines()[0].strip("|").split("|")]


_BASELINE = _gen(gid="v0", is_baseline=True, decision="baseline")


# ---------------------------------------------------------------------------
# Per-board-entry Δ scalar
# ---------------------------------------------------------------------------


def test_per_board_table_without_entries_or_challengers_says_so() -> None:
    assert render_per_board_delta_table(_data()) == "_No per-board outcomes recorded yet._"


def test_per_board_table_has_a_row_per_entry_and_a_column_per_challenger() -> None:
    gens = (
        _BASELINE,
        _gen(
            gid="v1",
            parent="v0",
            gen_score={
                "entries": {"slides": {"scalar_delta": -0.08}, "qa": {"scalar_delta": 0.02}}
            },
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            gen_score={"per_entry": [{"entry_id": "slides", "delta": 0.04}]},
        ),
    )
    table = render_per_board_delta_table(_data(gens, (_entry("slides"), _entry("qa"))))
    # The baseline never gets a column.
    assert _header(table) == ["board entry", "v1", "v2"]
    assert _rows(table) == {"slides": ["-0.080", "+0.040"], "qa": ["+0.020", "—"]}


def test_per_board_table_marks_cached_columns() -> None:
    gens = (
        _BASELINE,
        _gen(
            gid="v1",
            parent="v0",
            gen_score={"entries": {"slides": {"delta": -0.05}}, "champion_cached": True},
        ),
    )
    table = render_per_board_delta_table(_data(gens, (_entry("slides"),)))
    assert _header(table) == ["board entry", "v1 (cached)"]


def test_per_board_table_reports_missing_data_as_a_dash_not_a_zero() -> None:
    gens = (_BASELINE, _gen(gid="v1", parent="v0", gen_score={"scalar": 0.5}))
    table = render_per_board_delta_table(_data(gens, (_entry("slides"),)))
    assert _rows(table) == {"slides": ["—"]}


def test_per_board_table_diffs_absolute_drift_loss_against_the_parent() -> None:
    """``per_entry`` keyed by entry id carries absolute ``drift_loss`` values.

    The cell is the challenger's value minus its parent champion's. Regression
    for Task #196: a resolver that understood only a list of rows or a cached
    delta reported every cell of the current on-disk shape as having no data.
    """

    def score(slides: float, qa: float) -> dict[str, object]:
        return {
            "per_entry": {
                "slides": {"drift_loss": slides, "pass_fail": True},
                "qa": {"drift_loss": qa, "pass_fail": False},
            }
        }

    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline", gen_score=score(12.0, 22.0)),
        _gen(gid="v1", parent="v0", gen_score=score(10.0, 20.0)),
        _gen(gid="v2", parent="v1", decision="rejected", gen_score=score(4.0, 25.0)),
    )
    table = render_per_board_delta_table(_data(gens, (_entry("slides"), _entry("qa"))))
    assert _rows(table) == {"slides": ["-2.000", "-6.000"], "qa": ["-2.000", "+5.000"]}


def test_per_board_table_diffs_against_the_parent_not_the_seed() -> None:
    def score(loss: float) -> dict[str, object]:
        return {"per_entry": {"a": {"drift_loss": loss}}}

    gens = (
        _BASELINE,
        _gen(gid="v1", parent="v0", gen_score=score(50.0)),
        _gen(gid="v2", parent="v1", gen_score=score(30.0)),
        _gen(gid="v3", parent="v2", decision="rejected", gen_score=score(35.0)),
    )
    table = render_per_board_delta_table(_data(gens, (_entry("a"),)))
    # v1's parent is the baseline, which carries no per-entry data.
    assert _rows(table) == {"a": ["—", "-20.000", "+5.000"]}


# ---------------------------------------------------------------------------
# Hypothesis vs outcome
# ---------------------------------------------------------------------------


def test_predicted_drift_delta_sum_accepts_medium_magnitude() -> None:
    g = _gen(
        gid="vX",
        parent="vP",
        expected_metric_movements=(
            {"direction": "decrease", "metric_name": "drift:off_topic", "magnitude": "medium"},
        ),
    )
    assert _predicted_drift_delta_sum(g) == pytest.approx(-0.10)
    assert _predicted_drift_delta_sum(_gen(gid="vY", parent="vP")) is None


def test_parse_expected_pass_rate_delta_handles_range_and_single_value() -> None:
    assert _parse_expected_pass_rate_delta("+0.05 to +0.15") == pytest.approx(0.10)
    assert _parse_expected_pass_rate_delta("+0.10") == pytest.approx(0.10)
    assert _parse_expected_pass_rate_delta("-0.20 to -0.10") == pytest.approx(-0.15)
    assert _parse_expected_pass_rate_delta("") is None
    assert _parse_expected_pass_rate_delta("unknown") is None


def test_hit_or_miss_reflects_directional_agreement_not_magnitude() -> None:
    assert _hit_or_miss(0.05, 0.50) == "hit"
    assert _hit_or_miss(-0.05, -0.50) == "hit"
    assert _hit_or_miss(0.05, -0.50) == "miss"
    assert _hit_or_miss(0.0, 0.0) == "hit"
    assert _hit_or_miss(None, 0.10) == ""
    assert _hit_or_miss(0.10, None) == ""


def test_hypothesis_table_pairs_prediction_with_outcome_for_every_decided_challenger() -> None:
    gens = (
        _BASELINE,
        _gen(
            gid="v1",
            parent="v0",
            pass_rate_delta=0.2,
            drift_loss_delta=-3.0,
            expected_pass_rate_delta="+0.05 to +0.15",
            expected_metric_movements=(
                {"direction": "decrease", "metric_name": "drift:off_topic", "magnitude": "large"},
            ),
        ),
        # A rejected round's prediction was still made, so the pair is reported.
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            pass_rate_delta=-0.1,
            drift_loss_delta=1.5,
            expected_pass_rate_delta="+0.10",
        ),
        _gen(gid="v3", parent="v1", decision="pending"),
    )
    table = render_hypothesis_outcome_table(_data(gens))
    assert _rows(table) == {
        "v1": ["promoted", "+0.100", "+0.200", "hit", "-0.200", "-3.000", "hit"],
        "v2": ["rejected", "+0.100", "-0.100", "miss", "—", "+1.500", "—"],
    }


def test_hypothesis_table_without_a_decided_challenger_says_so() -> None:
    table = render_hypothesis_outcome_table(_data((_BASELINE,)))
    assert table == "_No scored challengers to compare against predictions._"


# ---------------------------------------------------------------------------
# Drift-kind movements per challenger
# ---------------------------------------------------------------------------


def test_challenger_drift_table_includes_rejected_challengers() -> None:
    gens = (
        _BASELINE,
        _gen(
            gid="v1",
            parent="v0",
            metric_movements=(
                {"metric_name": "drift:off_topic", "from_value": 0.4, "to_value": 0.1},
                {"metric_name": "cost:tokens", "from_value": 10, "to_value": 12},
            ),
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            metric_movements=(
                {"metric_name": "drift:looping", "from_value": 0.2, "to_value": 0.5},
            ),
        ),
    )
    lines = render_challenger_drift_table(_data(gens)).splitlines()[2:]
    assert lines == [
        "| `v1` | promoted | `off_topic` | 0.400 | 0.100 | -0.300 |",
        "| `v2` | rejected | `looping` | 0.200 | 0.500 | +0.300 |",
    ]


def test_challenger_drift_table_without_movements_says_so() -> None:
    gens = (_BASELINE, _gen(gid="v1", parent="v0"))
    assert render_challenger_drift_table(_data(gens)) == "_No drift movements recorded yet._"


# ---------------------------------------------------------------------------
# Mutation-impact matrix and lineage
# ---------------------------------------------------------------------------


def test_mutation_impact_table_without_challengers_says_so() -> None:
    assert "matrix is empty" in render_mutation_impact_table(_data())


def test_mutation_impact_table_marks_each_touched_site_per_challenger() -> None:
    gens = (
        _BASELINE,
        _gen(
            gid="v1",
            parent="v0",
            patches=({"mutation_id": "sys_prompt", "op": "replace", "rationale": ""},),
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            patches=(
                {"mutation_id": "temp", "op": "set_numeric", "rationale": ""},
                {"mutation_id": "sys_prompt", "op": "replace", "rationale": ""},
            ),
        ),
    )
    table = render_mutation_impact_table(_data(gens))
    # The baseline carries no patch and gets no column; each column is headed
    # by its round's outcome. A site no challenger touched has no row.
    assert _header(table) == ["mutation id", "v1 (promoted)", "v2 (rejected)"]
    assert _rows(table) == {"sys_prompt": ["patched", "patched"], "temp": ["", "patched"]}


def test_mutation_impact_table_without_patches_says_so() -> None:
    gens = (_BASELINE, _gen(gid="v1", parent="v0", decision="rejected"))
    assert "No patches" in render_mutation_impact_table(_data(gens))


def test_lineage_table_names_each_generation_parent_and_outcome() -> None:
    gens = (
        _BASELINE,
        _gen(gid="v1", parent="v0"),
        _gen(gid="v2", parent="v1", decision="rejected"),
        _gen(gid="v3", parent="v1", decision=""),
    )
    assert _rows(render_lineage_table(_data(gens))) == {
        "v0": ["—", "baseline"],
        "v1": ["v0", "promoted"],
        "v2": ["v1", "rejected"],
        "v3": ["v1", "pending"],
    }
