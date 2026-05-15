r"""Programmatic factory helpers for :class:`~zicato.core.Expectation`.

The board JSONL format encodes matcher specs as a single string field on
:class:`~zicato.core.Expectation` (one wire shape per matcher kind). This
module exposes two namespaced factory classes — :class:`Predicate` and
:class:`Rubric` — so operators authoring boards in Python can spell

    Predicate.regex(r"answered \d+ questions")
    Rubric.judge("Score clarity 0-10", threshold=7.0)

instead of hand-constructing ``Expectation(kind=..., spec=...)`` calls
and remembering which spec shape each kind expects. The returned values
are plain :class:`Expectation` instances; nothing about these helpers
extends the wire schema.

Why static-method classes rather than free functions
----------------------------------------------------

Namespacing the helpers keeps the public surface tight: a single
``from zicato.board.predicates import Predicate, Rubric`` brings in the
whole library, and ``Predicate.contains`` reads better at the call site
than ``predicate_contains`` would. The classes are never instantiated.
"""

from __future__ import annotations

import json
from typing import Any

from zicato.core.types import Expectation, ExpectationFiresOn


class Predicate:
    """Static factory helpers for the deterministic expectation kinds.

    The four classmethods cover the matchers that do not need an LLM:
    substring, regex, JSON schema, and dotted-path Python callable. All
    return a fully-formed :class:`Expectation` ready to attach to a
    :class:`~zicato.core.BoardEntry`.

    The class itself is never instantiated; the helpers are
    ``staticmethod``s for the namespacing.
    """

    def __new__(cls) -> Predicate:  # pragma: no cover — defensive
        raise TypeError("Predicate is a namespace of static helpers; do not instantiate.")

    @staticmethod
    def contains(substring: str, *, fires_on: ExpectationFiresOn = "final_output") -> Expectation:
        """Pass iff ``final_output`` contains ``substring`` (case-sensitive).

        Empty substrings are rejected at matcher time (see
        :func:`zicato.board.matchers._eval_expected_text`), so we leave
        the runtime check to the dispatcher rather than duplicating it
        here — the operator should hear about the typo at run time
        regardless of which construction path produced the expectation.
        """
        return Expectation(kind="expected_text", spec=substring, fires_on=fires_on)

    @staticmethod
    def regex(pattern: str, *, fires_on: ExpectationFiresOn = "final_output") -> Expectation:
        """Pass iff ``re.search(pattern, final_output)`` matches."""
        return Expectation(kind="regex", spec=pattern, fires_on=fires_on)

    @staticmethod
    def schema(
        schema_dict: dict[str, Any],
        *,
        fires_on: ExpectationFiresOn = "final_output",
    ) -> Expectation:
        """Pass iff ``json.loads(final_output)`` validates against ``schema_dict``.

        The schema is JSON-serialised eagerly so the resulting
        :class:`Expectation` round-trips through the board JSONL writer
        without further plumbing. ``sort_keys=True`` is used so two
        equivalent schemas produce identical specs.
        """
        spec = json.dumps(schema_dict, sort_keys=True)
        return Expectation(kind="json_schema", spec=spec, fires_on=fires_on)

    @staticmethod
    def python(dotted_path: str, *, fires_on: ExpectationFiresOn = "final_output") -> Expectation:
        """Pass iff the imported ``dotted_path`` callable returns ``True``.

        The dotted path must resolve to a callable accepting a
        :class:`~zicato.core.RunResult` and returning ``bool`` (sync or
        async — see :func:`zicato.board.matchers._eval_predicate`).
        """
        return Expectation(kind="predicate", spec=dotted_path, fires_on=fires_on)


class Rubric:
    """Static factory helpers for the built-in LLM-as-judge matcher.

    The single :meth:`judge` factory builds an :class:`Expectation` of
    kind ``"rubric"`` whose ``spec`` is a JSON document carrying the
    operator-supplied rubric text, the score threshold (or ``None`` for
    advisory grading), and the numerical scale. The runtime side is
    :func:`zicato.board.rubric.evaluate_rubric_judge`.
    """

    def __new__(cls) -> Rubric:  # pragma: no cover — defensive
        raise TypeError("Rubric is a namespace of static helpers; do not instantiate.")

    @staticmethod
    def judge(
        rubric_text: str,
        *,
        threshold: float | None = None,
        scale: tuple[float, float] = (0.0, 10.0),
        fires_on: ExpectationFiresOn = "final_output",
    ) -> Expectation:
        """Build a ``"rubric"``-kind :class:`Expectation`.

        Parameters
        ----------
        rubric_text:
            Operator-supplied rubric. Free-form prose; the runtime side
            embeds it verbatim into the judge's user prompt.
        threshold:
            Minimum score on ``scale`` for the expectation to pass.
            ``None`` makes the expectation advisory — it always passes
            and the score lands in :attr:`ExpectationResult.detail` for
            inspection.
        scale:
            ``(lo, hi)`` numerical bounds the judge is told to score on.
            Defaults to a familiar ``0.0 to 10.0``.
        fires_on:
            Whether the expectation fires on the final output or the
            full conversation transcript. The rubric judge consumes the
            transcript when present (see
            :func:`zicato.board.rubric.evaluate_rubric_judge`); this
            field only steers which slice the dispatcher would normally
            select when sharing infrastructure with the deterministic
            matchers.
        """
        if scale[0] >= scale[1]:
            raise ValueError(f"Rubric.judge: scale must be (lo, hi) with lo < hi, got {scale!r}")
        if threshold is not None and not (scale[0] <= threshold <= scale[1]):
            raise ValueError(f"Rubric.judge: threshold {threshold} is outside scale {scale!r}")
        payload = {
            "rubric": rubric_text,
            "threshold": threshold,
            "scale": [float(scale[0]), float(scale[1])],
        }
        # ``sort_keys=True`` so the spec text is canonical and two
        # equivalent rubrics produce byte-identical Expectations.
        spec = json.dumps(payload, sort_keys=True)
        return Expectation(kind="rubric", spec=spec, fires_on=fires_on)


__all__ = ["Predicate", "Rubric"]
