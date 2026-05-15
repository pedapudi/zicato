"""Tests for the deterministic tournament-outcome diagrams in :mod:`zicato.epoch.analysis`.

These cover the four pure rendering primitives plus the integrated
section composer. The renderers take typed ``list[Generation]`` /
``list[Experiment]`` inputs so the tests can construct fixtures directly
via :mod:`zicato.testing.fixtures` without touching the on-disk
experiment format (which is being refactored concurrently).
"""

from __future__ import annotations

from zicato.core.types import (
    DriftMovementActual,
    Experiment,
    Generation,
    OutcomeRecord,
)
from zicato.epoch.analysis import (
    render_drift_kind_movement_table,
    render_mermaid_lineage,
    render_score_sparkline,
    render_tournament_outcomes_section,
    render_trajectory_table,
)
from zicato.testing.fixtures import (
    make_experiment,
    make_generation,
    make_hypothesis_spec,
    make_outcome_record,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _baseline_gen(gid: str = "v0") -> Generation:
    return make_generation(id=gid, parent_id=None, promoted=True)


def _child_gen(gid: str, parent_id: str, promoted: bool) -> Generation:
    return make_generation(id=gid, parent_id=parent_id, promoted=promoted)


def _experiment(
    gid: str,
    parent_id: str,
    *,
    decision: str | None = "promoted",
    scalar_delta: float = -0.10,
    pass_delta: float = 0.0,
    drift_loss_delta: float = -0.05,
    drift_movements: tuple[DriftMovementActual, ...] = (),
    rejection_reason: str = "",
    core_idea: str = "Refine the system prompt.",
) -> Experiment:
    if decision is None:
        outcome: OutcomeRecord | None = None
    else:
        outcome = make_outcome_record(
            tournament_decision=decision,
            scalar_score_delta=scalar_delta,
            pass_rate_delta=pass_delta,
            drift_loss_delta=drift_loss_delta,
            drift_movements=drift_movements,
            rejection_reason=rejection_reason,
        )
    return make_experiment(
        id=f"exp_{gid}",
        generation_id=gid,
        parent_generation_id=parent_id,
        hypothesis=make_hypothesis_spec(core_idea=core_idea),
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# render_mermaid_lineage
# ---------------------------------------------------------------------------


def test_mermaid_lineage_empty() -> None:
    out = render_mermaid_lineage([], [])
    assert out.startswith("```mermaid")
    assert out.endswith("```")
    assert "(no generations)" in out


def test_mermaid_lineage_single_baseline() -> None:
    gens = [_baseline_gen("v0")]
    out = render_mermaid_lineage(gens, [])
    assert "graph LR" in out
    # One node, no edges (no ==>, no -.->).
    assert 'v0["v0' in out
    assert "==>" not in out
    assert "-.->" not in out
    # Baseline class assignment.
    assert "class v0 baseline" in out


def test_mermaid_lineage_mixed_edges_use_correct_arrows() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
        _child_gen("v2", "v1", promoted=False),
        _child_gen("v3", "v1", promoted=True),
        _child_gen("v4", "v3", promoted=False),
    ]
    exps = [
        _experiment("v1", "v0", decision="promoted", scalar_delta=-0.30),
        _experiment(
            "v2",
            "v1",
            decision="rejected",
            scalar_delta=+0.20,
            rejection_reason="TOOL_ERROR regression",
        ),
        _experiment("v3", "v1", decision="promoted", scalar_delta=-0.10),
        _experiment(
            "v4",
            "v3",
            decision="rejected",
            scalar_delta=+0.05,
            rejection_reason="pass_rate_regression",
        ),
    ]
    out = render_mermaid_lineage(gens, exps)

    # Promoted edges use the thick arrow.
    assert "v0 ==>|promoted| v1" in out
    assert "v1 ==>|promoted| v3" in out
    # Rejected edges use the dashed arrow with a (possibly truncated)
    # rejection reason as the label.
    assert "v1 -.->|TOOL_ERROR regression| v2" in out
    assert "v3 -.->|pass_rate_regression| v4" in out

    # Counts: 1 baseline node + 4 child nodes = 5 declarations.
    node_decls = [ln for ln in out.splitlines() if '["' in ln]
    assert len(node_decls) == 5

    # Promoted / rejected class assignments are present.
    assert "class" in out
    assert "v1" in out and "v3" in out  # promoted set
    assert "promoted" in out
    assert "rejected" in out


def test_mermaid_lineage_truncates_long_rejection_reason() -> None:
    long_reason = "A" * 80
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=False),
    ]
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="rejected",
            scalar_delta=+0.10,
            rejection_reason=long_reason,
        )
    ]
    out = render_mermaid_lineage(gens, exps)
    # No edge label should be longer than ~30 chars + the trailing ellipsis.
    assert long_reason not in out
    assert "…" in out


