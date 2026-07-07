"""Issue #19 phase 2 — declarative transform registry + the two contract fields.

Covers:

* every registered transform op (``linear`` / ``pow`` / ``harmonic`` / ``cap``
  / ``clip`` / ``log1p``) — value behaviour;
* param validation rejecting bad specs AT LOAD (unknown op, missing / typo'd /
  non-finite param, ``clip`` lo>hi), and that ``ScoringWeights`` construction
  triggers it;
* ``pass_transform`` ``pow(2)`` reproduces quadratic-recall at Seam 2;
* ``drift_kind_aggregation`` ``harmonic`` reproduces the OLD looping value at
  Seam 1 (the unconditional special-case the builtin no longer carries);
* a retired ``pass_exponent`` config key is rejected loudly (not silently dropped);
* NEUTRAL defaults (no transforms) leave drift + scalar at the linear builtin
  with ``"builtin"`` provenance;
* provenance is recorded for the transformed pass term + each transformed kind.

The serde round-trip / auto-roll lives in
``test_contract_serializer_completeness.py`` (the new fields were added to its
curated table). The worker serialize→JSON→deserialize round-trip that DRIVES
real scoring lives in ``test_subprocess_workers.py``.
"""

from __future__ import annotations

import math

import pytest

from zicato.core import DriftCount, ScoringWeights
from zicato.scoring import (
    PROVENANCE_BUILTIN,
    DriftContext,
    ScalarContext,
    builtin_drift_loss,
    builtin_scalar,
    resolve_drift_loss,
    resolve_scalar,
)
from zicato.scoring.transforms import (
    TransformSpecError,
    apply_transform,
    is_neutral,
    transform_op_names,
    validate_transform_spec,
)

# ---------------------------------------------------------------------------
# 1. The registry — each op's value behaviour.
# ---------------------------------------------------------------------------


def test_op_set_is_the_documented_six() -> None:
    assert set(transform_op_names()) == {"linear", "pow", "harmonic", "cap", "clip", "log1p"}


def test_linear_is_identity() -> None:
    for v in (-3.0, 0.0, 0.5, 7.25):
        assert apply_transform({"op": "linear"}, v) == v


def test_pow_raises_to_exponent() -> None:
    assert apply_transform({"op": "pow", "exponent": 2.0}, 0.4) == pytest.approx(0.16)
    assert apply_transform({"op": "pow", "exponent": 1.0}, 0.4) == pytest.approx(0.4)
    assert apply_transform({"op": "pow", "exponent": 0.5}, 9.0) == pytest.approx(3.0)


def test_harmonic_is_partial_harmonic_sum_over_count() -> None:
    assert apply_transform({"op": "harmonic"}, 0) == 0.0
    assert apply_transform({"op": "harmonic"}, 1) == pytest.approx(1.0)
    assert apply_transform({"op": "harmonic"}, 2) == pytest.approx(1.0 + 0.5)
    assert apply_transform({"op": "harmonic"}, 3) == pytest.approx(1.0 + 0.5 + 1.0 / 3.0)
    # Fractional counts truncate (a count is an integer event tally).
    assert apply_transform({"op": "harmonic"}, 2.9) == pytest.approx(1.0 + 0.5)


def test_cap_upper_bounds() -> None:
    assert apply_transform({"op": "cap", "max": 5.0}, 3.0) == 3.0
    assert apply_transform({"op": "cap", "max": 5.0}, 9.0) == 5.0


def test_clip_two_sided() -> None:
    spec = {"op": "clip", "lo": 0.0, "hi": 1.0}
    assert apply_transform(spec, -2.0) == 0.0
    assert apply_transform(spec, 0.5) == 0.5
    assert apply_transform(spec, 4.0) == 1.0


def test_log1p_diminishing() -> None:
    assert apply_transform({"op": "log1p"}, 0.0) == 0.0
    assert apply_transform({"op": "log1p"}, math.e - 1.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2. Validation — fail-fast at load, never a NaN mid-scoring.
# ---------------------------------------------------------------------------


def test_validate_accepts_every_well_formed_op() -> None:
    for spec in (
        {"op": "linear"},
        {"op": "pow", "exponent": 2.0},
        {"op": "harmonic"},
        {"op": "cap", "max": 5.0},
        {"op": "clip", "lo": 0.0, "hi": 1.0},
        {"op": "log1p"},
    ):
        validate_transform_spec(spec)  # must not raise


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "nope"},  # unknown op
        {"exponent": 2.0},  # missing op
        {"op": "pow"},  # missing required param
        {"op": "pow", "exponent": float("inf")},  # non-finite param
        {"op": "pow", "exponent": float("nan")},  # NaN param
        {"op": "pow", "exponant": 2.0},  # typo'd param name (unknown)
        {"op": "pow", "exponent": "2"},  # non-numeric param
        {"op": "pow", "exponent": True},  # bool is not a number here
        {"op": "cap"},  # missing max
        {"op": "clip", "lo": 0.0},  # missing hi
        {"op": "clip", "lo": 1.0, "hi": 0.0},  # lo > hi
        {"op": "linear", "exponent": 2.0},  # extra param on a no-param op
        "linear",  # not a mapping
    ],
)
def test_validate_rejects_bad_specs(bad: object) -> None:
    with pytest.raises(TransformSpecError):
        validate_transform_spec(bad)


