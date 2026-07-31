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

import re
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
    render_svg_hypothesis_vs_outcome,
    render_svg_lineage_compact,
    render_svg_mutation_impact_matrix,
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
    expected_pass_rate_delta: str = "",
    expected_drift_movements: tuple[dict[str, str], ...] = (),
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
        expected_drift_movements=expected_drift_movements,
        decision=decision,
        rejection_reason="",
        scalar_score_delta=scalar_score_delta,
        drift_loss_delta=drift_loss_delta,
        pass_rate_delta=pass_rate_delta,
        drift_movements=drift_movements,
        metric_movements=(),
        patches=patches,
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


def test_per_board_heatmap_reads_per_entry_dict_shape_with_drift_loss() -> None:
    """On-disk ``per_entry`` is a dict keyed by entry id with absolute
    ``drift_loss`` values. The heatmap subtracts the parent champion's
    drift_loss to form a per-cell delta (challenger − champion); a
    negative value (loss dropped) paints the cell as 'better' (green),
    a positive value as 'worse' (red).

    Regression for Task #196: the older resolver only understood a
    list-of-dicts shape or pre-computed ``scalar_delta`` keys, so
    workspaces with the current dict-of-dicts shape painted every cell
    as hatched 'no data'.
    """
    entries = (_entry("slides"), _entry("qa"))
    # v0 baseline carries its own per-entry numbers so the v1 column can
    # be diffed against the seed champion too.
    # v1 (parent v0): slides 10-12=-2, qa 20-22=-2.
    # v2 (parent v1): slides 4-10=-6 (better), qa 25-20=+5 (worse).
    gens = (
        _gen(
            gid="v0",
            is_baseline=True,
            decision="baseline",
            gen_score={
                "per_entry": {
                    "slides": {"drift_loss": 12.0, "pass_fail": True},
                    "qa": {"drift_loss": 22.0, "pass_fail": False},
                }
            },
        ),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            gen_score={
                "per_entry": {
                    "slides": {"drift_loss": 10.0, "pass_fail": True},
                    "qa": {"drift_loss": 20.0, "pass_fail": False},
                }
            },
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            gen_score={
                "per_entry": {
                    "slides": {"drift_loss": 4.0, "pass_fail": True},
                    "qa": {"drift_loss": 25.0, "pass_fail": False},
                }
            },
        ),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    # Every CELL carries a real delta — no hatched placeholders (the
    # ``nodata-stripes`` pattern is defined once in <defs>; only cells
    # that fall back to hatched reference it via ``fill="url(#nodata-stripes)"``).
    assert 'fill="url(#nodata-stripes)"' not in svg
    # The v2.qa cell rendered the +5.000 challenger-minus-champion delta
    # (drift_loss went 20 -> 25, a regression).
    assert "+5.000" in svg
    # The v2.slides cell rendered -6.000 (drift_loss 10 -> 4 = improved).
    assert "-6.000" in svg
    # The v1 column is diffed against the baseline seed: slides 10-12=-2.
    assert "-2.000" in svg


def test_per_board_heatmap_dict_shape_promoted_lineage_baseline_diff() -> None:
    """A promoted challenger's per-entry delta is computed against the
    immediately-preceding champion (its parent), NOT against the seed.
    """
    entries = (_entry("a"),)
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            gen_score={"per_entry": {"a": {"drift_loss": 50.0}}},
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="promoted",
            gen_score={"per_entry": {"a": {"drift_loss": 30.0}}},
        ),
        _gen(
            gid="v3",
            parent="v2",
            decision="rejected",
            gen_score={"per_entry": {"a": {"drift_loss": 35.0}}},
        ),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    # v2 vs v1 = 30 - 50 = -20.000 (better).
    assert "-20.000" in svg
    # v3 vs v2 = 35 - 30 = +5.000 (worse).
    assert "+5.000" in svg
    # v1 has no parent champion with per-entry data (baseline has none),
    # so its cell hatches.
    assert 'fill="url(#nodata-stripes)"' in svg


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


