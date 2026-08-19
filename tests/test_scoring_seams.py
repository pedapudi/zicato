"""A deliberately independent second implementation of both scoring seams.

The two seams — per-run drift loss (Seam 1, reducer) and per-generation scalar
synthesis (Seam 2, tournament) — are computed here a SECOND time, from the
specification rather than by calling the shipped code, and the two are pinned
against each other bit-for-bit across a corpus that exercises every weighting
axis. A change to :mod:`zicato.scoring.builtins` that is not mirrored in
:func:`_ref_drift_loss` / :func:`_ref_scalar` reds this module by design: the
point is that the formula has to be written twice before it moves.

The corpus covers:

* per-kind weights and severity weights (including a missing-severity → 0.0
  fallback), and ``plan_revisions``;
* judge-attributed drift (``custom`` / ``custom:<name>``, the unattributed
  bare bucket, and the default-judge-weight fallback), which the drift channel
  EXCLUDES and the ``judge:`` channel scores;
* the ``failure:`` channel on a clean run and on an aborted one;
* namespace aggregates, including a negative rubric-style weight and the
  sorted-sum determinism the composition depends on;
* the continuous ``mean_score`` path (scored boards AND the all-bool board
  where ``mean_score == pass_rate`` byte-for-byte);
* a ``pass`` curve with ``pass_weight != 1``.

It also asserts the dispatchers return the ``"builtin"`` provenance and that
the live ``compute_drift_loss`` / ``aggregate_generation_score`` paths match.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

from zicato.core import DriftCount, JudgeLoss, LossProfile, ScoringWeights
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


def _ref_is_judge_attributed(kind: str) -> bool:
    """Independently: is this a custom-judge drift kind?"""
    return kind == "custom" or kind.startswith("custom:")


def _ref_kind_multiplier(kind: str, weights: ScoringWeights) -> float:
    """Independently: the per-kind multiplier for a first-class drift kind."""
    return weights.per_kind_weights.get(kind, 1.0)


def _ref_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    weights: ScoringWeights,
) -> float:
    """Independently: the ``drift:`` channel's per-run term (Seam 1)."""
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        if _ref_is_judge_attributed(c.kind):
            continue
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _ref_kind_multiplier(c.kind, weights)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    return max(0.0, float(loss))


def _ref_judge_channel(
    drift_counts: tuple[DriftCount, ...],
    weights: ScoringWeights,
) -> dict[str, float]:
    """Independently: ``{judge_name: weighted_loss}`` for the judge channel."""
    raw: dict[str, float] = {}
    for c in drift_counts:
        if not _ref_is_judge_attributed(c.kind):
            continue
        name = c.kind[len("custom:") :] if c.kind.startswith("custom:") else ""
        raw[name] = raw.get(name, 0.0) + weights.severity_weights.get(c.severity, 0.0) * c.count
    return {
        name: value * weights.per_judge_weights.get(name, weights.default_judge_weight)
        for name, value in raw.items()
    }


def _ref_failure_channel(
    task_failure_ratio: float,
    not_completed: bool,
    weights: ScoringWeights,
) -> float:
    """Independently: the ``failure:`` channel's per-run total."""
    total = weights.task_failure_weight * task_failure_ratio
    if not_completed:
        total += weights.not_completed_weight
    return total


def _ref_scalar(
    mean_score: float,
    namespace_aggregates: dict[str, float],
    weights: ScoringWeights,
) -> float:
    """Independently: the scalar composition (Seam 2).

    One bounded pass/miss term, plus every already-weighted channel in SORTED
    namespace order.
    """
    components: dict[str, float] = {"pass": weights.pass_weight * (1.0 - mean_score)}
    for ns in sorted(namespace_aggregates):
        name = ns[:-1] if ns.endswith(":") else ns
        components[name] = namespace_aggregates[ns]
    return sum(components.values())


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

