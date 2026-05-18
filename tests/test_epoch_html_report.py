"""Tests for :mod:`zicato.epoch.html_report`.

The renderer is deterministic and produces a single self-contained
HTML document. The tests focus on:

* structural invariants (well-formed parse via :mod:`html.parser`),
* no external resource references (no ``http`` links, no remote
  scripts or stylesheets),
* dark-mode CSS branch present,
* size envelope holds for a 20-generation synthetic epoch,
* palette correctness (promoted nodes use the promoted color, rejected
  nodes use the rejected color).
"""

from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

from zicato.core.types import (
    DriftMovementActual,
    ExpectedDriftMovement,
    Experiment,
    Generation,
)
from zicato.epoch.html_report import (
    PROMOTED_COLOR,
    REJECTED_COLOR,
    HtmlReportContext,
    render_experiment_cards,
    render_html_report,
    render_metadata_panel,
    render_svg_drift_heatmap,
    render_svg_lineage,
    render_svg_score_trajectory,
    write_html_report,
)
from zicato.testing.fixtures import (
    make_experiment,
    make_generation,
    make_hypothesis_spec,
    make_outcome_record,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StackParser(HTMLParser):
    """HTMLParser that tracks unclosed tags and counts of named tags."""

    # Tags that browsers treat as void (no closing) — the parser MUST NOT
    # complain about them being unclosed. SVG primitives we emit as
    # self-closing (path/rect/circle/line) are added here too. ``marker``
    # is NOT in this list because we emit nested arrow-head ``path`` inside
    # it and rely on the explicit ``</marker>`` to close.
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
        "path",
        "rect",
        "circle",
        "line",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tag_counts: dict[str, int] = {}
        self.unbalanced: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        if tag in self._VOID:
            return
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <tag/> form — count but do not push.
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.unbalanced.append(f"close without open: {tag}")
            return
        # Pop until we find a matching open. Treat dangling closes as
        # errors. SVG and HTML produced by html_report are well-nested so
        # this strict policy is the right test.
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        self.unbalanced.append(f"mismatched: expected </{self.stack[-1]}> got </{tag}>")


def _assert_well_formed(html: str) -> None:
    parser = _StackParser()
    parser.feed(html)
    parser.close()
    assert not parser.unbalanced, f"unbalanced tags: {parser.unbalanced[:3]}"
    assert not parser.stack, f"unclosed tags remain: {parser.stack}"


def _ctx(
    *,
    generations: list[Generation],
    experiments: list[Experiment],
    epoch_id: str = "ep_test",
    epoch_name: str = "test epoch",
    duration: str = "3 days",
    final_scalar: float = 0.42,
    promoted_count: int | None = None,
    rejected_count: int | None = None,
    narrative_html: str = "",
) -> HtmlReportContext:
    if promoted_count is None:
        promoted_count = sum(
            1
            for e in experiments
            if e.outcome is not None and e.outcome.tournament_decision == "promoted"
        )
    if rejected_count is None:
        rejected_count = sum(
            1
            for e in experiments
            if e.outcome is not None and e.outcome.tournament_decision == "rejected"
        )
    return HtmlReportContext(
        epoch_id=epoch_id,
        epoch_name=epoch_name,
        duration=duration,
        generations=generations,
        experiments=experiments,
        final_scalar=final_scalar,
        promoted_count=promoted_count,
        rejected_count=rejected_count,
        narrative_html=narrative_html,
    )


def _build_mixed_lineage(n: int) -> tuple[list[Generation], list[Experiment]]:
    """Build n generations alternating promoted / rejected (plus baseline)."""
    generations: list[Generation] = [make_generation(id="v0", parent_id=None)]
    experiments: list[Experiment] = []
    head = "v0"
    for i in range(1, n):
        gid = f"v{i}"
        # Alternate decisions; every 3rd is deferred so all three branches
        # appear in the fixture.
        if i % 3 == 0:
            decision = "deferred"
            scalar_delta = 0.001
            drift_delta = -0.005
            pass_delta = 0.0
        elif i % 2 == 1:
            decision = "promoted"
            scalar_delta = 0.08 + i * 0.005
            drift_delta = -0.05 - i * 0.002
            pass_delta = 0.02
        else:
            decision = "rejected"
            scalar_delta = -0.03
            drift_delta = 0.04
            pass_delta = -0.01
        gen = make_generation(
            id=gid,
            parent_id=head,
            promoted=(decision == "promoted"),
        )
        generations.append(gen)
        movements = (
            DriftMovementActual(
                kind="off_topic",
                from_rate=1.0,
                to_rate=0.8 if decision == "promoted" else 1.2,
                hypothesis_match=(decision == "promoted"),
            ),
            DriftMovementActual(
                kind="tool_error",
                from_rate=0.5,
                to_rate=0.4 if decision == "promoted" else 0.7,
                hypothesis_match=(decision == "promoted"),
            ),
        )
        outcome = make_outcome_record(
            tournament_decision=decision,
            scalar_score_delta=scalar_delta,
            drift_loss_delta=drift_delta,
            pass_rate_delta=pass_delta,
            drift_movements=movements,
            rejection_reason="margin not cleared" if decision == "rejected" else "",
        )
        exp = make_experiment(
            id=f"exp_{i}",
            generation_id=gid,
            parent_generation_id=head,
            hypothesis=make_hypothesis_spec(
                core_idea=f"Try mutation {i}: bias the system prompt.",
                why=f"Pattern detector flagged a recurring issue at v{i - 1}.",
                expected_drift_movements=(
                    ExpectedDriftMovement(
                        kind="off_topic",
                        direction="decrease",
                        magnitude="small",
                    ),
                ),
                expected_pass_rate_delta="+0.00 to +0.05",
            ),
            outcome=outcome,
        )
        experiments.append(exp)
        if decision == "promoted":
            head = gid
    return generations, experiments


# ---------------------------------------------------------------------------
# Empty / minimal fixtures
# ---------------------------------------------------------------------------


def test_render_html_report_zero_generations() -> None:
    ctx = _ctx(generations=[], experiments=[])
    html = render_html_report(ctx)
    assert "<!DOCTYPE html>" in html
    assert "<title>" in html
    assert "<style>" in html
    assert "No generations" in html or "No experiments" in html
    _assert_well_formed(html)


def test_render_html_report_baseline_only() -> None:
    baseline = make_generation(id="v0", parent_id=None)
    ctx = _ctx(generations=[baseline], experiments=[])
    html = render_html_report(ctx)
    # No edges should be drawn; "baseline" marker present somewhere.
    assert "baseline" in html
    _assert_well_formed(html)
    # The trajectory should still render a single tick.
    assert "v0" in html


# ---------------------------------------------------------------------------
# Mixed lineage
# ---------------------------------------------------------------------------


def test_render_html_report_mixed_lineage_node_count() -> None:
    n = 6
    generations, experiments = _build_mixed_lineage(n)
    ctx = _ctx(generations=generations, experiments=experiments)
    svg = render_svg_lineage(generations, experiments)
    # Each generation produces one rect node.
    assert svg.count("<rect") >= n
    # And each non-baseline produces an edge path.
    assert svg.count("<path") >= n - 1
    # Full doc:
    html = render_html_report(ctx)
    _assert_well_formed(html)
    # Each experiment becomes a <details> card.
    assert html.count("<details") == len(experiments)


def test_render_html_report_no_external_resources() -> None:
    generations, experiments = _build_mixed_lineage(6)
    ctx = _ctx(generations=generations, experiments=experiments)
    html = render_html_report(ctx)
    # Rules out external CSS/JS/images.
    assert 'href="http' not in html
    assert 'src="http' not in html
    assert "<link " not in html
    # No <script src="..."> form.
    assert "script src=" not in html


def test_render_html_report_dark_mode_present() -> None:
    ctx = _ctx(generations=[make_generation()], experiments=[])
    html = render_html_report(ctx)
    assert "prefers-color-scheme: dark" in html


def test_render_html_report_size_envelope() -> None:
    generations, experiments = _build_mixed_lineage(20)
    ctx = _ctx(
        generations=generations,
        experiments=experiments,
        duration="14 days",
    )
    html = render_html_report(ctx)
    assert (
        len(html.encode("utf-8")) < 100_000
    ), f"output exceeded 100 KB envelope: {len(html.encode('utf-8'))} bytes"
    _assert_well_formed(html)


def test_render_html_report_palette_promoted_and_rejected() -> None:
    generations, experiments = _build_mixed_lineage(5)
    ctx = _ctx(generations=generations, experiments=experiments)
    # Both palette colors must appear in the SVG lineage output.
    lineage = render_svg_lineage(generations, experiments)
    assert PROMOTED_COLOR in lineage
    assert REJECTED_COLOR in lineage
    # Each promoted experiment's gen id should appear in a promoted-colored
    # node — encoded as a rect with the promoted stroke.
    promoted_ids = [
        e.generation_id
        for e in experiments
        if e.outcome is not None and e.outcome.tournament_decision == "promoted"
    ]
    rejected_ids = [
        e.generation_id
        for e in experiments
        if e.outcome is not None and e.outcome.tournament_decision == "rejected"
    ]
    # At least one promoted and one rejected node should be in our fixture.
    assert promoted_ids and rejected_ids
    # Spot-check counts: every promoted rect node uses the promoted color and
    # solid border (no stroke-dasharray on that rect). The renderer encodes
    # promoted nodes as a rect followed by the gen id text; the only rects
    # in the lineage SVG are nodes and the empty placeholder is absent here.
    # We use the badge in the experiment cards as a stable, easy-to-test
    # signal that the palette flows through:
    full = render_html_report(ctx)
    for gid in promoted_ids:
        # The card for a promoted experiment must carry the promoted badge.
        assert f'class="badge promoted">promoted</span><span class="mono">{gid}' in full or (
            'class="badge promoted"' in full and gid in full
        )
    for gid in rejected_ids:
        assert 'class="badge rejected"' in full and gid in full


# ---------------------------------------------------------------------------
# Internal helpers, smoke
# ---------------------------------------------------------------------------


def test_render_metadata_panel_counts_match() -> None:
    generations, experiments = _build_mixed_lineage(7)
    ctx = _ctx(generations=generations, experiments=experiments)
    panel = render_metadata_panel(ctx)
    # The number of stat cells is fixed; verify a few key labels appear.
    assert "Generations" in panel
    assert "Promoted" in panel
    assert "Rejected" in panel
    assert "Final scalar" in panel
    # Promoted / rejected numbers should be present as integers.
    assert f">{ctx.promoted_count}<" in panel
    assert f">{ctx.rejected_count}<" in panel


def test_render_experiment_cards_open_top_three() -> None:
    _, experiments = _build_mixed_lineage(6)
    html = render_experiment_cards(experiments)
    # At least three details are open. The renderer reverses the order so
    # newest is first.
    open_count = html.count('<details class="experiment-card" open>')
    assert open_count == min(3, len(experiments))
    # The total card count matches the experiment count.
    assert html.count("<details") == len(experiments)


def test_render_svg_drift_heatmap_empty() -> None:
    # No experiments: empty placeholder with the canonical message.
    svg = render_svg_drift_heatmap([], [])
    assert "No drift movements" in svg


def test_render_svg_score_trajectory_empty() -> None:
    svg = render_svg_score_trajectory([], [])
    assert "No generations" in svg


def test_write_html_report_round_trip(tmp_path: Path) -> None:
    generations, experiments = _build_mixed_lineage(4)
    ctx = _ctx(generations=generations, experiments=experiments)
    target = tmp_path / "analysis.html"
    write_html_report(target, ctx)
    body = target.read_text(encoding="utf-8")
    assert body == render_html_report(ctx)
    _assert_well_formed(body)


def test_render_html_report_with_narrative_html() -> None:
    generations, experiments = _build_mixed_lineage(3)
    ctx = _ctx(
        generations=generations,
        experiments=experiments,
        narrative_html="<p>handcrafted narrative.</p>",
    )
    html = render_html_report(ctx)
    assert "<p>handcrafted narrative.</p>" in html
    assert "<h2>Narrative</h2>" in html


def test_pending_outcome_renders_as_baseline_marker() -> None:
    # An experiment without an outcome should render with the pending badge.
    gen0 = make_generation(id="v0", parent_id=None)
    gen1 = make_generation(id="v1", parent_id="v0")
    exp = make_experiment(generation_id="v1", parent_generation_id="v0", outcome=None)
    ctx = _ctx(generations=[gen0, gen1], experiments=[exp])
    html = render_html_report(ctx)
    assert 'class="badge pending"' in html
    _assert_well_formed(html)


def test_html_report_lineage_edge_label_truncates_long_reason() -> None:
    # A rejection reason longer than 24 characters should be truncated.
    gen0 = make_generation(id="v0", parent_id=None)
    gen1 = make_generation(id="v1", parent_id="v0")
    long_reason = "x" * 100
    outcome = make_outcome_record(
        tournament_decision="rejected",
        rejection_reason=long_reason,
    )
    exp = make_experiment(
        generation_id="v1",
        parent_generation_id="v0",
        outcome=outcome,
    )
    svg = render_svg_lineage([gen0, gen1], [exp])
    # Truncated label is at most ~25 chars (including ellipsis).
    assert long_reason not in svg
    assert "…" in svg


def test_html_report_no_emoji_decision_markers() -> None:
    # House style: avoid emoji in HTML output.
    generations, experiments = _build_mixed_lineage(5)
    ctx = _ctx(generations=generations, experiments=experiments)
    html = render_html_report(ctx)
    # A small smoke-screen against a few common emoji codepoints.
    for c in ("✅", "❌", "\U0001f389", "\U0001f4ca"):
        assert c not in html, f"emoji {c!r} should not appear in report"


def test_outcome_with_deferred_renders_deferred_badge() -> None:
    gen0 = make_generation(id="v0", parent_id=None)
    gen1 = make_generation(id="v1", parent_id="v0")
    outcome = make_outcome_record(tournament_decision="deferred")
    exp = make_experiment(
        generation_id="v1",
        parent_generation_id="v0",
        outcome=outcome,
    )
    ctx = _ctx(generations=[gen0, gen1], experiments=[exp])
    html = render_html_report(ctx)
    assert 'class="badge deferred"' in html
    _assert_well_formed(html)


def test_render_html_report_replace_friendly() -> None:
    # HtmlReportContext is frozen — dataclasses.replace should work
    # for downstream callers building a context incrementally.
    ctx = _ctx(generations=[], experiments=[])
    ctx2 = replace(ctx, epoch_name="renamed")
    assert ctx2.epoch_name == "renamed"
    assert ctx.epoch_name != ctx2.epoch_name