def _lineage_boxes(svg: str) -> list[tuple[float, float, float, float]]:
    """Every node rect in the lineage figure as ``(x, y, w, h)``."""
    boxes = []
    for m in re.finditer(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="(\d+)" height="(\d+)"', svg):
        boxes.append(tuple(float(v) for v in m.groups()))  # type: ignore[arg-type]
    return boxes


def test_lineage_compact_wraps_instead_of_piling_up_at_high_n() -> None:
    """Issue #129: the index-positional layout overlapped past ~9 nodes.

    The step is ``(usable_width - node_width) / (n - 1)``, so it shrinks
    with every generation while the boxes stay 84px wide. Around nine
    nodes the step drops below the node width and the figure becomes a
    pile — exactly the regime a champion-retained epoch produces, since
    every rejected sibling adds a node without advancing the spine.
    """
    gens = (_gen(gid="v0", is_baseline=True, decision="baseline"),) + tuple(
        # one promotion early, then twenty straight rejections: the shape a
        # long champion-retained run actually has.
        _gen(gid=f"v{i}", parent="v0", decision="promoted" if i == 1 else "rejected")
        for i in range(1, 21)
    )
    svg = render_svg_lineage_compact(_data(gens))
    _assert_inline_svg(svg)

    boxes = _lineage_boxes(svg)
    assert len(boxes) == 21, "one rect per generation survives the wrap"
    for i, (ax, ay, aw, ah) in enumerate(boxes):
        for bx, by, bw, bh in boxes[i + 1 :]:
            overlaps = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
            assert not overlaps, f"nodes overlap at n=21: {(ax, ay)} vs {(bx, by)}"

    # The canvas grew to hold the extra rows rather than cropping them.
    height = float(re.search(r'viewBox="0 0 \d+ ([\d.]+)"', svg).group(1))  # type: ignore[union-attr]
    assert height > 160, "the wrapped layout widens the canvas height"
    assert max(y + h for _, y, _, h in boxes) <= height, "every node fits inside the viewBox"


def test_lineage_compact_layout_is_deterministic() -> None:
    gens = (_gen(gid="v0", is_baseline=True, decision="baseline"),) + tuple(
        _gen(gid=f"v{i}", parent="v0", decision="rejected") for i in range(1, 21)
    )
    first = render_svg_lineage_compact(_data(gens))
    assert first == render_svg_lineage_compact(_data(gens))


def test_lineage_compact_keeps_the_single_row_layout_when_it_fits() -> None:
    """Small n is untouched — the wrap engages only where boxes would collide."""
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(gid="v1", parent="v0", decision="promoted"),
        _gen(gid="v2", parent="v1", decision="rejected"),
    )
    svg = render_svg_lineage_compact(_data(gens))
    boxes = _lineage_boxes(svg)
    assert len({y for _, y, _, _ in boxes}) == 2, "promoted on the centerline, rejected below"
    assert 'viewBox="0 0 720 160"' in svg, "the default canvas is unchanged"


def test_lineage_compact_canvas_only_ever_grows() -> None:
    """The caller's ``height`` is a floor the wrap may exceed, never undercut.

    The first wrap is two rows, which needs 142px against the default
    160 — so returning the computed height unconditionally made the
    figure jump SHORTER at exactly the generation count where it gains a
    row. Every count from a single row up must be monotone in the
    canvas the report reserves for it.
    """
    heights = []
    for n in range(2, 26):
        gens = (_gen(gid="v0", is_baseline=True, decision="baseline"),) + tuple(
            _gen(gid=f"v{i}", parent="v0", decision="promoted" if i == 1 else "rejected")
            for i in range(1, n)
        )
        svg = render_svg_lineage_compact(_data(gens))
        h = float(re.search(r'viewBox="0 0 \d+ ([\d.]+)"', svg).group(1))  # type: ignore[union-attr]
        assert h >= 160, f"n={n} rendered a canvas shorter than the requested height: {h}"
        heights.append(h)
    assert heights == sorted(heights), f"canvas height is not monotone in n: {heights}"


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


# ---------------------------------------------------------------------------
# Hypothesis vs outcome
# ---------------------------------------------------------------------------


def test_hypothesis_vs_outcome_empty_returns_placeholder() -> None:
    svg = render_svg_hypothesis_vs_outcome(_data())
    _assert_inline_svg(svg)
    assert "No scored generations" in svg