# Weight presets exercising every multiplier axis.
_W_DEFAULT = ScoringWeights()
_W_KINDS = ScoringWeights(
    per_kind_weights={"off_topic": 2.0, "tool_error": 0.5},
    plan_revision_weight=0.5,
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
        ScoringWeights(plan_revision_weight=0.3),
    ),
    (
        # Issue #19 phase-2 migration pin: the builtin no longer carries the
        # unconditional harmonic ``looping_reasoning`` special-case it once
        # did. A looping_reasoning count scores PURELY LINEARLY in the builtin
        # (severity × kind_weight × count), exactly like any other kind;
        # harmonic is now opt-in via ``drift_kind_aggregation`` and is proven
        # to reproduce the OLD value in ``test_scoring_transforms.py``. The
        # reference here is the linear formula, so this case fails if anyone
        # re-introduces a harmonic special-case into the builtin.
        "looping_reasoning_is_linear_in_builtin",
        (
            DriftCount(kind="looping_reasoning", severity="warning", count=5),
            DriftCount(kind="looping_reasoning", severity="info", count=3),
        ),
        0,
        0.0,
        0,
        ScoringWeights(per_kind_weights={"looping_reasoning": 1.5}),
    ),
]


def test_builtin_drift_loss_byte_identical_to_reference() -> None:
    for name, dc, pr, _tfr, _rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, w)
        got = builtin_drift_loss(drift_counts=dc, plan_revisions=pr, weights=w)
        # Byte-identical: bit-for-bit equal floats (no tolerance).
        assert got == expected, f"{name}: builtin {got!r} != ref {expected!r}"
        assert got.hex() == expected.hex(), name


def test_resolve_drift_loss_dispatches_builtin_with_provenance() -> None:
    for name, dc, pr, tfr, rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, w)
        ctx = DriftContext(
            drift_counts=dc,
            plan_revisions=pr,
            task_failure_ratio=tfr,
            runtime_ms=rt,
            weights=w,
            builtin_loss=builtin_drift_loss(
                drift_counts=dc,
                plan_revisions=pr,
                weights=w,
            ),
        )
        loss, prov = resolve_drift_loss(ctx)
        assert loss == expected, name
        assert prov == PROVENANCE_BUILTIN == "builtin", name


def test_live_compute_drift_loss_matches_reference() -> None:
    """The live reducer entry point still equals the pre-refactor formula."""
    for name, dc, pr, tfr, rt, w in _DRIFT_CORPUS:
        expected = _ref_drift_loss(dc, pr, w)
        got = compute_drift_loss(
            drift_counts=dc,
            plan_revisions=pr,
            weights=w,
            task_failure_ratio=tfr,
            runtime_ms=rt,
        )
        assert got == expected, name


def test_builtin_kind_multiplier_matches_reference() -> None:
    cases = ["off_topic", "tool_error", "unknown"]
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
    ("pass_weight_4", 0.25, 1.0, {}, ScoringWeights(pass_weight=4.0)),
    ("pass_weight_0", 0.0, 3.0, {}, ScoringWeights(pass_weight=0.0)),
    (
        "every_channel",
        0.5,
        1.5,
        {
            "drift:": 1.5,
            "judge:": 4.0,
            "failure:": 60.0,
            "runtime:": 0.0,
            "cost:": 0.8,
            "rubric:": -0.4,
            "output:": 0.0,
        },
        _W_DEFAULT,
    ),
    (
        "namespaces_pos_and_neg",
        0.6,
        1.5,
        {"drift:": 1.5, "cost:": 0.8, "rubric:": -0.4, "output:": 0.0},
        ScoringWeights(
            namespace_weights={
                "drift:": 1.0,
                "failure:": 1.0,
                "cost:": 1.0,
                "rubric:": -1.0,
                "output:": 0.0,
            },
        ),
    ),
    (
        "continuous_score",
        0.834,  # a graded mean_score, not collapsible to a pass_rate
        0.9,
        {"drift:": 1.8},
        ScoringWeights(namespace_weights={"drift:": 2.0, "failure:": 1.0}, pass_weight=1.5),
    ),
]


def test_builtin_scalar_byte_identical_to_reference() -> None:
    for name, ms, _dlm, ns, w in _SCALAR_CORPUS:
        expected = _ref_scalar(ms, ns, w)
        got = builtin_scalar(mean_score=ms, namespace_aggregates=ns, weights=w)
        assert got == expected, f"{name}: builtin {got!r} != ref {expected!r}"
        assert got.hex() == expected.hex(), name


