"""Golden byte-identical proof for the issue-#19 phase-1 scoring extraction.

PHASE 1 is a pure refactor: the two scoring seams (per-run drift loss in the
reducer; per-generation scalar synthesis in the tournament) were lifted out
into :mod:`zicato.scoring` and routed through dispatchers, with NO behaviour
change. This module pins that the extracted built-ins + dispatchers produce
results BYTE-IDENTICAL to the historical inline formulas.

The reference implementations in this file (:func:`_ref_drift_loss`,
:func:`_ref_scalar`) are the LITERAL pre-refactor formulas, transcribed from
``telemetry/reducer.py::compute_drift_loss`` and the scalar composition in
``tournament/scoring.py::aggregate_generation_score`` as they stood before the
extraction. The corpus exercises every weighting axis the seams touch:

* per-kind weights, per-judge weights (``custom`` / ``custom:<name>``),
  the unattributed bare ``custom`` bucket, and the default-judge-weight
  fallback;
* severity weights (including a missing-severity → 0.0 fallback);
* ``plan_revisions``, ``task_failure_ratio``, and ``runtime`` terms;
* namespace aggregates (incl. the ``"drift:"`` exclusion + a negative
  rubric-style weight);
* the issue-#18 continuous ``mean_score`` path (scored boards AND the
  all-bool board where ``mean_score == pass_rate`` byte-for-byte);
* a ``pass`` curve with ``pass_weight != 1`` (a stand-in for the future
  ``pass_transform`` axis — PHASE 1 keeps the linear ``(1 - mean_score)``
  shape, so the reference here is exactly that linear term).

It also asserts the dispatchers return the ``"builtin"`` provenance and that
the live ``compute_drift_loss`` / ``aggregate_generation_score`` paths match.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

from zicato.core import DriftCount, LossProfile, ScoringWeights
from zicato.scoring import (
    PROVENANCE_BUILTIN,
    DriftContext,
    ScalarContext,
    builtin_drift_loss,
    builtin_scalar,
    resolve_drift_loss,
    resolve_scalar,
)
from zicato.scoring.builtins import _kind_multiplier as _builtin_kind_mult
from zicato.telemetry.reducer import compute_drift_loss, read_loss_profile, write_loss_profile
from zicato.tournament.scoring import (
    aggregate_generation_score,
    aggregate_namespaced_metrics,
)

# ---------------------------------------------------------------------------
# Reference implementations — the LITERAL pre-refactor formulas.
# ---------------------------------------------------------------------------

_TASK_FAILURE_RATIO_MULTIPLIER = 10.0


def _ref_kind_multiplier(kind: str, weights: ScoringWeights) -> float:
    """Pre-refactor ``_kind_multiplier`` from reducer.py (transcribed)."""
    if kind == "custom":
        is_custom, judge_name = True, ""
    elif kind.startswith("custom:"):
        is_custom, judge_name = True, kind[len("custom:") :]
    else:
        is_custom, judge_name = False, ""
    if is_custom:
        return weights.per_judge_weights.get(judge_name, weights.default_judge_weight)
    return weights.per_kind_weights.get(kind, 1.0)


def _ref_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    task_failure_ratio: float,
    runtime_ms: int,
    weights: ScoringWeights,
) -> float:
    """Pre-refactor ``compute_drift_loss`` body (transcribed verbatim)."""
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _ref_kind_multiplier(c.kind, weights)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * task_failure_ratio
    loss += weights.runtime_weight * (runtime_ms / 1000.0)
    return max(0.0, float(loss))


def _ref_scalar(
    mean_score: float,
    drift_loss_mean: float,
    namespace_aggregates: dict[str, float],
    weights: ScoringWeights,
) -> float:
    """Pre-refactor scalar composition from aggregate_generation_score."""
    drift_component = weights.drift_weight * drift_loss_mean
    pass_component = weights.pass_weight * (1.0 - mean_score)
    components: dict[str, float] = {"drift": drift_component, "pass": pass_component}
    for ns, value in namespace_aggregates.items():
        if ns == "drift:":
            continue
        name = ns[:-1] if ns.endswith(":") else ns
        components[name] = value
    return sum(components.values())


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

# Weight presets exercising every multiplier axis.
_W_DEFAULT = ScoringWeights()
_W_KINDS = ScoringWeights(
    per_kind_weights={"off_topic": 2.0, "tool_error": 0.5},
    plan_revision_weight=0.5,
    runtime_weight=0.01,
)
_W_JUDGES = ScoringWeights(
    per_judge_weights={"precision": 3.0, "structure": 0.25},
    default_judge_weight=1.5,
)
_W_MISSING_SEV = ScoringWeights(
    # Drop "critical" → its severity multiplier falls back to 0.0.
    severity_weights={"info": 1.0, "warning": 2.0},
    per_kind_weights={"off_topic": 4.0},
)

_DRIFT_CORPUS: list[tuple[str, tuple[DriftCount, ...], int, float, int, ScoringWeights]] = [
    ("empty", (), 0, 0.0, 0, _W_DEFAULT),
    (
        "first_class_kinds",
        (
            DriftCount(kind="off_topic", severity="warning", count=2),
            DriftCount(kind="tool_error", severity="critical", count=1),
        ),
        3,
        0.4,
        12_345,
        _W_KINDS,
    ),
    (
        "custom_judges",
        (
            DriftCount(kind="custom:precision", severity="warning", count=2),
            DriftCount(kind="custom:structure", severity="info", count=5),
            DriftCount(kind="custom", severity="critical", count=1),  # bare/unattributed
            DriftCount(kind="custom:unlisted", severity="warning", count=3),  # default weight
        ),
        0,
        0.0,
        2_000,
        _W_JUDGES,
    ),
    (
        "missing_severity_fallback",
        (
            DriftCount(kind="off_topic", severity="critical", count=4),  # sev → 0.0
            DriftCount(kind="off_topic", severity="warning", count=2),
        ),
        1,
        1.0,
        999,
        _W_MISSING_SEV,
    ),
    (
        "task_failure_and_runtime",
        (DriftCount(kind="schema_violation", severity="info", count=1),),
        7,
        0.75,
        60_000,
        ScoringWeights(plan_revision_weight=0.3, runtime_weight=0.05),
    ),
]


def test_builtin_drift_loss_byte_identical_to_reference() -> None:
    for name, dc, pr, tfr, rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, tfr, rt, w)
        got = builtin_drift_loss(
            drift_counts=dc,
            plan_revisions=pr,
            task_failure_ratio=tfr,
            runtime_ms=rt,
            weights=w,
        )
        # Byte-identical: bit-for-bit equal floats (no tolerance).
        assert got == expected, f"{name}: builtin {got!r} != ref {expected!r}"
        assert got.hex() == expected.hex(), name


def test_resolve_drift_loss_dispatches_builtin_with_provenance() -> None:
    for name, dc, pr, tfr, rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, tfr, rt, w)
        ctx = DriftContext(
            drift_counts=dc,
            plan_revisions=pr,
            task_failure_ratio=tfr,
            runtime_ms=rt,
            weights=w,
            builtin_loss=builtin_drift_loss(
                drift_counts=dc,
                plan_revisions=pr,
                task_failure_ratio=tfr,
                runtime_ms=rt,
                weights=w,
            ),
        )
        loss, prov = resolve_drift_loss(ctx)
        assert loss == expected, name
        assert prov == PROVENANCE_BUILTIN == "builtin", name


def test_live_compute_drift_loss_matches_reference() -> None:
    """The live reducer entry point still equals the pre-refactor formula."""
    for name, dc, pr, tfr, rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, tfr, rt, w)
        got = compute_drift_loss(
            drift_counts=dc,
            plan_revisions=pr,
            task_failure_ratio=tfr,
            runtime_ms=rt,
            weights=w,
        )
        assert got == expected, name


def test_builtin_kind_multiplier_matches_reference() -> None:
    cases = ["off_topic", "tool_error", "custom", "custom:precision", "custom:unlisted", "unknown"]
    for w in (_W_DEFAULT, _W_KINDS, _W_JUDGES, _W_MISSING_SEV):
        for kind in cases:
            assert _builtin_kind_mult(kind, w) == _ref_kind_multiplier(kind, w), (kind, w)


# ---------------------------------------------------------------------------
# Seam 2 — scalar
# ---------------------------------------------------------------------------


def _loss(
    entry_id: str,
    *,
    drift_loss: float,
    pass_fail: bool | None = None,
    score: float | None = None,
    metric_counts: tuple = (),
) -> LossProfile:
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v0",
        epoch_id="e0",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        score=score,
        metric_counts=metric_counts,
    )


# Scalar corpus: (name, mean_score, drift_loss_mean, namespace_aggregates, weights)
_SCALAR_CORPUS = [
    ("default_perfect", 1.0, 0.0, {}, _W_DEFAULT),
    ("default_mixed", 0.5, 2.0, {"drift:": 2.0}, _W_DEFAULT),
    ("pass_weight_4", 0.25, 1.0, {}, ScoringWeights(drift_weight=1.0, pass_weight=4.0)),
    ("pass_weight_0", 0.0, 3.0, {}, ScoringWeights(drift_weight=1.0, pass_weight=0.0)),
    (
        "namespaces_pos_and_neg",
        0.6,
        1.5,
        {"drift:": 1.5, "cost:": 0.8, "rubric:": -0.4, "output:": 0.0},
        ScoringWeights(
            namespace_weights={"drift:": 1.0, "cost:": 1.0, "rubric:": -1.0, "output:": 0.0},
        ),
    ),
    (
        "continuous_score",
        0.834,  # a graded mean_score, not collapsible to a pass_rate
        0.9,
        {"drift:": 0.9},
        ScoringWeights(drift_weight=2.0, pass_weight=1.5),
    ),
]


def test_builtin_scalar_byte_identical_to_reference() -> None:
    for name, ms, dlm, ns, w in _SCALAR_CORPUS:
        expected = _ref_scalar(ms, dlm, ns, w)
        got = builtin_scalar(mean_score=ms, drift_loss_mean=dlm, namespace_aggregates=ns, weights=w)
        assert got == expected, f"{name}: builtin {got!r} != ref {expected!r}"
        assert got.hex() == expected.hex(), name


def test_resolve_scalar_dispatches_builtin_with_provenance() -> None:
    for name, ms, dlm, ns, w in _SCALAR_CORPUS:
        expected = _ref_scalar(ms, dlm, ns, w)
        ctx = ScalarContext(
            pass_rate=ms,
            mean_score=ms,
            drift_loss_mean=dlm,
            namespace_aggregates=ns,
            per_judge_loss={},
            weights=w,
            builtin_scalar=builtin_scalar(
                mean_score=ms, drift_loss_mean=dlm, namespace_aggregates=ns, weights=w
            ),
        )
        scalar, prov = resolve_scalar(ctx)
        assert scalar == expected, name
        assert prov == PROVENANCE_BUILTIN == "builtin", name


def test_live_aggregate_scalar_matches_reference_scored_board() -> None:
    """End-to-end through aggregate_generation_score on a CONTINUOUS board.

    Exercises the #18 mean_score path with float scores so mean_score does
    NOT collapse to pass_rate, plus a non-drift namespace metric, and proves
    the live scalar equals the reference recomputed from the same aggregates.
    """
    weights = ScoringWeights(drift_weight=1.0, pass_weight=2.0)
    # Scores chosen so the graded mean (0.55) does NOT equal the binary
    # pass_rate (2/3) — proving the scalar runs on mean_score, not pass_rate.
    losses = [
        _loss("a", drift_loss=1.0, pass_fail=True, score=0.85),
        _loss("b", drift_loss=3.0, pass_fail=False, score=0.1),
        _loss("c", drift_loss=2.0, pass_fail=True, score=0.7),
    ]
    agg = aggregate_generation_score(losses, weights)
    ns_agg = aggregate_namespaced_metrics(losses, weights)
    expected = _ref_scalar(agg["mean_score"], agg["drift_loss_mean"], dict(ns_agg), weights)
    assert agg["scalar"] == expected
    assert agg["scalar_provenance"] == "builtin"
    # mean_score is a genuine graded mean here (not a binary pass_rate).
    assert agg["mean_score"] != agg["pass_rate"]
    assert math.isclose(agg["mean_score"], (0.85 + 0.1 + 0.7) / 3)


def test_live_aggregate_scalar_all_bool_board_equals_reference() -> None:
    """All-bool board: mean_score == pass_rate byte-for-byte, scalar matches."""
    weights = ScoringWeights()
    losses = [
        _loss("a", drift_loss=0.0, pass_fail=True),
        _loss("b", drift_loss=2.0, pass_fail=False),
    ]
    agg = aggregate_generation_score(losses, weights)
    assert agg["mean_score"] == agg["pass_rate"]
    ns_agg = aggregate_namespaced_metrics(losses, weights)
    expected = _ref_scalar(agg["mean_score"], agg["drift_loss_mean"], dict(ns_agg), weights)
    assert agg["scalar"] == expected
    assert agg["scalar_provenance"] == "builtin"


# ---------------------------------------------------------------------------
# Provenance scaffold — round-trip + back-compat
# ---------------------------------------------------------------------------


def test_loss_profile_scoring_provenance_round_trips(tmp_path: Path) -> None:
    """scoring_provenance survives write/read unchanged."""
    base = _loss("p", drift_loss=1.0, pass_fail=True)
    profile = dataclasses.replace(base, scoring_provenance="builtin")
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    loaded = read_loss_profile(p)
    assert loaded.scoring_provenance == "builtin"
    assert loaded == profile


def test_loss_profile_without_provenance_field_reads_as_none(tmp_path: Path) -> None:
    """A loss.json written before scoring_provenance existed reads cleanly."""
    profile = _loss("old", drift_loss=2.0, pass_fail=False)
    p = tmp_path / "loss.json"
    write_loss_profile(profile, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("scoring_provenance", None)  # simulate a pre-feature file
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = read_loss_profile(p)
    assert loaded.scoring_provenance is None


def test_reduce_loss_stamps_builtin_provenance(tmp_path: Path) -> None:
    """The live reducer stamps a 'builtin' provenance on the profile it builds."""
    from zicato.core import BoardEntry, ExpectationResult
    from zicato.telemetry.reducer import reduce_loss

    entry = BoardEntry(id="e-prov", kind="single_turn", wall_clock_budget_seconds=60, input="hi")
    events = tmp_path / "events.jsonl"  # absent file → empty events, still valid
    profile = reduce_loss(
        events,
        entry,
        "v0",
        "ep0",
        ExpectationResult(kind="predicate", passed=True),
        1000,
        False,
        ScoringWeights(),
    )
    assert profile.scoring_provenance == "builtin"