def test_hypothesis_vs_outcome_renders_row_per_scored_generation() -> None:
    """One row per non-pending generation — promoted AND rejected.

    Rejected rounds still made a prediction; the figure renders the
    pair so the proposer's hit-rate covers the full campaign, not
    only the promoted spine.
    """
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            scalar_score_delta=-0.2,
            pass_rate_delta=0.10,
            drift_loss_delta=-0.30,
            expected_pass_rate_delta="+0.05 to +0.15",
            expected_drift_movements=(
                {"kind": "off_topic", "direction": "decrease", "magnitude": "moderate"},
            ),
            cumulative_scalar=-0.2,
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            scalar_score_delta=0.14,
            pass_rate_delta=-0.05,
            drift_loss_delta=0.12,
            expected_pass_rate_delta="+0.0",
            cumulative_scalar=-0.06,
        ),
        # A pending generation should be skipped.
        _gen(gid="v3", parent="v2", decision="pending"),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    # Every scored generation gets a row, including the baseline so
    # the lineage stays anchored visually. The pending v3 is skipped.
    assert ">v0</text>" in svg
    assert ">v1</text>" in svg
    assert ">v2</text>" in svg
    assert ">v3</text>" not in svg
    # Both lane headers are labelled with per-lane extents.
    assert ">pass rate Δ " in svg
    assert ">drift loss Δ " in svg
    # Predicted marker uses the predicted palette token; actual bar
    # uses the per-row decision token.
    assert "var(--paper-predicted" in svg
    assert "var(--paper-promoted)" in svg
    assert "var(--paper-rejected)" in svg
    # No raw hex / no external resources.
    assert PROMOTED_COLOR not in svg
    assert REJECTED_COLOR not in svg
    assert 'href="http' not in svg
    # Pred/act numeric labels appear for each scored generation.
    assert "act +0.100" in svg  # v1 actual pass rate
    assert "act -0.050" in svg  # v2 actual pass rate (rejected, still rendered)
    assert "pred +0.100" in svg  # v1 predicted pass-rate midpoint of +0.05..+0.15
    # Decision pills present on the rendered rows.
    assert ">promoted</text>" in svg
    assert ">rejected</text>" in svg
    assert ">baseline</text>" in svg
    # The title strip carries the figure header.
    assert "PREDICTED vs ACTUAL" in svg


def test_hypothesis_vs_outcome_lanes_have_independent_extents() -> None:
    """Each metric lane sets its own symmetric extent.

    Drift-loss Δ lives on a much larger scale than pass-rate Δ; a
    shared axis squashes pass-rate predictions to invisible slivers.
    The lane headers expose the per-lane extent so a reader can
    see both metrics at readable magnitudes.
    """
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            pass_rate_delta=0.33,  # small absolute number
            drift_loss_delta=-24.0,  # large absolute number
            expected_pass_rate_delta="+0.10 to +0.25",
            expected_drift_movements=(
                {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"},
            ),
        ),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    # Pass-rate lane extent reflects the small ±0.33 scale.
    assert "(lane ±0.33)" in svg
    # Drift-loss lane extent reflects the large ±24.00 scale.
    assert "(lane ±24.00)" in svg
    # The realised drift_loss_delta is still labelled at full precision.
    assert "act -24.000" in svg
    # The "medium" magnitude token resolves to a real prediction on the
    # v1 row rather than silently dropping to no-prediction (the
    # original bug). The baseline row v0 still shows no-prediction
    # because the baseline carries no hypothesis.
    # Rows render in lineage order (v0 then v1), so the substring AFTER
    # the v1 row marker is the v1 row + footer only.
    v1_segment = svg.split(">v1</text>")[1]
    assert "(no prediction)" not in v1_segment


def test_hypothesis_vs_outcome_renders_hit_and_miss_glyphs() -> None:
    """Each lane gets a hit/miss glyph summarising the pair."""
    gens = (
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            pass_rate_delta=0.10,
            drift_loss_delta=-0.30,
            expected_pass_rate_delta="+0.05 to +0.15",
            expected_drift_movements=(
                {"kind": "off_topic", "direction": "decrease", "magnitude": "moderate"},
            ),
        ),
        _gen(
            gid="v2",
            parent="v1",
            decision="rejected",
            # Proposer predicted pass-rate up; outcome dropped it.
            pass_rate_delta=-0.05,
            drift_loss_delta=0.12,
            expected_pass_rate_delta="+0.10",
        ),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    assert ">hit</text>" in svg
    assert ">miss</text>" in svg


