"""Tests for the JSON-schema validation + Experiment parser."""

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mp(
    mid: str,
    *,
    kind: str = "span",
    metadata: dict[str, str] | None = None,
) -> MutationPoint:
    return MutationPoint(
        id=mid,
        kind=kind,
        file=Path(f"/src/{mid}.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="placeholder",
        content_hash="deadbeef",
        metadata=metadata or {},
    )


_MUTATIONS = {
    "router__sp": _mp("router__sp"),
    "planner__thresh": _mp(
        "planner__thresh", metadata={"min": "0.0", "max": "1.0"}
    ),
    "router__strategy": _mp(
        "router__strategy", metadata={"enum": "alpha, beta , gamma"}
    ),
}


def _valid_payload() -> dict:
    return {
        "hypothesis": {
            "core_idea": "tighten router",
            "modulating": ["router__sp"],
            "why": "off_topic dominates",
            "expected_drift_movements": [
                {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
            ],
            "expected_pass_rate_delta": "+0.05",
        },
        "patches": [
            {
                "mutation_id": "router__sp",
                "op": "replace",
                "new_content": "new system prompt",
                "rationale": "remove preamble license",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_accepts_valid_payload() -> None:
    exp = parse_experiment_json(
        json.dumps(_valid_payload()),
        epoch_id="e1",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_MUTATIONS,
    )
    assert exp.epoch_id == "e1"
    assert exp.parent_generation_id == "v0"
    assert exp.generation_id == "v1"
    assert exp.id == "exp_e1_v1"
    assert exp.hypothesis.core_idea == "tighten router"
    assert exp.hypothesis.modulating == ("router__sp",)
    assert len(exp.hypothesis.expected_drift_movements) == 1
    assert exp.hypothesis.expected_drift_movements[0].kind == "off_topic"
    assert exp.hypothesis.risks == ""  # defaults when omitted
    assert len(exp.patches) == 1
    assert exp.patches[0].op == "replace"
    assert exp.patches[0].new_content == "new system prompt"
    assert exp.patches[0].new_numeric is None
    assert exp.patches[0].new_enum is None
    assert exp.outcome is None


def test_parse_strips_markdown_fences() -> None:
    body = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    exp = parse_experiment_json(
        body,
        epoch_id="e1",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_MUTATIONS,
    )
    assert exp.hypothesis.core_idea == "tighten router"


def test_parse_accepts_set_numeric_in_range() -> None:
    payload = _valid_payload()
    payload["patches"] = [
        {
            "mutation_id": "planner__thresh",
            "op": "set_numeric",
            "new_numeric": 0.5,
            "rationale": "midpoint",
        }
    ]
    payload["hypothesis"]["modulating"] = ["planner__thresh"]
    exp = parse_experiment_json(
        json.dumps(payload),
        epoch_id="e1",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_MUTATIONS,
    )
    assert exp.patches[0].op == "set_numeric"
    assert exp.patches[0].new_numeric == 0.5


def test_parse_accepts_set_enum_in_domain() -> None:
    payload = _valid_payload()
    payload["patches"] = [
        {
            "mutation_id": "router__strategy",
            "op": "set_enum",
            "new_enum": "beta",
            "rationale": "switch strategy",
        }
    ]
    payload["hypothesis"]["modulating"] = ["router__strategy"]
    exp = parse_experiment_json(
        json.dumps(payload),
        epoch_id="e1",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id=_MUTATIONS,
    )
    assert exp.patches[0].new_enum == "beta"


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


def _expect_parse_error(payload, match_substr: str) -> None:
    with pytest.raises(ExperimentParseError) as exc_info:
        parse_experiment_json(
            json.dumps(payload) if not isinstance(payload, str) else payload,
            epoch_id="e1",
            parent_gen="v0",
            new_gen="v1",
            mutations_by_id=_MUTATIONS,
        )
    assert match_substr.lower() in str(exc_info.value).lower()


def test_parse_rejects_invalid_json() -> None:
    _expect_parse_error("{this is not json", "valid json")


def test_parse_rejects_empty_response() -> None:
    _expect_parse_error("", "empty response")


def test_parse_rejects_top_level_array() -> None:
    _expect_parse_error("[]", "object at the top level")


def test_parse_rejects_missing_hypothesis() -> None:
    p = _valid_payload()
    del p["hypothesis"]
    _expect_parse_error(p, "hypothesis")


def test_parse_rejects_missing_patches() -> None:
    p = _valid_payload()
    del p["patches"]
    _expect_parse_error(p, "patches")


def test_parse_rejects_empty_patches_array() -> None:
    p = _valid_payload()
    p["patches"] = []
    _expect_parse_error(p, "non-empty")


def test_parse_rejects_invalid_op() -> None:
    p = _valid_payload()
    p["patches"][0]["op"] = "delete"
    _expect_parse_error(p, "delete")


def test_parse_rejects_invalid_direction() -> None:
    p = _valid_payload()
    p["hypothesis"]["expected_drift_movements"][0]["direction"] = "sideways"
    _expect_parse_error(p, "sideways")


def test_parse_rejects_invalid_magnitude() -> None:
    p = _valid_payload()
    p["hypothesis"]["expected_drift_movements"][0]["magnitude"] = "huge"
    _expect_parse_error(p, "huge")


def test_parse_rejects_unknown_mutation_id() -> None:
    p = _valid_payload()
    p["patches"][0]["mutation_id"] = "nonexistent_id"
    _expect_parse_error(p, "nonexistent_id")


def test_parse_rejects_unknown_modulating_id() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = ["never_seen_id"]
    _expect_parse_error(p, "never_seen_id")


def test_parse_rejects_replace_without_new_content() -> None:
    p = _valid_payload()
    del p["patches"][0]["new_content"]
    _expect_parse_error(p, "new_content")


def test_parse_rejects_replace_with_extra_numeric_field() -> None:
    p = _valid_payload()
    p["patches"][0]["new_numeric"] = 1
    _expect_parse_error(p, "must not set 'new_numeric'")


def test_parse_rejects_set_numeric_out_of_range_high() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = ["planner__thresh"]
    p["patches"] = [
        {
            "mutation_id": "planner__thresh",
            "op": "set_numeric",
            "new_numeric": 2.0,
            "rationale": "out of range",
        }
    ]
    _expect_parse_error(p, "above max")


def test_parse_rejects_set_numeric_out_of_range_low() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = ["planner__thresh"]
    p["patches"] = [
        {
            "mutation_id": "planner__thresh",
            "op": "set_numeric",
            "new_numeric": -0.5,
            "rationale": "out of range",
        }
    ]
    _expect_parse_error(p, "below min")


def test_parse_rejects_set_enum_not_in_domain() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = ["router__strategy"]
    p["patches"] = [
        {
            "mutation_id": "router__strategy",
            "op": "set_enum",
            "new_enum": "delta",
            "rationale": "not in domain",
        }
    ]
    _expect_parse_error(p, "delta")


def test_parse_rejects_unknown_drift_kind() -> None:
    p = _valid_payload()
    p["hypothesis"]["expected_drift_movements"] = [
        {"kind": "mystery_kind", "direction": "decrease", "magnitude": "small"}
    ]
    _expect_parse_error(p, "mystery_kind")


def test_parse_rejects_set_numeric_with_string_value() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = ["planner__thresh"]
    p["patches"] = [
        {
            "mutation_id": "planner__thresh",
            "op": "set_numeric",
            "new_numeric": "not a number",
            "rationale": "wrong type",
        }
    ]
    _expect_parse_error(p, "number")


def test_parse_rejects_empty_modulating() -> None:
    p = _valid_payload()
    p["hypothesis"]["modulating"] = []
    _expect_parse_error(p, "non-empty")


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_schema_has_required_top_level_keys() -> None:
    assert EXPERIMENT_JSON_SCHEMA["required"] == ["hypothesis", "patches"]
