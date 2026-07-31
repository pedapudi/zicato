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

But that precedent has the same defect one layer up, which is why the
fix has to be a **render conformance rule** rather than another
well-shaped dataclass field:
:func:`zicato.cli.commands.reflect._render_practice_section` never reads
``evidence`` at all, so the numbers behind all eleven practice checks are
invisible to operators today. Both layers of this codebase collect good
structured evidence and then drop it at the last hop; adding a third
evidence field would reproduce the bug rather than fix it.

**B. Surfaces that assume the champion advances.** ``PracticeCheck`` also
models the fix for pattern B: an explicit ``VERDICT_UNMEASURED`` plus an
``unmeasured_reason`` naming the missing input — e.g.
``check_promotion_hygiene`` answers "No promotions under this contract
yet — promotion hygiene has nothing to audit" rather than reporting a
vacuous pass. Surfaces that degrade silently when the champion is
retained lack that third state: they cannot distinguish "measured, and
it is fine" from "there was nothing to measure".

Pattern B is not uniform, and the exceptions cut both ways. Several
report sections DO degrade honestly (the "_No promoted lineage long
enough..._" notices), and ``build_round_timeline`` models retention
correctly with explicit ``held`` steps. But the report's headline
callout does not: it publishes the last REJECTED challenger's
counterfactual under a label naming the promoted lineage, so a
zero-promotion epoch can be headlined as having *improved*. Reading a
few honest degradations is not evidence that a surface family is
sound — each of these was written independently, and each invented its
own behaviour for a regime nothing centrally records.

These are ``xfail(strict=True)`` triage pins: they fail today, and the
marker comes off with the fix.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from zicato.analyzer.report_data import EpochReportData, GenerationView, _cumulate_scalar
from zicato.analyzer.report_sections import _render_campaign_callout, render_score_sparkline
from zicato.cli.commands.reflect import _render_practice_section
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


def test_loop_health_summary_carries_the_detector_s_recommendation() -> None:
    """The one hop that turns a detection into an action.

    Fifteen of the nineteen detectors write an explicit
    ``detail["recommendation"]`` — a sentence saying what to change. It
    reached the per-round health JSON and stopped there: the generic
    terminal renderer read only string attributes, so the dict holding
    the remediation was skipped.

    This is #129's "last hop" in its most literal form. The harness had
    already done the work of deciding what the operator should do, and
    then did not say it. The renderer now names the finding's stable
    ``code`` and appends the remediation, clipped so the line stays one
    line.
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

    FIXED as adjudicated: ``plateaued`` stays a property of the promoted
    spine — redefining it to fold in rejections would make a flag named
    for a scalar window mean something else, and every reader of it
    would have to be re-checked. What was missing is the companion fact
    saying whether the flag rests on a measurement at all, so
    ``plateau_measurable`` was added beside it and the assertion below
    reads the pair. The original single-field assertion is kept as a
    comment: it is now *expected* to be equal, and that equality is
    exactly why the second field has to exist.
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

    # ...and the raw flag still collapses the two regimes, by design:
    #   assert stalled_traj.plateaued == improving_traj.plateaued == False
    # so the flag must not be read alone. The PAIR separates them: the
    # stalled run's one-node spine cannot support a plateau judgement,
    # and now says so.
    assert stalled_traj.plateau_measurable is False
    assert improving_traj.plateau_measurable is True
    assert (stalled_traj.plateaued, stalled_traj.plateau_measurable) != (
        improving_traj.plateaued,
        improving_traj.plateau_measurable,
    ), (
        "a run with six rejections and zero promotions reports the same "
        f"plateaued={stalled_traj.plateaued!r} as a run improving every round"
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

    FIXED: the ``floor is not None`` conjunct is gone. The floor now
    chooses WHICH honest word applies rather than whether one applies —
    ``"no_signal"`` with a measured floor (every challenger tied inside
    the A/A spread is a claim about noise, and needs a measurement to
    back it), ``"stalled"`` without one (a report of promotions that did
    not happen, which claims nothing about noise). The assertion below
    pins the exact word rather than merely ``!= "improving"``, so a
    future fallthrough to some third reassuring word fails here too.
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
    assert view["verdict"] == "stalled", (
        "six challengers, zero promotions, no measured floor — the dashboard "
        f"calls this {view['verdict']!r}"
    )


# ---------------------------------------------------------------------------
# Pattern B — the published report
# ---------------------------------------------------------------------------


def _generation(
    gid: str, parent: str, *, baseline: bool = False, decision: str = "rejected", delta: float = 0.0
) -> GenerationView:
    """One generation view carrying only the fields these pins read."""
    return GenerationView(
        generation_id=gid,
        parent_generation_id=parent,
        is_baseline=baseline,
        proposed_at="",
        core_idea="idea",
        why="why",
        risks="",
        modulating=(),
        expected_pass_rate_delta="",
        expected_drift_movements=(),
        decision=decision,
        rejection_reason="below promote_margin",
        scalar_score_delta=delta,
        drift_loss_delta=0.0,
        pass_rate_delta=0.0,
        drift_movements=(),
        metric_movements=(),
        patches=(),
    )


def _report_data(generations: list[GenerationView]) -> EpochReportData:
    """Build an ``EpochReportData`` defaulting every field these pins ignore."""
    cumulated = _cumulate_scalar(generations)
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(EpochReportData):
        if f.name == "generations":
            kwargs[f.name] = tuple(cumulated)
            continue
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        annotation = str(f.type)
        if "tuple" in annotation:
            kwargs[f.name] = ()
        elif "dict" in annotation:
            kwargs[f.name] = {}
        elif "bool" in annotation:
            kwargs[f.name] = False
        else:
            kwargs[f.name] = ""
    return EpochReportData(**kwargs)  # type: ignore[arg-type]


def _stalled_epoch(rounds: int = 20, delta: float = -0.043) -> EpochReportData:
    """A baseline plus ``rounds`` rejected challengers — nothing promoted."""
    return _report_data(
        [_generation("v0", "", baseline=True, decision="baseline")]
        + [_generation(f"v{i}", "v0", delta=delta) for i in range(1, rounds + 1)]
    )


def test_campaign_callout_does_not_credit_a_rejected_challenger_to_the_lineage() -> None:
    """The published headline must not contradict itself.

    ``EpochReportData.final_scalar`` returns
    ``generations[-1].cumulative_scalar``, and ``_cumulate_scalar`` fills
    a cumulative for EVERY generation regardless of decision. With zero
    promotions the newest generation is a rejected one, so the callout
    reports a score that was measured and then discarded — under a label
    naming the promoted lineage.

    When that discarded challenger happened to improve on the champion
    but failed the margin, the direction word compounds the error and the
    sentence contradicts itself outright: "has improved to `-0.043` ...
    (0 promoted, 20 rejected)". The honest rendering of a lineage that
    never advanced is "held to `+0.000`".

    The same number is handed to the prose LLM as ground truth via
    ``report_prompts.py``'s ``final_cumulative_scalar``, in a digest whose
    docstring calls itself "the factual substrate ... so the model never
    has a reason to compute or guess a value".

    FIXED: ``final_scalar`` is champion-anchored (last promoted
    generation, else the baseline). The counterfactual keeps its place
    in the callout and in the LLM digest, but under
    ``latest_rejected_scalar`` — a name that says whose number it is.
    """
    callout = _render_campaign_callout(_stalled_epoch())

    assert "0 promoted, 20 rejected" in callout
    assert (
        "improved" not in callout
    ), f"a lineage that never advanced is headlined as improving: {callout!r}"
    assert (
        "held to `+0.000`" in callout
    ), f"the callout credits a discarded challenger to the promoted lineage: {callout!r}"
    # The discarded number survives — labelled as the path not taken.
    assert "-0.043" in callout and "a path not taken" in callout, callout


def test_trajectory_chart_marks_the_champion_as_current_not_the_last_attempt() -> None:
    """``<- current`` must point at the generation actually in force.

    ``render_score_sparkline`` pins the marker to ``last_idx =
    len(gens) - 1``. With zero promotions that index holds a rejected
    challenger whose patches were thrown away, while the generation
    genuinely in force is the baseline at the top of the chart. The
    operator reads the chart bottom-up and takes the discarded attempt
    for the state of the system.

    FIXED: the marker follows the champion row — the last promoted
    generation, or the baseline when nothing has promoted.
    """
    data = _stalled_epoch(rounds=3)
    chart = render_score_sparkline(data)

    current_lines = [ln for ln in chart.splitlines() if "<- current" in ln]
    assert len(current_lines) == 1, chart
    assert (
        "rejected" not in current_lines[0]
    ), f"'<- current' marks a discarded rejected challenger: {current_lines[0]!r}"
    assert "baseline" in current_lines[0], current_lines[0]


# ---------------------------------------------------------------------------
# Pattern A — the precedent's own renderer
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="#129 pattern A: _render_practice_section never reads PracticeCheck.evidence, "
    "so the structured evidence behind every practice verdict is invisible to operators",
)
def test_practice_review_renders_the_evidence_behind_its_verdict() -> None:
    """The convention this issue wants to spread has the same defect.

    ``PracticeCheck`` is zicato's best-shaped diagnostic contract —
    ``evidence`` dict, ``rationale``, and an explicit ``unmeasured``
    verdict. But its renderer emits ``check_id`` / ``headline`` /
    ``rationale`` / ``unmeasured_reason`` / ``proposed_op`` and never
    touches ``evidence``, so the numbers behind all eleven checks never
    reach the report.

    This is why the fix for pattern A has to be a RENDER conformance
    rule: adding another well-shaped evidence field to a dataclass
    reproduces the bug rather than fixing it.
    """
    check = {
        "check_id": "promotion_hygiene",
        "verdict": "unsound",
        "headline": "promotions are clearing a margin that sits below the measured floor",
        "evidence": {
            "promotions": 4,
            "promote_margin": 0.01,
            "margin_below_floor": True,
            "recommended_promote_margin": 0.075,
        },
        "rationale": "a promotion on a sub-floor margin promotes noise",
        "proposed_op": None,
        "unmeasured_reason": None,
    }

    rendered = "\n".join(_render_practice_section([check]))

    assert "0.075" in rendered, (
        "the recommended margin the check computed never reaches the report; "
        f"rendered={rendered!r}"
    )