def test_hypothesis_vs_outcome_handles_missing_predictions_gracefully() -> None:
    """A round without `expected_*` fields renders ``(no prediction)`` + actual."""
    gens = (
        _gen(
            gid="v1",
            parent="v0",
            decision="rejected",
            pass_rate_delta=-0.05,
            drift_loss_delta=0.04,
            # No expected_* fields.
        ),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    assert "(no prediction)" in svg
    # Actual deltas are still rendered with the per-row decision token.
    assert "act -0.050" in svg
    assert "act +0.040" in svg


def test_hypothesis_vs_outcome_renders_from_workspace_fixture(tmp_path: Path) -> None:
    """End-to-end: gather a real workspace view then render the figure.

    Mirrors the live experiment.json shape (with the textual
    ``expected_pass_rate_delta`` ranges + ``magnitude`` words the
    proposer actually writes) so the figure regresses cleanly when the
    field-name contract changes.
    """
    from zicato.analyzer.report_data import gather_epoch_report_data

    ws = tmp_path / ".zicato"
    epoch = "ep"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    import json

    (edir / "config.json").write_text(json.dumps({"name": "ep"}))
    (edir / "board.jsonl").write_text("")
    (edir / "scoring.json").write_text(json.dumps({}))

    rounds = (
        ("v0", "", "baseline", None, {}),
        (
            "v1",
            "v0",
            "promoted",
            {
                "tournament_decision": "promoted",
                "scalar_score_delta": -14.4,
                "drift_loss_delta": -13.7,
                "pass_rate_delta": 0.0,
                "drift_movements": [],
            },
            {
                "core_idea": "Tighten the web developer's slide-structure instructions.",
                "expected_pass_rate_delta": "+0.10 to +0.20",
                "expected_drift_movements": [],
            },
        ),
        (
            "v2",
            "v1",
            "rejected",
            {
                "tournament_decision": "rejected",
                "scalar_score_delta": 10.1,
                "drift_loss_delta": 9.6,
                "pass_rate_delta": 0.17,
                "drift_movements": [],
            },
            {
                "core_idea": "Tighten researcher topical constraints.",
                "expected_pass_rate_delta": "+0.05 to +0.15",
                "expected_drift_movements": [
                    {
                        "direction": "decrease",
                        "kind": "off_topic",
                        "magnitude": "medium",
                    },
                ],
            },
        ),
    )
    for gen, parent, _, outcome, hypothesis in rounds:
        gd = edir / "generations" / gen
        gd.mkdir(parents=True)
        payload: dict[str, object] = {
            "generation_id": gen,
            "parent_generation_id": parent,
            "hypothesis": hypothesis or {"core_idea": "baseline"},
        }
        if outcome:
            payload["outcome"] = outcome
        (gd / "experiment.json").write_text(json.dumps(payload))

    data = gather_epoch_report_data(ws, epoch)
    svg = render_svg_hypothesis_vs_outcome(data)
    _assert_inline_svg(svg)
    # Every generation gets a row, including the baseline.
    assert ">v0</text>" in svg
    assert ">v1</text>" in svg
    assert ">v2</text>" in svg
    # The proposer's textual prediction is parsed and labelled —
    # v1's "+0.10 to +0.20" midpoint = +0.15.
    assert "pred +0.150" in svg
    # The realised pass-rate Δ for v2 (+0.17) appears with the actual prefix.
    assert "act +0.170" in svg
    # The "medium" magnitude resolves to a non-empty drift prediction
    # rather than silently dropping the bar (the previous bug). v2 is
    # the last row, so the substring after its marker contains only
    # v2's content + the footer.
    assert "(no prediction)" not in svg.split(">v2</text>")[1]
    # The core_idea snippet is surfaced in the row gutter so the
    # reader can scan WHAT was proposed alongside the prediction.
    # The snippet is truncated, so we look for an early-portion match.
    assert "Tighten the web developer" in svg


def test_hypothesis_vs_outcome_baseline_row_suppresses_lane_bars() -> None:
    """The v0 baseline row anchors the lineage but draws no lane bars.

    The baseline carries no proposer prediction AND no outcome delta;
    rendering "predicted zero / actual zero" would mislead the reader
    into thinking the baseline made a (correct) zero forecast. The row
    is still present (so v0 is visible in lineage order) but both
    lanes read ``(no prediction) / act —``.
    """
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            pass_rate_delta=0.10,
            drift_loss_delta=-0.30,
            expected_pass_rate_delta="+0.05 to +0.15",
        ),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    # The v0 row is rendered (gen id + decision pill).
    assert ">v0</text>" in svg
    assert ">baseline</text>" in svg
    # The baseline lanes carry the "act —" sentinel rather than
    # "act +0.000" — the row anchors lineage without faking a forecast.
    # Find the v0 row's segment (between v0 and v1 markers).
    v0_segment = svg.split(">v0</text>")[1].split(">v1</text>")[0]
    assert "act —" in v0_segment
    assert "(no prediction)" in v0_segment


