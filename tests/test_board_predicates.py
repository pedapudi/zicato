"""Tests for the :class:`Predicate` / :class:`Rubric` / :class:`Judge` factories."""

from __future__ import annotations

import json

import pytest
from goldfive import DriftSeverity

from zicato.board.judges import Judge
from zicato.board.predicates import Predicate, Rubric
from zicato.core.types import (
    Expectation,
    ExpectationKind,
    JudgeMode,
    JudgeSpec,
    OutputScope,
)

# ---------------------------------------------------------------------------
# Predicate.contains
# ---------------------------------------------------------------------------


def test_predicate_contains_returns_expected_text_expectation() -> None:
    """``Predicate.contains`` produces ``kind=ExpectationKind.EXPECTED_TEXT``."""
    exp = Predicate.contains("hello world")
    assert exp == Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="hello world")
    assert exp.kind is ExpectationKind.EXPECTED_TEXT
    assert exp.reads is OutputScope.FINAL


def test_predicate_contains_respects_reads() -> None:
    """``reads`` carries through."""
    exp = Predicate.contains("ok", reads=OutputScope.TRANSCRIPT)
    assert exp.reads is OutputScope.TRANSCRIPT


# ---------------------------------------------------------------------------
# Predicate.regex
# ---------------------------------------------------------------------------


def test_predicate_regex_returns_regex_expectation() -> None:
    """``Predicate.regex`` produces ``kind=ExpectationKind.REGEX``."""
    exp = Predicate.regex(r"answer \d+")
    assert exp.kind is ExpectationKind.REGEX
    assert exp.spec == r"answer \d+"


def test_predicate_regex_respects_reads() -> None:
    """``reads`` carries through for regex."""
    exp = Predicate.regex(r"x", reads=OutputScope.TRANSCRIPT)
    assert exp.reads is OutputScope.TRANSCRIPT


# ---------------------------------------------------------------------------
# Predicate.schema
# ---------------------------------------------------------------------------


def test_predicate_schema_returns_json_schema_expectation() -> None:
    """``Predicate.schema`` produces ``kind=ExpectationKind.JSON_SCHEMA``."""
    schema = {"type": "object", "required": ["a"]}
    exp = Predicate.schema(schema)
    assert exp.kind is ExpectationKind.JSON_SCHEMA
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
    """``Predicate.python`` produces ``kind=ExpectationKind.PREDICATE``."""
    exp = Predicate.python("mypkg.mymod.myfunc")
    assert exp == Expectation(kind=ExpectationKind.PREDICATE, spec="mypkg.mymod.myfunc")


# ---------------------------------------------------------------------------
# Predicate is a namespace, not a class
# ---------------------------------------------------------------------------


def test_predicate_namespace_cannot_be_instantiated() -> None:
    """``Predicate()`` raises — it's a namespace, not a class to instantiate."""
    with pytest.raises(TypeError, match="namespace"):
        Predicate()


# ---------------------------------------------------------------------------
# Rubric.score
# ---------------------------------------------------------------------------


def test_rubric_score_encodes_threshold_and_scale() -> None:
    """``Rubric.score`` encodes rubric, threshold, and scale into the spec."""
    exp = Rubric.score("clarity 0-10", threshold=7.0, scale=(0.0, 10.0))
    assert exp.kind is ExpectationKind.RUBRIC
    payload = json.loads(exp.spec)
    assert payload == {
        "rubric": "clarity 0-10",
        "threshold": 7.0,
        "scale": [0.0, 10.0],
    }


def test_rubric_score_none_threshold_serializes_as_null() -> None:
    """A ``None`` threshold encodes as JSON null."""
    exp = Rubric.score("rubric text")
    payload = json.loads(exp.spec)
    assert payload["threshold"] is None


def test_rubric_score_respects_reads() -> None:
    """``reads`` carries through to the Expectation."""
    exp = Rubric.score("rubric", reads=OutputScope.TRANSCRIPT)
    assert exp.reads is OutputScope.TRANSCRIPT


def test_rubric_score_default_scale_is_zero_to_ten() -> None:
    """The default scale is 0-10."""
    exp = Rubric.score("rubric")
    payload = json.loads(exp.spec)
    assert payload["scale"] == [0.0, 10.0]


def test_rubric_namespace_cannot_be_instantiated() -> None:
    """``Rubric()`` raises — namespace."""
    with pytest.raises(TypeError, match="namespace"):
        Rubric()


