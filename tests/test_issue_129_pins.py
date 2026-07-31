"""Triage pins for the two cross-cutting patterns in issue #129.

Issue #129 generalises eleven reports (#118-#128) into two claims:

**A. Detection without explanation.** zicato detects that something is
wrong and fails to say what. Triage refined the claim: the evidence is
*collected* — all 19 :class:`~zicato.health.diagnostics.HealthFinding`
construction sites populate the documented ``detail`` dict ("the numbers
that tripped the detector"). The gap is the **render** hop. The
operator-facing one-liner is built by
:func:`zicato.orchestrator._summarise_loop_health`, whose ``_text``
helper accepts only *string* attributes (``message`` / ``summary`` /
``detail`` / ``description``) and returns the first non-empty one.
``detail`` is a **dict**, so it is skipped by an ``isinstance(val, str)``
guard: every number the detector measured is structurally unreachable
from the line the operator actually reads. The evidence does reach the
per-round health JSON (via :func:`zicato.orchestrator._loop_health_to_json`,
which uses ``dataclasses.asdict``) — so this is a surfacing gap, not a
collection gap, and the fix is cheap.

The bar is not invented here. zicato's sibling diagnostic contract,
:class:`zicato.reflection.practices.PracticeCheck`, already specifies it:
``headline`` is documented as "a single sentence **with the numbers
inline**", carried alongside a structured ``evidence`` dict and a
``rationale``. ``HealthFinding.summary`` has no such requirement, and the
renderer drops the dict that would have supplied the numbers.

**B. Surfaces that assume the champion advances.** ``PracticeCheck`` also
models the fix for pattern B: an explicit ``VERDICT_UNMEASURED`` plus an
``unmeasured_reason`` naming the missing input — e.g.
``check_promotion_hygiene`` answers "No promotions under this contract
yet — promotion hygiene has nothing to audit" rather than reporting a
vacuous pass. Surfaces that degrade silently when the champion is
retained lack that third state: they cannot distinguish "measured, and
it is fine" from "there was nothing to measure".

These are ``xfail(strict=True)`` triage pins: they fail today, and the
marker comes off with the fix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zicato.health.diagnostics import HealthFinding, LoopHealth
from zicato.orchestrator import _summarise_loop_health
from zicato.tournament.detail import optimization_trajectory

EPOCH = "2026-07_e0"

_SCHEMA = """
CREATE TABLE epochs (epoch_id TEXT, name TEXT, created_at TEXT);
CREATE TABLE generations (
    epoch_id TEXT, generation_id TEXT, parent_generation_id TEXT, promoted INTEGER
);
CREATE TABLE tournaments (
    tournament_id TEXT, epoch_id TEXT,
    parent_generation_id TEXT, child_generation_id TEXT,
    decision TEXT, parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
    rejection_reason TEXT, ran_at TEXT
);
CREATE TABLE experiments (
    epoch_id TEXT, generation_id TEXT,
    hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT,
    tournament_decision TEXT, rejection_reason TEXT,
    scalar_score_delta REAL, drift_loss_delta REAL, pass_rate_delta REAL,
    outcome_json TEXT
);
CREATE TABLE patches (
    patch_id TEXT, epoch_id TEXT, generation_id TEXT,
    mutation_id TEXT, op TEXT, rationale TEXT
);
CREATE TABLE runs (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    runtime_ms INTEGER, aborted INTEGER
);
CREATE TABLE loss_profiles (
    run_id TEXT, epoch_id TEXT, generation_id TEXT, entry_id TEXT,
    drift_loss REAL, pass_fail INTEGER, loss_json TEXT
);
CREATE TABLE metric_counts (
    run_id TEXT, namespace TEXT, name TEXT, severity TEXT, count REAL
);
"""


def _index_db(path: Path, generations: list[tuple], tournaments: list[tuple]) -> Path:
    """Build a synthetic analytical index holding one epoch's spine."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO epochs VALUES (?,?,?)", (EPOCH, "e0", "x"))
        conn.executemany("INSERT INTO generations VALUES (?,?,?,?)", generations)
        conn.executemany("INSERT INTO tournaments VALUES (?,?,?,?,?,?,?,?,?,?)", tournaments)
        conn.commit()
    finally:
        conn.close()
    return path


def _health(*findings: HealthFinding) -> LoopHealth:
    """Wrap findings in a ``LoopHealth`` shaped as the detectors emit it."""
    return LoopHealth(
        epoch_id="e0",
        findings=findings,
        healthy=not findings,
        checked_at="2026-07-31T00:00:00Z",
    )