def test_predicted_drift_delta_sum_accepts_medium_magnitude() -> None:
    """Direct unit test for the magnitude vocabulary the proposer writes.

    Live workspaces routinely include ``magnitude: "medium"`` in their
    expected_drift_movements entries; previously this token wasn't in
    the ``_MAGNITUDE_MAP`` so the figure silently rendered ``(no
    prediction)`` even when the proposer DID record a forecast.
    """
    from zicato.analyzer.report_figures import _predicted_drift_delta_sum

    g = _gen(
        gid="vX",
        parent="vP",
        decision="promoted",
        expected_drift_movements=(
            {
                "direction": "decrease",
                "kind": "off_topic",
                "magnitude": "medium",
            },
        ),
    )
    result = _predicted_drift_delta_sum(g)
    assert result is not None, "medium magnitude must resolve to a numeric prediction"
    assert result < 0.0, "decrease direction must produce a negative signed magnitude"


def test_parse_expected_pass_rate_delta_handles_range_and_single_value() -> None:
    """The proposer writes free-form pass-rate predictions.

    Ranges parse to their midpoint; bare signed numbers parse to that
    number; unparseable text falls through to ``None`` so the figure
    renders ``(no prediction)`` rather than a fake zero.
    """
    from zicato.analyzer.report_figures import _parse_expected_pass_rate_delta

    # Range — midpoint of [+0.05, +0.15] is +0.10.
    assert _parse_expected_pass_rate_delta("+0.05 to +0.15") == pytest.approx(0.10)
    # Single value.
    assert _parse_expected_pass_rate_delta("+0.10") == pytest.approx(0.10)
    # Negative range.
    assert _parse_expected_pass_rate_delta("-0.20 to -0.10") == pytest.approx(-0.15)
    # Unparseable.
    assert _parse_expected_pass_rate_delta("") is None
    assert _parse_expected_pass_rate_delta("unknown") is None


def test_hvo_hit_glyph_categorises_pairs_correctly() -> None:
    """The hit/miss glyph reflects directional agreement, not magnitude."""
    from zicato.analyzer.report_figures import _hvo_hit_glyph

    # Same direction (both positive) — hit, even if magnitudes differ.
    assert _hvo_hit_glyph(0.05, 0.50) == "hit"
    # Same direction (both negative) — hit.
    assert _hvo_hit_glyph(-0.05, -0.50) == "hit"
    # Opposite directions — miss.
    assert _hvo_hit_glyph(0.05, -0.50) == "miss"
    # Both ~zero — hit (the proposer correctly predicted no movement).
    assert _hvo_hit_glyph(0.0, 0.0) == "hit"
    # No prediction or no outcome — empty glyph.
    assert _hvo_hit_glyph(None, 0.10) == ""
    assert _hvo_hit_glyph(0.10, None) == ""


def test_hypothesis_vs_outcome_lane_extent_floors_with_only_baseline() -> None:
    """When only the baseline is scored, lane extents fall back to their floor.

    A degenerate input — every value is None or zero — must not produce
    a divide-by-zero or a zero-width lane. The lane extents floor at a
    sensible minimum so the lanes still render legibly.
    """
    gens = (_gen(gid="v0", is_baseline=True, decision="baseline"),)
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    # The pass-rate lane floor is 0.05; drift-loss lane floor is 0.10.
    # Each lane header carries its extent.
    assert "(lane ±0.05)" in svg
    assert "(lane ±0.10)" in svg


def test_hypothesis_vs_outcome_handles_single_rejected_challenger() -> None:
    """A campaign with only a rejected challenger still renders cleanly.

    Edge case: when every challenger was rejected, the figure should
    still render the proposer's prediction vs the realised outcome
    instead of dropping into the "no scored generations" placeholder.
    """
    gens = (
        _gen(
            gid="v1",
            parent="v0",
            decision="rejected",
            pass_rate_delta=-0.05,
            drift_loss_delta=0.10,
            expected_pass_rate_delta="+0.05",
            expected_drift_movements=(
                {"kind": "off_topic", "direction": "decrease", "magnitude": "small"},
            ),
        ),
    )
    svg = render_svg_hypothesis_vs_outcome(_data(gens))
    _assert_inline_svg(svg)
    assert "No scored generations" not in svg
    assert ">v1</text>" in svg
    assert ">rejected</text>" in svg
    assert "pred +0.050" in svg
    assert "act -0.050" in svg
    # The proposer predicted pass-rate up; outcome dropped it — the
    # lane is a miss.
    assert ">miss</text>" in svg