def test_rubric_score_rejects_threshold_outside_scale() -> None:
    """A threshold above the scale upper bound is rejected."""
    with pytest.raises(ValueError, match="outside scale"):
        Rubric.score("r", threshold=99.0, scale=(0.0, 10.0))


def test_rubric_score_rejects_inverted_scale() -> None:
    """A scale where lo >= hi is rejected."""
    with pytest.raises(ValueError, match="lo < hi"):
        Rubric.score("r", scale=(5.0, 5.0))


# ---------------------------------------------------------------------------
# Judge.custom — inline PROCESS judge
# ---------------------------------------------------------------------------


def test_judge_custom_returns_inline_judge_spec() -> None:
    """``Judge.custom`` produces an inline-mode :class:`JudgeSpec`."""
    js = Judge.custom("stays_on_task", "never abandons the goal", severity=DriftSeverity.WARNING)
    assert isinstance(js, JudgeSpec)
    assert js.name == "stays_on_task"
    assert js.mode is JudgeMode.INLINE
    assert js.body == "never abandons the goal"
    assert js.severity is DriftSeverity.WARNING


def test_judge_custom_rejects_empty_criterion() -> None:
    """An empty / whitespace criterion is rejected."""
    with pytest.raises(ValueError, match="criterion"):
        Judge.custom("name", "   ", severity=DriftSeverity.INFO)


def test_judge_custom_rejects_non_severity() -> None:
    """A non-:class:`DriftSeverity` ``severity`` is rejected."""
    with pytest.raises(ValueError, match="severity"):
        Judge.custom("name", "criterion", severity="warning")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Judge.python — dotted-path PROCESS judge
# ---------------------------------------------------------------------------


def test_judge_python_returns_python_judge_spec() -> None:
    """``Judge.python`` produces a python-mode :class:`JudgeSpec`."""
    js = Judge.python("no_pii", "myproj.judges.pii_guard", severity=DriftSeverity.CRITICAL)
    assert js.name == "no_pii"
    assert js.mode is JudgeMode.PYTHON
    assert js.body == "myproj.judges.pii_guard"
    assert js.severity is DriftSeverity.CRITICAL


def test_judge_python_rejects_path_without_module() -> None:
    """A dotted path with no module component is rejected."""
    with pytest.raises(ValueError, match="dotted path"):
        Judge.python("name", "just_a_name", severity=DriftSeverity.INFO)


# ---------------------------------------------------------------------------
# Judge name validation — the name becomes goldfive's judge_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "good", ["a", "no_pii", "stays-on-task", "judge3", "x9_y-z", "3leading_digit_ok"]
)
def test_judge_accepts_valid_slug_names(good: str) -> None:
    """Slug-like names are accepted (a leading digit is permitted)."""
    js = Judge.custom(good, "c", severity=DriftSeverity.INFO)
    assert js.name == good


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "trailing!", "-dash", "dot.name"])
def test_judge_rejects_non_slug_names(bad: str) -> None:
    """Non-slug names are rejected — the name is a stable identity."""
    with pytest.raises(ValueError, match="name|mandatory"):
        Judge.custom(bad, "c", severity=DriftSeverity.INFO)


def test_judge_namespace_cannot_be_instantiated() -> None:
    """``Judge()`` raises — namespace, like Predicate / Rubric."""
    with pytest.raises(TypeError, match="namespace"):
        Judge()


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_expectation_round_trips_through_json() -> None:
    """All factory-built Expectations serialize and deserialize cleanly.

    The enum-valued fields subclass ``str``, so ``json.dumps`` emits the
    bare token and rebuilding through the enum recovers the member.
    """
    exps = [
        Predicate.contains("hi"),
        Predicate.regex(r"\d+"),
        Predicate.schema({"type": "object"}),
        Predicate.python("a.b.c"),
        Rubric.score("rubric", threshold=5.0, scale=(0.0, 10.0)),
    ]
    for exp in exps:
        as_dict = {"kind": exp.kind, "spec": exp.spec, "reads": exp.reads}
        s = json.dumps(as_dict)
        back = json.loads(s)
        rebuilt = Expectation(
            kind=ExpectationKind(back["kind"]),
            spec=back["spec"],
            reads=OutputScope(back["reads"]),
        )
        assert rebuilt == exp
