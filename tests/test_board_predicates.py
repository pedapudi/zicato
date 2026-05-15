"""Tests for the :class:`Predicate` / :class:`Rubric` factory helpers."""

from __future__ import annotations

import json

import pytest

from zicato.board.predicates import Predicate, Rubric
from zicato.core.types import Expectation


# ---------------------------------------------------------------------------
# Predicate.contains
# ---------------------------------------------------------------------------


def test_predicate_contains_returns_expected_text_expectation() -> None:
    """``Predicate.contains`` produces ``kind='expected_text'``."""
    exp = Predicate.contains("hello world")
    assert exp == Expectation(kind="expected_text", spec="hello world")
    assert exp.fires_on == "final_output"


def test_predicate_contains_respects_fires_on() -> None:
    """``fires_on`` carries through."""
    exp = Predicate.contains("ok", fires_on="conversation_end")
    assert exp.fires_on == "conversation_end"


# ---------------------------------------------------------------------------
# Predicate.regex
# ---------------------------------------------------------------------------


def test_predicate_regex_returns_regex_expectation() -> None:
    """``Predicate.regex`` produces ``kind='regex'``."""
    exp = Predicate.regex(r"answer \d+")
    assert exp.kind == "regex"
    assert exp.spec == r"answer \d+"


def test_predicate_regex_respects_fires_on() -> None:
    """``fires_on`` carries through for regex."""
    exp = Predicate.regex(r"x", fires_on="conversation_end")
    assert exp.fires_on == "conversation_end"


# ---------------------------------------------------------------------------
# Predicate.schema
# ---------------------------------------------------------------------------


def test_predicate_schema_returns_json_schema_expectation() -> None:
    """``Predicate.schema`` produces ``kind='json_schema'`` with serialized spec."""
    schema = {"type": "object", "required": ["a"]}
    exp = Predicate.schema(schema)
    assert exp.kind == "json_schema"
    # The spec is JSON-serialized.
    decoded = json.loads(exp.spec)
    assert decoded == schema


def test_predicate_schema_sorts_keys_for_canonical_form() -> None:
    """``Predicate.schema`` canonicalizes the schema with sorted keys."""
    schema = {"type": "object", "required": ["a"]}
    exp_a = Predicate.schema(schema)
    exp_b = Predicate.schema({"required": ["a"], "type": "object"})
    assert exp_a.spec == exp_b.spec


# ---------------------------------------------------------------------------
# Predicate.python
# ---------------------------------------------------------------------------


def test_predicate_python_returns_predicate_expectation() -> None:
    """``Predicate.python`` produces ``kind='predicate'`` with the dotted path."""
    exp = Predicate.python("mypkg.mymod.myfunc")
    assert exp == Expectation(kind="predicate", spec="mypkg.mymod.myfunc")


# ---------------------------------------------------------------------------
# Predicate is a namespace, not a class
# ---------------------------------------------------------------------------


def test_predicate_namespace_cannot_be_instantiated() -> None:
    """``Predicate()`` raises — it's a namespace, not a class to instantiate."""
    with pytest.raises(TypeError, match="namespace"):
        Predicate()


# ---------------------------------------------------------------------------
# Rubric.judge
# ---------------------------------------------------------------------------


def test_rubric_judge_encodes_threshold_and_scale() -> None:
    """``Rubric.judge`` encodes rubric, threshold, and scale into the spec."""
    exp = Rubric.judge("clarity 0-10", threshold=7.0, scale=(0.0, 10.0))
    assert exp.kind == "rubric"
    payload = json.loads(exp.spec)
    assert payload == {
        "rubric": "clarity 0-10",
        "threshold": 7.0,
        "scale": [0.0, 10.0],
    }


def test_rubric_judge_none_threshold_serializes_as_null() -> None:
    """A ``None`` threshold encodes as JSON null."""
    exp = Rubric.judge("rubric text")
    payload = json.loads(exp.spec)
    assert payload["threshold"] is None


def test_rubric_judge_respects_fires_on() -> None:
    """``fires_on`` carries through to the Expectation."""
    exp = Rubric.judge("rubric", fires_on="conversation_end")
    assert exp.fires_on == "conversation_end"


def test_rubric_judge_default_scale_is_zero_to_ten() -> None:
    """The default scale is 0-10."""
    exp = Rubric.judge("rubric")
    payload = json.loads(exp.spec)
    assert payload["scale"] == [0.0, 10.0]


def test_rubric_namespace_cannot_be_instantiated() -> None:
    """``Rubric()`` raises — namespace."""
    with pytest.raises(TypeError, match="namespace"):
        Rubric()


def test_rubric_judge_rejects_threshold_outside_scale() -> None:
    """A threshold above the scale upper bound is rejected."""
    with pytest.raises(ValueError, match="outside scale"):
        Rubric.judge("r", threshold=99.0, scale=(0.0, 10.0))


def test_rubric_judge_rejects_inverted_scale() -> None:
    """A scale where lo >= hi is rejected."""
    with pytest.raises(ValueError, match="lo < hi"):
        Rubric.judge("r", scale=(5.0, 5.0))


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_expectation_round_trips_through_json() -> None:
    """All factory-built Expectations serialize and deserialize cleanly."""
    exps = [
        Predicate.contains("hi"),
        Predicate.regex(r"\d+"),
        Predicate.schema({"type": "object"}),
        Predicate.python("a.b.c"),
        Rubric.judge("rubric", threshold=5.0, scale=(0.0, 10.0)),
    ]
    for exp in exps:
        as_dict = {"kind": exp.kind, "spec": exp.spec, "fires_on": exp.fires_on}
        s = json.dumps(as_dict)
        back = json.loads(s)
        rebuilt = Expectation(
            kind=back["kind"], spec=back["spec"], fires_on=back["fires_on"]
        )
        assert rebuilt == exp
