"""Tests for the analyzer report's inline-SVG figure generators.

The figures module — :mod:`zicato.analyzer.report_figures` — turns the
structured epoch view into self-contained inline SVG fragments that the
markdown→HTML renderer drops into the paper. Coverage:

* Every figure is well-formed inline SVG with NO external resources.
* Each figure carries the expected structural elements for the data it
  was given (markers per generation, cells per (entry × generation),
  bars per drift kind, etc.).
* Empty inputs degrade to a placeholder SVG rather than raising.
* The dispatch helper :func:`render_figure` covers every canonical
  marker name and returns ``""`` for unknown names.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from zicato.analyzer.report_data import (
    BoardEntryView,
    EpochReportData,
    GenerationView,
)
from zicato.analyzer.report_figures import (
    PROMOTED_COLOR,
    REJECTED_COLOR,
    iter_figure_names,
    render_figure,
    render_svg_drift_movements,
    render_svg_lineage_compact,
    render_svg_mutation_surface,
    render_svg_per_board_heatmap,
    render_svg_score_trajectory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StackParser(HTMLParser):
    """HTMLParser that tracks unclosed tags so we can assert well-formedness."""

    _VOID = {"path", "rect", "circle", "line"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            return
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing — count but don't push.
        return

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.unbalanced.append(f"close without open: {tag}")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        self.unbalanced.append(f"mismatched: expected </{self.stack[-1]}> got </{tag}>")


def _assert_inline_svg(svg: str) -> None:
    assert svg.startswith("<svg "), f"expected an SVG fragment, got: {svg[:80]}"
    assert "</svg>" in svg
    # No external resources of any kind.
    assert 'href="http' not in svg
    assert 'src="http' not in svg
    assert "fonts.googleapis" not in svg
    parser = _StackParser()
    parser.feed(svg)
    parser.close()
    assert not parser.unbalanced, f"SVG not well-formed: {parser.unbalanced[:3]}"
    assert not parser.stack, f"SVG has unclosed tags: {parser.stack}"


def _gen(
    *,
    gid: str,
    parent: str = "",
    is_baseline: bool = False,
    decision: str = "promoted",
    scalar_score_delta: float = 0.0,
    drift_loss_delta: float = 0.0,
    pass_rate_delta: float = 0.0,
    drift_movements: tuple[dict[str, object], ...] = (),
    gen_score: dict[str, object] | None = None,
    cumulative_scalar: float = 0.0,
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
        expected_pass_rate_delta="",
        expected_drift_movements=(),
        decision=decision,
        rejection_reason="",
        scalar_score_delta=scalar_score_delta,
        drift_loss_delta=drift_loss_delta,
        pass_rate_delta=pass_rate_delta,
        drift_movements=drift_movements,
        metric_movements=(),
        patches=(),
        gen_score=gen_score or {},
        cumulative_scalar=cumulative_scalar,
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
    mutation_surface: tuple[dict[str, object], ...] = (),
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
        mutation_surface=mutation_surface,
        generations=generations,
        span_start="",
        span_end="",
    )


# ---------------------------------------------------------------------------
# Score trajectory
# ---------------------------------------------------------------------------


def test_score_trajectory_empty_returns_placeholder() -> None:
    svg = render_svg_score_trajectory(_data())
    _assert_inline_svg(svg)
    assert "No generations" in svg


def test_score_trajectory_marks_promoted_and_rejected_points() -> None:
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline", cumulative_scalar=0.0),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            scalar_score_delta=-0.2,
            cumulative_scalar=-0.2,
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            scalar_score_delta=0.1,
            cumulative_scalar=-0.1,
        ),
        _gen(
            gid="v3",
            parent="v1",
            decision="promoted",
            scalar_score_delta=-0.05,
            cumulative_scalar=-0.25,
        ),
    )
    svg = render_svg_score_trajectory(_data(gens))
    _assert_inline_svg(svg)
    # Promoted points use the promoted colour token, rejected use the
    # rejected token. Figures bind colours via CSS variables so a dark
    # host can re-tint without re-rendering the SVG.
    assert "var(--paper-promoted)" in svg
    assert "var(--paper-rejected)" in svg
    # The y-axis title labels the unit.
    assert "scalar (loss" in svg
    # Every generation id is plotted as an x-axis label.
    for gid in ("v0", "v1", "v2", "v3"):
        assert f">{gid}<" in svg
    # Promoted spine connects baseline + promoted generations — at least
    # one <path> styled with the promoted token is present.
    assert "stroke: var(--paper-promoted)" in svg
    # Per-point value labels (cumulative scalars) appear.
    assert "-0.250" in svg  # v3 cumulative
    # No raw hex hue should appear in figure markup — palette flows
    # exclusively via CSS vars.
    assert PROMOTED_COLOR not in svg
    assert REJECTED_COLOR not in svg


def test_score_trajectory_value_labels_use_fixed_3dp() -> None:
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline", cumulative_scalar=0.0),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            scalar_score_delta=-0.123,
            cumulative_scalar=-0.123,
        ),
    )
    svg = render_svg_score_trajectory(_data(gens))
    assert "-0.123" in svg
    assert "+0.000" in svg


# ---------------------------------------------------------------------------
# Drift movements
# ---------------------------------------------------------------------------


def test_drift_movements_empty_returns_placeholder() -> None:
    svg = render_svg_drift_movements(_data())
    _assert_inline_svg(svg)
    assert "No drift" in svg


def test_drift_movements_one_panel_per_challenger_with_drift() -> None:
    g1_drift = (
        {"kind": "off_topic", "from_rate": 0.40, "to_rate": 0.10},
        {"kind": "tool_error", "from_rate": 0.50, "to_rate": 0.55},
    )
    g2_drift = ({"kind": "off_topic", "from_rate": 0.10, "to_rate": 0.08},)
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(gid="v1", parent="v0", decision="promoted", drift_movements=g1_drift),
        _gen(gid="v2", parent="v1", decision="promoted", drift_movements=g2_drift),
        # A rejected generation with NO drift movements: no panel.
        _gen(gid="v3", parent="v2", decision="rejected", drift_movements=()),
    )
    svg = render_svg_drift_movements(_data(gens))
    _assert_inline_svg(svg)
    # One panel per generation that recorded drift = v1 + v2.
    assert ">v1 ·" in svg
    assert ">v2 ·" in svg
    assert ">v3 ·" not in svg
    # Drift kinds appear as labels.
    assert "off_topic" in svg
    assert "tool_error" in svg
    # Signed Δ values render — v1's off_topic went 0.40 -> 0.10, Δ=-0.30.
    assert "-0.300" in svg


# ---------------------------------------------------------------------------
# Per-board heatmap
# ---------------------------------------------------------------------------


def test_per_board_heatmap_empty_returns_placeholder() -> None:
    svg = render_svg_per_board_heatmap(_data())
    _assert_inline_svg(svg)


def test_per_board_heatmap_renders_cells_per_entry_x_generation() -> None:
    entries = (_entry("slides"), _entry("qa"))
    g_score_v1 = {
        "entries": {
            "slides": {"scalar_delta": -0.08},
            "qa": {"scalar_delta": 0.02},
        }
    }
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            scalar_score_delta=-0.06,
            gen_score=g_score_v1,
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            scalar_score_delta=0.04,
            gen_score={"per_entry": [{"entry_id": "slides", "delta": 0.04}]},
        ),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    # Both board entries are row labels; both challenger generations are
    # column headers. The baseline never gets a column.
    assert "slides" in svg
    assert "qa" in svg
    assert ">v1</text>" in svg
    assert ">v2</text>" in svg
    assert ">v0</text>" not in svg
    # The per-entry delta of v1.slides = -0.08 (better) renders as a cell
    # value label, and is coloured green-ish.
    assert "-0.080" in svg


def test_per_board_heatmap_marks_cached_columns() -> None:
    entries = (_entry("slides"),)
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            gen_score={"entries": {"slides": {"delta": -0.05}}, "champion_cached": True},
        ),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    assert "cached" in svg


def test_per_board_heatmap_renders_hatch_for_missing_data() -> None:
    entries = (_entry("slides"),)
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(gid="v1", parent="v0", decision="promoted", gen_score={"scalar": 0.5}),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    # No per-entry data — hatched stripes pattern present.
    assert "nodata-stripes" in svg


# ---------------------------------------------------------------------------
# Lineage compact
# ---------------------------------------------------------------------------


def test_lineage_compact_empty_returns_placeholder() -> None:
    svg = render_svg_lineage_compact(_data())
    _assert_inline_svg(svg)


def test_lineage_compact_renders_node_per_generation() -> None:
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(gid="v1", parent="v0", decision="promoted"),
        _gen(gid="v2", parent="v1", decision="rejected"),
    )
    svg = render_svg_lineage_compact(_data(gens))
    _assert_inline_svg(svg)
    # One rect per generation = 3 rects total.
    assert svg.count("<rect") == 3
    # Promoted and rejected palette tokens appear — figures bind colour
    # via CSS vars so a dark host can re-tint.
    assert "var(--paper-promoted)" in svg
    assert "var(--paper-rejected)" in svg
    # Every generation id is rendered.
    for gid in ("v0", "v1", "v2"):
        assert f">{gid}</text>" in svg
    # No raw hex hue should appear in lineage figure markup.
    assert PROMOTED_COLOR not in svg
    assert REJECTED_COLOR not in svg


# ---------------------------------------------------------------------------
# Mutation surface
# ---------------------------------------------------------------------------


def test_mutation_surface_empty_returns_placeholder() -> None:
    svg = render_svg_mutation_surface(_data())
    _assert_inline_svg(svg)


def test_mutation_surface_renders_one_row_per_mutation() -> None:
    mutations = (
        {"id": "sys_prompt", "kind": "prompt_text", "file": "agent/prompt.txt"},
        {"id": "temp", "kind": "numeric", "file": "agent/config.py", "line_start": 12},
        {
            "id": "tool",
            "kind": "tool_set",
            "file": "agent/tools.py",
            "line_start": 5,
            "line_end": 30,
        },
    )
    svg = render_svg_mutation_surface(_data(mutation_surface=mutations))
    _assert_inline_svg(svg)
    # Each id appears as monospace text.
    for mid in ("sys_prompt", "temp", "tool"):
        assert f">{mid}<" in svg
    # Line-range annotation appears for the third row.
    assert "agent/tools.py:5-30" in svg
    # Single-line annotation for the second.
    assert "agent/config.py:12" in svg


def test_mutation_surface_caps_at_max_rows_with_overflow_note() -> None:
    mutations = tuple({"id": f"m{i}", "kind": "k", "file": "f.py"} for i in range(20))
    svg = render_svg_mutation_surface(_data(mutation_surface=mutations), max_rows=5)
    _assert_inline_svg(svg)
    assert "+15 more mutation points" in svg


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(iter_figure_names()))
def test_render_figure_dispatch_covers_every_marker(name: str) -> None:
    svg = render_figure(name, _data())
    _assert_inline_svg(svg)


def test_render_figure_returns_empty_for_unknown_name() -> None:
    assert render_figure("not-a-real-figure", _data()) == ""


# ---------------------------------------------------------------------------
# Golden-output assertion against the real fixture workspace
# ---------------------------------------------------------------------------


def test_score_trajectory_golden_with_fixture_workspace(tmp_path: Path) -> None:
    """The fixture workspace's score trajectory contains the expected markers.

    A small structural golden — not byte-exact, but the renderer's
    contract for the fixture data: y-axis title, both challenger
    decisions present, all three generations on the x axis.
    """
    # Match the standing fixture in test_analyzer_report.py.
    from tests.test_analyzer_report import (
        epoch_workspace,  # type: ignore[attr-defined]  # noqa: F401
    )

    # Build the fixture in-line so the fixture import doesn't conflict.
    from zicato.analyzer.report_data import gather_epoch_report_data

    ws = tmp_path / ".zicato"
    epoch = "ep"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    import json

    (edir / "config.json").write_text(json.dumps({"name": "ep"}))
    (edir / "board.jsonl").write_text("")
    (edir / "scoring.json").write_text(json.dumps({}))

    # baseline + 1 promoted + 1 rejected
    for gen, parent, dec, delta in (
        ("v0", "", "baseline", 0.0),
        ("v1", "v0", "promoted", -0.2),
        ("v2", "v1", "rejected", 0.1),
    ):
        gd = edir / "generations" / gen
        gd.mkdir(parents=True)
        payload: dict[str, object] = {
            "generation_id": gen,
            "parent_generation_id": parent,
            "hypothesis": {"core_idea": "x"},
        }
        if parent:
            payload["outcome"] = {
                "tournament_decision": dec,
                "scalar_score_delta": delta,
                "drift_loss_delta": 0.0,
                "pass_rate_delta": 0.0,
                "drift_movements": [],
            }
        (gd / "experiment.json").write_text(json.dumps(payload))

    data = gather_epoch_report_data(ws, epoch)
    svg = render_svg_score_trajectory(data)
    _assert_inline_svg(svg)
    # Three generations -> three x-axis labels.
    assert ">v0<" in svg and ">v1<" in svg and ">v2<" in svg
    # Cumulative scalar -0.2 at v1, -0.1 at v2 (rejected, off-spine).
    assert "-0.200" in svg
    assert "-0.100" in svg