def test_scoringweights_validates_pass_transform_at_construction() -> None:
    with pytest.raises(ValueError):
        ScoringWeights(pass_transform={"op": "pow"})  # missing exponent


def test_scoringweights_validates_drift_kind_aggregation_at_construction() -> None:
    with pytest.raises(ValueError) as exc:
        ScoringWeights(drift_kind_aggregation={"off_topic": {"op": "bogus"}})
    # The error names the offending kind for a clear contract-load failure.
    assert "off_topic" in str(exc.value)


def test_scoringweights_neutral_specs_construct() -> None:
    ScoringWeights(pass_transform=None, drift_kind_aggregation={})
    ScoringWeights(pass_transform={"op": "linear"})


def test_is_neutral() -> None:
    assert is_neutral(None)
    assert is_neutral({"op": "linear"})
    assert not is_neutral({"op": "pow", "exponent": 2.0})


# ---------------------------------------------------------------------------
# 3. Seam 2 — pass_transform pow reproduces quadratic recall.
# ---------------------------------------------------------------------------


def _scalar_ctx(weights: ScoringWeights, *, mean_score: float, drift_loss_mean: float = 0.0):
    return ScalarContext(
        pass_rate=mean_score,
        mean_score=mean_score,
        drift_loss_mean=drift_loss_mean,
        namespace_aggregates={},
        per_judge_loss={},
        weights=weights,
        builtin_scalar=builtin_scalar(
            mean_score=mean_score,
            drift_loss_mean=drift_loss_mean,
            namespace_aggregates={},
            weights=weights,
        ),
    )


def test_pass_transform_pow_reproduces_quadratic_recall() -> None:
    """``pass_transform=pow(2)`` ⇒ the miss term is ``(1 - mean_score) ** 2``."""
    mean_score = 0.3  # miss = 0.7
    weights = ScoringWeights(
        drift_weight=1.0,
        pass_weight=2.0,
        pass_transform={"op": "pow", "exponent": 2.0},
    )
    ctx = _scalar_ctx(weights, mean_score=mean_score, drift_loss_mean=1.5)
    scalar, prov = resolve_scalar(ctx)

    miss = 1.0 - mean_score
    expected = weights.drift_weight * 1.5 + weights.pass_weight * (miss**2)
    assert scalar == pytest.approx(expected)
    # Provenance records the pass transform structurally.
    assert prov == "transform:pass=pow(2.0)"

    # Cross-check against the OLD bespoke ``pass_exponent`` formula
    # (1 - mean_score) ** pass_exponent — byte-for-byte the same shape.
    legacy = weights.drift_weight * 1.5 + weights.pass_weight * (miss**2.0)
    assert scalar == pytest.approx(legacy)


def test_pass_transform_neutral_is_builtin() -> None:
    weights = ScoringWeights(pass_weight=2.0)  # no pass_transform → linear
    ctx = _scalar_ctx(weights, mean_score=0.3, drift_loss_mean=1.5)
    scalar, prov = resolve_scalar(ctx)
    assert scalar == ctx.builtin_scalar
    assert prov == PROVENANCE_BUILTIN == "builtin"


def test_pass_transform_linear_explicit_is_builtin() -> None:
    weights = ScoringWeights(pass_weight=2.0, pass_transform={"op": "linear"})
    ctx = _scalar_ctx(weights, mean_score=0.3, drift_loss_mean=1.5)
    scalar, prov = resolve_scalar(ctx)
    assert scalar == ctx.builtin_scalar
    assert prov == "builtin"


# ---------------------------------------------------------------------------
# 4. Seam 1 — drift_kind_aggregation harmonic reproduces the OLD looping value.
# ---------------------------------------------------------------------------


def _drift_ctx(weights: ScoringWeights, drift_counts: tuple[DriftCount, ...]):
    return DriftContext(
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
        builtin_loss=builtin_drift_loss(
            drift_counts=drift_counts,
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=0,
            weights=weights,
        ),
    )


def _old_harmonic_looping_loss(
    drift_counts: tuple[DriftCount, ...], weights: ScoringWeights
) -> float:
    """The OLD unconditional ``looping_reasoning`` special-case, transcribed.

    Per the historical special-case, the looping kind's count contributed via
    the partial harmonic sum ``1 + 1/2 + … + 1/count`` (diminishing returns)
    in place of the linear ``count``, still scaled by severity × kind weight.
    All other kinds were plain linear.
    """
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = weights.per_kind_weights.get(c.kind, 1.0)
        if c.kind == "looping_reasoning":
            shaped = sum(1.0 / k for k in range(1, int(c.count) + 1))
        else:
            shaped = c.count
        loss += sev_mult * kind_mult * shaped
    return max(0.0, loss)


