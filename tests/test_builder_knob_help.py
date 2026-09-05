"""The knob help the builder serves is read from the scoring configuration's docstrings."""

from __future__ import annotations

from dataclasses import dataclass, field

from zicato.builder.knob_help import (
    field_docs,
    knob_help,
    knob_paths,
    plain_text,
    render_default,
)
from zicato.core.scoring_config import LadderConfig, ScoringWeights


@dataclass(frozen=True)
class _Documented:
    """A knob dataclass with a Fields section.

    The lead paragraph is not a field entry.

    Fields
    ------
    alpha:
        The first paragraph of ``alpha``.

        Its second paragraph, which
        wraps across two lines.
    beta:
        A list follows:

        * one item that
          wraps
        * a second item
    """

    alpha: int = 1
    beta: str = "x"


def test_field_docs_splits_entries_paragraphs_and_list_items() -> None:
    docs = field_docs(_Documented)
    assert set(docs) == {"alpha", "beta"}
    assert docs["alpha"] == (
        "The first paragraph of alpha.\n\nIts second paragraph, which wraps across two lines."
    )
    assert docs["beta"] == "A list follows:\n\n* one item that wraps\n* a second item"


def test_field_docs_is_empty_without_a_fields_section() -> None:
    @dataclass(frozen=True)
    class Bare:
        """No fields section here."""

        value: int = field(default=0)

    assert field_docs(Bare) == {}


def test_plain_text_strips_docstring_markup() -> None:
    assert plain_text("see :attr:`ScoringWeights.pass_weight`") == "see ScoringWeights.pass_weight"
    assert plain_text("see :func:`~zicato.board.split.split_board`") == "see split_board"
    assert plain_text("``True`` and *emphasis* and **strong**") == "True and emphasis and strong"
    # a bullet asterisk and a multiplication sign are left alone.
    assert plain_text("* item\nbest_of_n × entries") == "* item\nbest_of_n × entries"


def test_render_default_reads_as_the_operator_sees_it() -> None:
    assert render_default(True) == "on"
    assert render_default(False) == "off"
    assert render_default(None) == "unset"
    assert render_default(0.01) == "0.01"
    assert render_default(16) == "16"
    assert render_default("mechanical") == "mechanical"
    assert render_default(("pytest", "tests/", "-q")) == "pytest tests/ -q"
    assert render_default({"warning": 3.0, "info": 1.0}) == '{"info": 1.0, "warning": 3.0}'


def test_knob_paths_prefix_nested_configs_by_their_field_path() -> None:
    paths = knob_paths()
    assert paths[ScoringWeights] == ""
    assert paths[LadderConfig] == "overfitting.ladder."


def test_knob_help_keys_by_contract_path_and_renders_the_field_default() -> None:
    served = knob_help()
    assert served["overfitting.ladder.threshold"]["default"] == "unset"
    assert served["overfitting.holdout_fraction"]["default"] == "0.3"
    assert served["regression_test_command"]["default"] == "pytest tests/ -q"
    assert served["pass_rate_monotonicity"]["default"] == "on"
    assert "Ladder" in served["overfitting.ladder.enabled"]["help"]