@pytest.mark.xfail(
    strict=True,
    reason="#129 pattern A: _summarise_loop_health drops HealthFinding.detail — "
    "the operator-facing line carries none of the numbers that tripped the detector",
)
def test_loop_health_summary_carries_the_evidence_that_tripped_the_detector() -> None:
    """The rendered summary must name the numbers, not just the verdict.

    A ``dead_judge`` finding knows *which* judge is dead and *how many*
    rounds it stayed silent. Both live in ``detail``; neither reaches the
    operator. Without them the line is an assertion the operator cannot
    act on without going to the JSON — which is exactly the "last hop"
    #129 describes.
    """
    finding = HealthFinding(
        code="dead_judge",
        severity="critical",
        summary="1 declared judge produced no metric",
        detail={"dead_judges": ["safety"], "rounds_observed": 12, "metrics_seen": 0},
    )

    summary, has_critical = _summarise_loop_health(_health(finding))

    assert has_critical is True
    # The discriminating evidence: which judge, and over how many rounds.
    assert "safety" in summary, f"summary names no judge: {summary!r}"
    assert "12" in summary, f"summary carries no measurement: {summary!r}"


@pytest.mark.xfail(
    strict=True,
    reason="#129 pattern A: a finding whose detail dict is empty is indistinguishable "
    "from one whose evidence was dropped in rendering",
)
def test_every_finding_renders_at_least_one_measured_quantity() -> None:
    """Evidence-bearing rendering must be a property of the renderer, not of luck.

    Pinned as a conformance check over the renderer rather than over one
    detector: whatever a detector puts in ``detail``, the rendered line
    must reflect it. This is the generic form of the convention
    ``PracticeCheck`` already holds itself to.
    """
    finding = HealthFinding(
        code="degenerate_scoring",
        severity="critical",
        summary="scoring is degenerate",
        detail={"distinct_scalars": 1, "generations_compared": 8},
    )

    summary, _ = _summarise_loop_health(_health(finding))

    rendered = [str(v) for v in finding.detail.values() if str(v) in summary]
    assert rendered, (
        "no value from the finding's detail dict reached the operator-facing "
        f"summary; detail={finding.detail!r} summary={summary!r}"
    )


# ---------------------------------------------------------------------------
# Pattern B — surfaces that assume the champion advances
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="#129 pattern B: optimization_trajectory computes `plateaued` over the "
    "PROMOTED spine only, so a run where nothing promotes reports plateaued=False — "
    "the same value a healthily-improving run reports",
)
def test_stalled_run_is_distinguishable_from_an_improving_one(tmp_path: Path) -> None:
    """Six straight rejections must not read as "not plateaued".

    :func:`zicato.tournament.detail.optimization_trajectory` derives
    ``plateaued`` from :func:`_is_plateaued`, which walks only the
    *promoted* spine. With zero promotions the spine holds the seed
    alone, so fewer than ``PLATEAU_WINDOW`` scalars exist and the
    function short-circuits to ``False`` ("cannot plateau").

    The result is that the most stalled run possible — every challenger
    rejected, the champion retained throughout — reports the identical
    ``plateaued`` value as a run improving on every round. That is #129
    pattern B exactly: the surface degrades in the regime where the
    operator most needs it, and it degrades to a *reassuring* value
    rather than an absent one.
    """
    stalled = _index_db(
        tmp_path / "stalled.db",
        # v0 seed promoted; six challengers, none promoted.
        [(EPOCH, "v0", None, 1)] + [(EPOCH, f"v{i}", "v0", 0) for i in range(1, 7)],
        [
            (f"t{i}", EPOCH, "v0", f"v{i}", "rejected", 1.0, 1.0, 0.0, "below margin", "x")
            for i in range(1, 7)
        ],
    )
    improving = _index_db(
        tmp_path / "improving.db",
        [
            (EPOCH, "v0", None, 1),
            (EPOCH, "v1", "v0", 1),
            (EPOCH, "v2", "v1", 1),
            (EPOCH, "v3", "v2", 1),
        ],
        [
            ("t1", EPOCH, "v0", "v1", "promoted", 3.0, 2.5, -0.5, "", "x"),
            ("t2", EPOCH, "v1", "v2", "promoted", 2.5, 2.0, -0.5, "", "x"),
            ("t3", EPOCH, "v2", "v3", "promoted", 2.0, 1.5, -0.5, "", "x"),
        ],
    )

    stalled_traj = optimization_trajectory(stalled, EPOCH)
    improving_traj = optimization_trajectory(improving, EPOCH)

    # The counts DO record the stall — the evidence exists...
    assert stalled_traj.challenger_count == 6
    assert stalled_traj.promoted_count == 0

    # ...but the summary flag an operator reads collapses the two regimes.
    assert stalled_traj.plateaued != improving_traj.plateaued, (
        "a run with six rejections and zero promotions reports the same "
        f"plateaued={stalled_traj.plateaued!r} as a run improving every round"
    )
