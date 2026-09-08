"""Named metric predictions use one schema for proposals and episode responses."""

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


def _parse(movements, *, judges=frozenset()):
    return parse_experiment_json(
        response_text=json.dumps(
            {
                "hypothesis": _base_hypothesis(expected_metric_movements=movements),
                "patches": _ok_patches(),
            }
        ),
        epoch_id="ep",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_mp(),
        custom_judge_names=judges,
    )


def test_predictions_cover_drift_cost_and_declared_judges() -> None:
    names = [
        "drift:off_topic",
        "cost:tokens_spent",
        "judge:file_findability",
        "rubric:slide_structure",
    ]
    exp = _parse(
        [
            {"metric_name": name, "direction": "decrease_or_neutral", "magnitude": "medium"}
            for name in names
        ],
        judges=frozenset({"file_findability"}),
    )
    assert [movement.metric_name for movement in exp.hypothesis.expected_metric_movements] == names
    assert all(
        m.direction == "decrease_or_neutral" and m.magnitude == "medium"
        for m in exp.hypothesis.expected_metric_movements
    )


@pytest.mark.parametrize(
    "name",
    [
        "drift:not_a_real_kind",
        "judge:undeclared",
        "file_findability",
        "drift:file_findability",
        "drift:custom:file_findability",
    ],
)
def test_predictions_refuse_unknown_targets_and_judge_aliases(name: str) -> None:
    with pytest.raises(ExperimentParseError):
        _parse(
            [{"metric_name": name, "direction": "increase", "magnitude": "small"}],
            judges=frozenset({"file_findability"}),
        )


@pytest.mark.parametrize(
    "movements",
    [
        [],
        [{"direction": "decrease", "magnitude": "small"}],
        [{"metric_name": "cost:tokens_spent", "direction": "downward", "magnitude": "small"}],
        [{"metric_name": "cost:tokens_spent", "direction": "decrease", "magnitude": "moderate"}],
    ],
)
def test_predictions_require_complete_nonempty_valid_movements(movements) -> None:
    with pytest.raises(ExperimentParseError):
        _parse(movements)


def test_episode_and_parser_share_the_hypothesis_schema() -> None:
    from zicato.proposer.foe_request import HYPOTHESIS_SCHEMA

    assert HYPOTHESIS_SCHEMA is EXPERIMENT_JSON_SCHEMA["properties"]["hypothesis"]
    assert "expected_metric_movements" in HYPOTHESIS_SCHEMA["required"]