def test_drift_kind_harmonic_reproduces_old_looping_value() -> None:
    drift = (
        DriftCount(kind="looping_reasoning", severity="warning", count=4),
        DriftCount(kind="off_topic", severity="info", count=2),  # untouched, linear
    )
    weights = ScoringWeights(
        per_kind_weights={"looping_reasoning": 1.5},
        drift_kind_aggregation={"looping_reasoning": {"op": "harmonic"}},
    )
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))

    expected = _old_harmonic_looping_loss(drift, weights)
    assert loss == pytest.approx(expected)
    # Provenance records which kind reshaped + how.
    assert prov == "transform:drift{looping_reasoning=harmonic}"

    # The harmonic-transformed loss is STRICTLY less than the linear builtin
    # for count>1 (diminishing returns), proving it actually reshaped.
    builtin = builtin_drift_loss(
        drift_counts=drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert loss < builtin


def test_builtin_drift_loss_is_pure_linear_no_harmonic() -> None:
    """The builtin no longer carries the unconditional harmonic special-case.

    A looping_reasoning count scores LINEARLY in the builtin; harmonic is now
    opt-in only via drift_kind_aggregation.
    """
    drift = (DriftCount(kind="looping_reasoning", severity="warning", count=4),)
    weights = ScoringWeights()  # no aggregation configured
    builtin = builtin_drift_loss(
        drift_counts=drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    sev = weights.severity_weights["warning"]
    assert builtin == pytest.approx(sev * 1.0 * 4)  # linear count, NOT harmonic
    # And the dispatcher with no aggregation returns the builtin verbatim.
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))
    assert loss == builtin
    assert prov == "builtin"


def test_drift_kind_cap_bounds_a_kind() -> None:
    drift = (DriftCount(kind="off_topic", severity="info", count=9),)
    weights = ScoringWeights(drift_kind_aggregation={"off_topic": {"op": "cap", "max": 5.0}})
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))
    sev = weights.severity_weights["info"]
    assert loss == pytest.approx(sev * 1.0 * 5.0)  # capped count 5, not 9
    assert prov == "transform:drift{off_topic=cap(5.0)}"


def test_drift_kind_neutral_defaults_are_builtin() -> None:
    drift = (
        DriftCount(kind="off_topic", severity="warning", count=2),
        DriftCount(kind="looping_reasoning", severity="info", count=3),
    )
    weights = ScoringWeights()  # nothing configured
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))
    assert loss == builtin_drift_loss(
        drift_counts=drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert prov == "builtin"


def test_drift_kind_explicit_linear_is_builtin() -> None:
    drift = (DriftCount(kind="off_topic", severity="warning", count=2),)
    weights = ScoringWeights(drift_kind_aggregation={"off_topic": {"op": "linear"}})
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))
    assert loss == builtin_drift_loss(
        drift_counts=drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    assert prov == "builtin"


def test_drift_kind_aggregation_only_touches_named_kind() -> None:
    """A configured kind absent from the run's drift counts changes nothing."""
    drift = (DriftCount(kind="off_topic", severity="warning", count=2),)
    weights = ScoringWeights(
        drift_kind_aggregation={"looping_reasoning": {"op": "harmonic"}},  # no looping present
    )
    loss, prov = resolve_drift_loss(_drift_ctx(weights, drift))
    assert loss == builtin_drift_loss(
        drift_counts=drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )
    # No kind actually transformed → reported as builtin (byte-identical).
    assert prov == "builtin"


# ---------------------------------------------------------------------------
# 5. Legacy pass_exponent is REJECTED loudly (issue #19 retired the field).
# ---------------------------------------------------------------------------


def test_legacy_pass_exponent_is_rejected_loudly() -> None:
    """A retired ``pass_exponent`` key fails fast with the migration message,
    rather than being silently ignored by the field-enumerating loader (which
    would score linearly with no error, no warning, and no epoch roll)."""
    from zicato.workspace_loader import scoring_weights_from_dict

    with pytest.raises(ValueError, match="pass_exponent") as exc:
        scoring_weights_from_dict({"pass_weight": 2.0, "pass_exponent": 2.0})
    # The message points the operator at the replacement.
    assert "pass_transform" in str(exc.value)
    assert "pow" in str(exc.value)


def test_legacy_pass_exponent_rejected_via_lifecycle_path_too() -> None:
    """The frozen-snapshot lifecycle loader rejects symmetrically, so a stale
    contract can't sneak a silently-ignored ``pass_exponent`` through either path."""
    from zicato.epoch.lifecycle import _scoring_from_dict

    with pytest.raises(ValueError, match="pass_exponent"):
        _scoring_from_dict({"pass_exponent": 2.0})