def test_mermaid_lineage_sanitizes_label_characters() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=False),
    ]
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="rejected",
            scalar_delta=+0.10,
            rejection_reason='broke "tool" <flow>',
        )
    ]
    out = render_mermaid_lineage(gens, exps)
    # Raw quotes / angle brackets must not appear inside the edge label.
    assert '"tool"' not in out
    assert "<flow>" not in out
    assert "&quot;" in out or "&lt;" in out


# ---------------------------------------------------------------------------
# render_trajectory_table
# ---------------------------------------------------------------------------


def test_trajectory_table_has_row_per_generation() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
        _child_gen("v2", "v1", promoted=False),
    ]
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="promoted",
            scalar_delta=-0.20,
            core_idea="Tighten the writer prompt.",
        ),
        _experiment(
            "v2", "v1", decision="rejected", scalar_delta=+0.15, core_idea="Loosen safety filter."
        ),
    ]
    out = render_trajectory_table(gens, exps)

    # Header.
    assert "| gen | score | Δ from parent | decision | core_idea |" in out
    # One row per generation (3 data rows beyond the 2 header rows).
    data_rows = [ln for ln in out.splitlines() if ln.startswith("| v")]
    assert len(data_rows) == 3
    # Decisions appear in their expected columns.
    assert "baseline" in out
    assert "promoted" in out
    assert "rejected" in out
    # Core ideas surface.
    assert "Tighten the writer prompt." in out
    assert "Loosen safety filter." in out


def test_trajectory_table_marks_pending_outcomes() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=False),
    ]
    exps = [
        _experiment("v1", "v0", decision=None, core_idea="Pending experiment."),
    ]
    out = render_trajectory_table(gens, exps)
    # Pending row contains the marker word; delta is blank.
    pending_row = next(ln for ln in out.splitlines() if ln.startswith("| v1"))
    assert "pending" in pending_row


def test_trajectory_table_empty() -> None:
    out = render_trajectory_table([], [])
    assert "no generations" in out


# ---------------------------------------------------------------------------
# render_score_sparkline
# ---------------------------------------------------------------------------


def test_sparkline_is_deterministic_snapshot() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
        _child_gen("v2", "v1", promoted=False),
        _child_gen("v3", "v1", promoted=True),
    ]
    exps = [
        _experiment("v1", "v0", decision="promoted", scalar_delta=-0.30),
        _experiment(
            "v2",
            "v1",
            decision="rejected",
            scalar_delta=+0.20,
            rejection_reason="TOOL_ERROR_regression",
        ),
        _experiment("v3", "v1", decision="promoted", scalar_delta=-0.05),
    ]
    out = render_score_sparkline(gens, exps, width=20)

    # Wrapped in a plain fenced block.
    lines = out.splitlines()
    assert lines[0] == "```"
    assert lines[-1] == "```"

    body = lines[1:-1]
    assert len(body) == 4

    # Each body line starts with the generation id and a colon.
    for gid, line in zip(["v0", "v1", "v2", "v3"], body, strict=True):
        assert line.startswith(f"{gid}:")

    # Bars use the documented glyphs only.
    body_text = "\n".join(body)
    assert "█" in body_text
    assert "░" in body_text

    # The arrow + decision word annotations appear with the correct sign.
    assert "↓ -0.300" in body[1]
    assert "↑ +0.200" in body[2]
    assert "↓ -0.050" in body[3]
    assert "PROMOTED" in body[1]
    assert "REJECTED" in body[2]
    assert "PROMOTED" in body[3]

    # Rejection reason gets surfaced in the trailing bracket.
    assert "[TOOL_ERROR_regression]" in body[2]

    # The final row is marked as current.
    assert body[-1].endswith("← current")


def test_sparkline_empty_generations() -> None:
    out = render_score_sparkline([], [])
    assert out.startswith("```")
    assert "(no generations)" in out


def test_sparkline_handles_baseline_only() -> None:
    gens = [_baseline_gen("v0")]
    out = render_score_sparkline(gens, [], width=10)
    body = [ln for ln in out.splitlines() if ln.startswith("v0")]
    assert len(body) == 1
    assert "baseline" in body[0]
    assert "← current" in body[0]


# ---------------------------------------------------------------------------
# render_drift_kind_movement_table
# ---------------------------------------------------------------------------


def _movement(kind: str, frm: float, to: float) -> DriftMovementActual:
    return DriftMovementActual(
        kind=kind,
        from_rate=frm,
        to_rate=to,
        hypothesis_match=True,
    )


