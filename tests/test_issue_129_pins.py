"""Triage pins for the two cross-cutting patterns in issue #129.

Issue #129 generalises eleven reports (#118-#128) into two claims:

**A. Detection without explanation.** zicato detects that something is
wrong and fails to say what. Triage narrowed this claim considerably,
and the narrowing matters: at the detector layer it does not hold. All
19 :class:`~zicato.health.diagnostics.HealthFinding` construction sites
populate the documented ``detail`` dict, every one of their ``summary``
strings interpolates the measured quantities, and 15 of the 19 carry an
explicit ``detail["recommendation"]`` telling the operator what to do.
The collection layer is in good shape.

The gap is the **render** hop, and specifically the loss of the
recommendation. :func:`zicato.orchestrator._summarise_loop_health` builds
the operator-facing one-liner from a ``_text`` helper that accepts only
*string* attributes (``message`` / ``summary`` / ``detail`` /
``description``) and returns the first non-empty one. ``detail`` is a
**dict**, so an ``isinstance(val, str)`` guard skips it: the
remediation the detector already wrote is structurally unreachable from
the line the operator reads. The same renderer also shows only
``findings[0]``, collapsing every other finding to a ``(+N more)``
count.

The evidence does survive to the per-round health JSON (via
:func:`zicato.orchestrator._loop_health_to_json`, which uses
``dataclasses.asdict``), and a handful of findings get bespoke
terminal warnings (``_warn_dead_judges`` and siblings) that do inline
their detail. So this is a surfacing gap on the generic path, not a
collection gap — which is why the fix is cheap.

The bar is not invented here. zicato's sibling diagnostic contract,
:class:`zicato.reflection.practices.PracticeCheck`, already specifies
it: ``headline`` is documented as "a single sentence **with the numbers
inline**", carried alongside a structured ``evidence`` dict and a
``rationale``. The health path collects the same material and then drops
the actionable half on the way out.

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

import json
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
    reason="#129 pattern A: _summarise_loop_health drops detail['recommendation'] — "
    "the remediation the detector already wrote never reaches the operator",
)
def test_loop_health_summary_carries_the_detector_s_recommendation() -> None:
    """The one hop that turns a detection into an action.

    Fifteen of the nineteen detectors write an explicit
    ``detail["recommendation"]`` — a sentence saying what to change. It
    reaches the per-round health JSON and stops there: the generic
    terminal renderer reads only string attributes, so the dict holding
    the remediation is skipped.

    This is #129's "last hop" in its most literal form. The harness has
    already done the work of deciding what the operator should do, and
    then does not say it.
    """
    finding = HealthFinding(
        code="margin_below_noise_floor",
        severity="critical",
        summary="promote_margin 0.01 sits below the measured noise floor 0.04",
        detail={
            "promote_margin": 0.01,
            "noise_floor_max_abs_delta": 0.04,
            "recommendation": (
                "raise promote_margin clear of the floor, or enable the evidence gate"
            ),
        },
    )

    summary, has_critical = _summarise_loop_health(_health(finding))

    assert has_critical is True
    assert "raise promote_margin" in summary, (
        "the detector wrote a remediation and the renderer dropped it; " f"summary={summary!r}"
    )


# NOT PINNED (deliberate): ``_summarise_loop_health`` renders only
# ``findings[0]`` and collapses the rest to ``(+N more critical)``, so a
# round tripping three detectors reports one of them. That is a real
# operator cost, but the function is documented as deriving a ONE-LINE
# summary and a pin demanding it render every finding would contradict
# its stated contract rather than expose a defect in it. The fix belongs
# at the caller (``_warn_loop_no_signal`` emitting one warning per
# critical finding), so it is registered as a pattern-level item rather
# than pinned here against the wrong function.


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


@pytest.mark.xfail(
    strict=True,
    reason="#129 pattern B: the dashboard trajectory verdict reports 'improving' for a "
    "run with zero promotions when no noise floor was measured — issue #84's "
    "stuck_no_promotions guard is gated on `floor is not None`",
)
def test_zero_promotion_run_without_a_measured_floor_is_not_reported_as_improving(
    tmp_path: Path,
) -> None:
    """The worst pattern-B instance: a stalled loop rendered as healthy.

    :func:`zicato.query.loop_view.build_optimization_trajectory` computes
    the word the dashboard shows. It already knows the stalled case —
    ``stuck_no_promotions`` was added for issue #84 — but the branch
    reads ``if floor is not None and stuck_no_promotions``. Noise-floor
    calibration is opt-in, so in a workspace that never ran it the guard
    is skipped entirely and control falls through to
    ``elif not traj.plateaued: verdict = "improving"``.

    The raw ``plateaued`` flag is ``False`` here only because the
    promoted spine is too SHORT to plateau (the defect pinned above), so
    the fallthrough converts "we have no measurement" into the single
    most reassuring word the UI can print. An operator watching six
    consecutive rejections is told the loop is improving.

    The floor is the wrong gate: without one the honest verdict is
    unmeasurable, never ``"improving"``. Fixing the gate means letting
    ``stuck_no_promotions`` suppress ``"improving"`` on its own.
    """
    from zicato.query import WorkspacePaths, build_optimization_trajectory  # noqa: PLC0415

    # A workspace whose epoch config carries NO measured noise floor.
    ws = tmp_path / ".zicato"
    epoch_dir = ws / "epochs" / EPOCH
    epoch_dir.mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    (epoch_dir / "config.json").write_text(
        json.dumps({"contract_hash": "h1", "closed": False}), encoding="utf-8"
    )
    _index_db(
        ws / "index.db",
        # v0 seed (unpromoted, as the seed row is written) + six rejections.
        [(EPOCH, "v0", None, 0)] + [(EPOCH, f"v{i}", "v0", 0) for i in range(1, 7)],
        [
            (f"t{i}", EPOCH, "v0", f"v{i}", "rejected", 3.6, 3.6, 0.0, "below margin", "x")
            for i in range(1, 7)
        ],
    )

    view = build_optimization_trajectory(WorkspacePaths(ws), EPOCH)

    assert view["challenger_count"] == 6
    assert view["promoted_count"] == 0
    assert view["noise_floor"] is None
    assert view["verdict"] != "improving", (
        "six challengers, zero promotions, no measured floor — the dashboard "
        f"still calls this {view['verdict']!r}"
    )