# ---------------------------------------------------------------------------
# Mutation-impact matrix
# ---------------------------------------------------------------------------


def test_mutation_impact_matrix_empty_returns_placeholder() -> None:
    svg = render_svg_mutation_impact_matrix(_data())
    _assert_inline_svg(svg)
    assert "empty" in svg or "no challengers" in svg.lower()


def test_mutation_impact_matrix_renders_cells_per_touched_site_x_generation() -> None:
    """Rows = touched mutation sites; columns = challenger generations.

    A cell is filled with the round's outcome colour when the site was
    touched in that generation, blank otherwise.
    """
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
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
    surface = (
        {"id": "sys_prompt", "kind": "prompt_text", "file": "agent/prompt.txt"},
        {"id": "temp", "kind": "numeric", "file": "agent/config.py"},
        # An untouched site is in the surface but should be dropped from
        # the matrix (only touched sites are rendered).
        {"id": "max_tokens", "kind": "numeric", "file": "agent/config.py"},
    )
    svg = render_svg_mutation_impact_matrix(_data(gens, mutation_surface=surface))
    _assert_inline_svg(svg)
    # Both challenger generations are column headers; baseline excluded.
    assert ">v1</text>" in svg
    assert ">v2</text>" in svg
    assert ">v0</text>" not in svg
    # Touched site ids appear as row labels.
    assert "sys_prompt" in svg
    assert "temp" in svg
    # Untouched site is dropped from the matrix.
    assert "max_tokens" not in svg
    # Palette: promoted + rejected cells use the matching tokens.
    assert "var(--paper-promoted)" in svg
    assert "var(--paper-rejected)" in svg
    # Outcome chips in the legend at the bottom.
    assert ">promoted</text>" in svg
    assert ">rejected</text>" in svg
    assert ">incomplete</text>" in svg
    # No raw hex in the rendered SVG.
    assert PROMOTED_COLOR not in svg
    assert REJECTED_COLOR not in svg


def test_mutation_impact_matrix_handles_no_touched_sites() -> None:
    """A challenger campaign with no patches yields a no-patches placeholder."""
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        # A challenger with no patches at all (rare but possible — proposer
        # produced no valid patch set).
        _gen(gid="v1", parent="v0", decision="rejected", patches=()),
    )
    svg = render_svg_mutation_impact_matrix(_data(gens))
    _assert_inline_svg(svg)
    assert "No patches" in svg or "no patches" in svg.lower()


# ---------------------------------------------------------------------------
# Polished existing-figure refinements
# ---------------------------------------------------------------------------


def test_score_trajectory_includes_axis_labels_and_legend() -> None:
    """The polished trajectory carries y/x axis labels + a top-strip legend.

    The legend uses the same palette tokens the markers use, so the host
    palette controls the rendered hue uniformly.
    """
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline", cumulative_scalar=0.0),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            scalar_score_delta=-0.2,
            cumulative_scalar=-0.2,
        ),
    )
    svg = render_svg_score_trajectory(_data(gens))
    _assert_inline_svg(svg)
    # Axis labels (y and x).
    assert "scalar (loss" in svg
    assert ">generation</text>" in svg
    # Top legend strip carries the three decision markers.
    assert ">promoted</text>" in svg
    assert ">rejected</text>" in svg
    assert ">baseline</text>" in svg
    # The legend uses the same palette tokens as the markers — no raw hex.
    assert "var(--paper-promoted)" in svg
    assert "var(--paper-rejected)" in svg


def test_per_board_heatmap_legend_uses_palette_tokens() -> None:
    """The heatmap legend swatches paint with the same tokens the cells do."""
    entries = (_entry("slides"),)
    gens = (
        _gen(gid="v0", is_baseline=True, decision="baseline"),
        _gen(
            gid="v1",
            parent="v0",
            decision="promoted",
            gen_score={"entries": {"slides": {"scalar_delta": -0.08}}},
        ),
    )
    svg = render_svg_per_board_heatmap(_data(gens, entries))
    _assert_inline_svg(svg)
    # Both worse + better chips bind to the palette tokens.
    assert ">worse</text>" in svg
    assert ">better</text>" in svg
    assert ">flat</text>" in svg
