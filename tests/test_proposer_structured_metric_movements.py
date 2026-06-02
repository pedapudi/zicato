"""Tests for the proposer's generalised ``expected_metric_movements`` schema.

The proposer JSON schema now accepts either ``expected_drift_movements``
(back-compat) or ``expected_metric_movements`` (generalised
namespaced) — or both. These tests cover the new field's shape,
namespace-prefixed metric names, and round-trip into the typed
:class:`HypothesisSpec`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import MutationPoint
from zicato.proposer.structured import (
    EXPERIMENT_JSON_SCHEMA,
    ExperimentParseError,
    parse_experiment_json,
)


def _mp() -> dict[str, MutationPoint]:
    """Single mutation manifest entry used by every test below."""
    p = MutationPoint(
        id="router__system_prompt",
        kind="span",
        file=Path("/abs/file.py"),
        source_root=Path("/abs"),
        line_start=1,
        line_end=2,
        content="placeholder",
        content_hash="h",
    )
    return {p.id: p}


def _ok_patches() -> list[dict]:
    return [
        {
            "mutation_id": "router__system_prompt",
            "op": "replace",
            "new_content": "tighter prompt",
            "rationale": "shorter content reduces token spend",
        }
    ]


def _base_hypothesis(**overrides: object) -> dict:
    """Returns a hypothesis dict with no movements; tests fill in one or both."""
    d: dict[str, object] = {
        "core_idea": "Reduce cost by trimming the prompt.",
        "modulating": ["router__system_prompt"],
        "why": "Cost dominates value at high token counts.",
        "expected_pass_rate_delta": "+0.00",
    }
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Schema-level acceptance of expected_metric_movements
# ---------------------------------------------------------------------------


def test_schema_accepts_response_with_only_metric_movements() -> None:
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "cost:tokens_spent",
                    "direction": "decrease",
                    "magnitude": "medium",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    exp = parse_experiment_json(
        response_text=json.dumps(payload),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
    )
    assert len(exp.hypothesis.expected_metric_movements) == 1
    mm = exp.hypothesis.expected_metric_movements[0]
    assert mm.metric_name == "cost:tokens_spent"
    assert mm.direction == "decrease"
    assert mm.magnitude == "medium"
    # Drift-specific field stays empty when the proposer only used the
    # generalised path.
    assert exp.hypothesis.expected_drift_movements == ()


def test_schema_accepts_response_with_only_drift_movements_back_compat() -> None:
    payload = {
        "hypothesis": _base_hypothesis(
            expected_drift_movements=[
                {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
            ]
        ),
        "patches": _ok_patches(),
    }
    exp = parse_experiment_json(
        response_text=json.dumps(payload),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
    )
    assert len(exp.hypothesis.expected_drift_movements) == 1
    assert exp.hypothesis.expected_drift_movements[0].kind == "off_topic"
    assert exp.hypothesis.expected_metric_movements == ()


def test_schema_accepts_both_drift_and_metric_movements_in_one_hypothesis() -> None:
    payload = {
        "hypothesis": _base_hypothesis(
            expected_drift_movements=[
                {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
            ],
            expected_metric_movements=[
                {
                    "metric_name": "cost:tokens_spent",
                    "direction": "decrease",
                    "magnitude": "medium",
                },
                {
                    "metric_name": "rubric:slide_structure",
                    "direction": "increase_or_neutral",
                    "magnitude": "small",
                },
            ],
        ),
        "patches": _ok_patches(),
    }
    exp = parse_experiment_json(
        response_text=json.dumps(payload),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
    )
    assert len(exp.hypothesis.expected_drift_movements) == 1
    assert len(exp.hypothesis.expected_metric_movements) == 2


def test_schema_rejects_hypothesis_with_neither_movements_field() -> None:
    payload = {
        "hypothesis": _base_hypothesis(),  # no movements field at all
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError) as exc:
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
        )
    assert "expected_drift_movements" in str(exc.value)
    assert "expected_metric_movements" in str(exc.value)


def test_schema_rejects_malformed_metric_movement_entry() -> None:
    # Missing metric_name.
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[{"direction": "decrease", "magnitude": "small"}]
        ),
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError):
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
        )


def test_schema_rejects_metric_movement_with_invalid_direction() -> None:
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "cost:tokens_spent",
                    "direction": "downward",  # invalid
                    "magnitude": "small",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError):
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
        )


def test_schema_validates_drift_namespace_kind_inside_metric_movement() -> None:
    """A metric_name starting with ``drift:`` must reference a registered drift kind."""
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "drift:not_a_real_kind",
                    "direction": "decrease",
                    "magnitude": "small",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError) as exc:
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
        )
    assert "unknown drift kind" in str(exc.value)


def test_drift_metric_accepts_declared_custom_judge_name() -> None:
    """A ``drift:<name>`` metric validates when ``<name>`` is a declared
    custom judge, even though it is not a built-in goldfive drift kind."""
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "drift:file_findability",
                    "direction": "decrease",
                    "magnitude": "medium",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    exp = parse_experiment_json(
        response_text=json.dumps(payload),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
        custom_judge_names=frozenset({"file_findability"}),
    )
    names = {m.metric_name for m in exp.hypothesis.expected_metric_movements}
    assert names == {"drift:file_findability"}


def test_drift_metric_rejects_custom_judge_name_when_not_declared() -> None:
    """``drift:file_findability`` is still rejected when the judge is not
    declared (no ``custom_judge_names`` threaded) — it is neither a
    built-in drift kind nor a known judge."""
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "drift:file_findability",
                    "direction": "decrease",
                    "magnitude": "medium",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError) as exc:
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
        )
    assert "unknown drift kind" in str(exc.value)


def test_drift_metric_rejects_bogus_kind_even_with_judges_declared() -> None:
    """A genuinely-unknown drift kind is still rejected even when other
    custom judges are declared — only the declared names are accepted."""
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "drift:bogus_kind",
                    "direction": "decrease",
                    "magnitude": "small",
                }
            ]
        ),
        "patches": _ok_patches(),
    }
    with pytest.raises(ExperimentParseError) as exc:
        parse_experiment_json(
            response_text=json.dumps(payload),
            epoch_id="ep",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_mp(),
            custom_judge_names=frozenset({"file_findability"}),
        )
    assert "unknown drift kind" in str(exc.value)
    assert "bogus_kind" in str(exc.value)


def test_schema_accepts_non_drift_namespace_without_kind_registry_check() -> None:
    """``cost:`` / ``rubric:`` / ``latency:`` / ``schema:`` / custom namespaces
    are not constrained by the goldfive drift-kind registry."""
    payload = {
        "hypothesis": _base_hypothesis(
            expected_metric_movements=[
                {
                    "metric_name": "latency:p99_response_ms",
                    "direction": "decrease",
                    "magnitude": "small",
                },
                {
                    "metric_name": "rubric:made_up_dimension",
                    "direction": "increase",
                    "magnitude": "small",
                },
            ]
        ),
        "patches": _ok_patches(),
    }
    # Both should parse cleanly — the proposer is free to invent
    # namespaces beyond the canonical ones.
    exp = parse_experiment_json(
        response_text=json.dumps(payload),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
    )
    names = {m.metric_name for m in exp.hypothesis.expected_metric_movements}
    assert names == {"latency:p99_response_ms", "rubric:made_up_dimension"}


def test_schema_metadata_carries_metric_movements_in_schema_object() -> None:
    """The schema doc declares both movement-array properties so the
    proposer's JSON-schema-aware inputs can introspect them."""
    props = EXPERIMENT_JSON_SCHEMA["properties"]["hypothesis"]["properties"]
    assert "expected_drift_movements" in props
    assert "expected_metric_movements" in props
    # Each movements item has the expected fields.
    mm_items = props["expected_metric_movements"]["items"]
    assert "metric_name" in mm_items["required"]
    assert "direction" in mm_items["required"]
    assert "magnitude" in mm_items["required"]