def test_drift_table_orders_by_abs_net_change_and_caps_at_12() -> None:
    # Build a v0 baseline plus several promoted children, each adding a
    # different drift movement. Promoted lineage is linear: v0 -> v1 ->
    # ... -> vN. The final v1 step shifts every kind by a known amount.
    gens = [_baseline_gen("v0"), _child_gen("v1", "v0", promoted=True)]
    # 15 drift kinds with monotonically-decreasing magnitude.
    movements: list[DriftMovementActual] = []
    for i in range(15):
        # net = abs(to - frm) = (15 - i) / 100 — gives 0.15 down to 0.01.
        magnitude = (15 - i) / 100.0
        movements.append(_movement(f"k_{i:02d}", 1.0, 1.0 - magnitude))
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="promoted",
            scalar_delta=-0.10,
            drift_movements=tuple(movements),
        )
    ]
    out = render_drift_kind_movement_table(gens, exps)

    # Header + separator + 12 data rows = 14 total lines.
    rows = [ln for ln in out.splitlines() if ln.startswith("|")]
    # Strip header (1) + separator (1) → 12 data rows.
    assert len(rows) == 2 + 12

    # Largest magnitude kind (k_00, net = -0.15) appears before the
    # smallest retained kind (k_11, net = -0.04).
    body = "\n".join(rows[2:])
    assert body.index("k_00") < body.index("k_11")
    # The capped-out kind (k_14, smallest magnitude) should NOT appear.
    assert "k_14" not in body


def test_drift_table_empty_when_no_movements() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
    ]
    exps = [_experiment("v1", "v0", decision="promoted", scalar_delta=-0.10)]
    out = render_drift_kind_movement_table(gens, exps)
    assert out == ""


def test_drift_table_chains_rates_across_multiple_promoted_steps() -> None:
    # v0 -> v1 (promoted) -> v2 (rejected) -> v3 (promoted off v1).
    # Only v1 and v3 should populate rate columns; v2 is a dead branch
    # the function ignores.
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
        _child_gen("v2", "v1", promoted=False),
        _child_gen("v3", "v1", promoted=True),
    ]
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="promoted",
            scalar_delta=-0.10,
            drift_movements=(_movement("off_topic", 1.00, 0.60),),
        ),
        _experiment(
            "v2",
            "v1",
            decision="rejected",
            scalar_delta=+0.20,
            drift_movements=(_movement("off_topic", 0.60, 5.00),),
        ),
        _experiment(
            "v3",
            "v1",
            decision="promoted",
            scalar_delta=-0.05,
            drift_movements=(_movement("off_topic", 0.60, 0.40),),
        ),
    ]
    out = render_drift_kind_movement_table(gens, exps)
    # Header references the promoted-chain generations only.
    assert "v0_rate" in out
    assert "v1_rate" in out
    assert "v3_rate" in out or "final_rate" in out
    # The rejected branch's outcome (5.00) must NOT leak into the table.
    assert "5.000" not in out
    # Net change = 0.40 - 1.00 = -0.60.
    assert "-0.600" in out


# ---------------------------------------------------------------------------
# render_tournament_outcomes_section
# ---------------------------------------------------------------------------


def test_section_integrates_all_four_renderers() -> None:
    gens = [
        _baseline_gen("v0"),
        _child_gen("v1", "v0", promoted=True),
        _child_gen("v2", "v1", promoted=False),
    ]
    exps = [
        _experiment(
            "v1",
            "v0",
            decision="promoted",
            scalar_delta=-0.20,
            drift_movements=(_movement("off_topic", 1.0, 0.5),),
        ),
        _experiment(
            "v2",
            "v1",
            decision="rejected",
            scalar_delta=+0.10,
            rejection_reason="regression",
        ),
    ]
    out = render_tournament_outcomes_section(gens, exps)
    assert "## Tournament outcomes" in out
    assert "### Lineage" in out
    assert "```mermaid" in out
    assert "### Scalar trajectory" in out
    assert "| gen | score |" in out
    assert "### Score sparkline" in out
    assert "← current" in out
    assert "### Drift-kind movements across the promoted lineage" in out
    assert "off_topic" in out


def test_section_elides_drift_subsection_when_no_movements() -> None:
    gens = [_baseline_gen("v0"), _child_gen("v1", "v0", promoted=True)]
    exps = [_experiment("v1", "v0", decision="promoted", scalar_delta=-0.10)]
    out = render_tournament_outcomes_section(gens, exps)
    assert "## Tournament outcomes" in out
    assert "### Drift-kind movements" not in out


def test_section_with_empty_inputs_is_graceful() -> None:
    out = render_tournament_outcomes_section([], [])
    assert "## Tournament outcomes" in out
    # Mermaid placeholder still present.
    assert "(no generations)" in out


# ---------------------------------------------------------------------------
# Hard cases — outcome=None, long rejection reasons in the section
# ---------------------------------------------------------------------------


def test_section_handles_pending_generation_in_table() -> None:
    gens = [_baseline_gen("v0"), _child_gen("v1", "v0", promoted=False)]
    exps = [_experiment("v1", "v0", decision=None, core_idea="Trial run, no decision yet.")]
    out = render_tournament_outcomes_section(gens, exps)
    # The trajectory table marks v1 as pending.
    table_lines = [ln for ln in out.splitlines() if ln.startswith("| v1")]
    assert table_lines
    assert "pending" in table_lines[0]