def test_resolve_scalar_dispatches_builtin_with_provenance() -> None:
    for name, ms, dlm, ns, w in _SCALAR_CORPUS:
        expected = _ref_scalar(ms, ns, w)
        ctx = ScalarContext(
            pass_rate=ms,
            mean_score=ms,
            drift_loss_mean=dlm,
            namespace_aggregates=ns,
            per_judge_loss={},
            weights=w,
            builtin_scalar=builtin_scalar(mean_score=ms, namespace_aggregates=ns, weights=w),
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
    weights = ScoringWeights(pass_weight=2.0)
    # Scores chosen so the graded mean (0.55) does NOT equal the binary
    # pass_rate (2/3) — proving the scalar runs on mean_score, not pass_rate.
    losses = [
        _loss("a", drift_loss=1.0, pass_fail=True, score=0.85),
        _loss("b", drift_loss=3.0, pass_fail=False, score=0.1),
        _loss("c", drift_loss=2.0, pass_fail=True, score=0.7),
    ]
    agg = aggregate_generation_score(losses, weights)
    ns_agg = aggregate_namespaced_metrics(losses, weights)
    expected = _ref_scalar(agg["mean_score"], dict(ns_agg), weights)
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
    expected = _ref_scalar(agg["mean_score"], dict(ns_agg), weights)
    assert agg["scalar"] == expected
    assert agg["scalar_provenance"] == "builtin"


def test_live_aggregate_scalar_with_judges_and_an_abort_matches_reference() -> None:
    """End-to-end over the channels the profile derives, not just drift + pass.

    One run carries judge-attributed drift, one aborted. The reference builds
    each channel independently from the run facts and composes them, so this
    fails if the derivation, the within-channel weights, or the sum drift.
    """
    weights = ScoringWeights(per_judge_weights={"precision": 3.0}, default_judge_weight=1.5)
    judged = dataclasses.replace(
        _loss("a", drift_loss=0.0, pass_fail=True),
        drift_counts=(
            DriftCount(kind="custom:precision", severity="warning", count=2),
            DriftCount(kind="custom:unlisted", severity="info", count=1),
        ),
        per_judge_loss=tuple(
            JudgeLoss(
                judge_name=name,
                raw_loss=0.0,
                weight=weights.per_judge_weights.get(name, weights.default_judge_weight),
                weighted_loss=value,
            )
            for name, value in sorted(
                _ref_judge_channel(
                    (
                        DriftCount(kind="custom:precision", severity="warning", count=2),
                        DriftCount(kind="custom:unlisted", severity="info", count=1),
                    ),
                    weights,
                ).items()
            )
        ),
    )
    aborted = dataclasses.replace(
        _loss("b", drift_loss=0.0, pass_fail=False),
        task_failure_ratio=1.0,
        not_completed=True,
    )
    losses = [judged, aborted]

    agg = aggregate_generation_score(losses, weights)
    ns_agg = aggregate_namespaced_metrics(losses, weights)

    # Each channel, rebuilt independently and meaned over the two runs.
    judge_mean = sum(sum(_ref_judge_channel(p.drift_counts, weights).values()) for p in losses) / 2
    failure_mean = (
        sum(_ref_failure_channel(p.task_failure_ratio, p.not_completed, weights) for p in losses)
        / 2
    )
    assert ns_agg["judge:"] == weights.namespace_weights["judge:"] * judge_mean
    assert ns_agg["failure:"] == weights.namespace_weights["failure:"] * failure_mean
    # The aborted run's cost is explicable per entry.
    assert agg["per_entry"]["b"]["failure"] == _ref_failure_channel(1.0, True, weights)
    assert agg["per_entry"]["a"]["failure"] == 0.0
    assert agg["scalar"] == _ref_scalar(agg["mean_score"], dict(ns_agg), weights)


def test_scalar_sums_the_channels_in_sorted_order() -> None:
    """The composition is sorted, so it does not depend on mapping order.

    Float addition is not associative: the same channels handed over in a
    different iteration order must still produce the identical scalar, which
    is only true because both the built-in and the reference sort.
    """
    weights = ScoringWeights()
    forward = {
        "cost:": 0.1,
        "drift:": 1.5,
        "failure:": 60.0,
        "judge:": 4.0,
        "rubric:": -0.4,
        "schema:": 0.25,
    }
    reversed_order = dict(reversed(list(forward.items())))
    a = builtin_scalar(mean_score=0.5, namespace_aggregates=forward, weights=weights)
    b = builtin_scalar(mean_score=0.5, namespace_aggregates=reversed_order, weights=weights)
    assert a.hex() == b.hex()


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
