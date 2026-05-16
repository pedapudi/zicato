r"""Programmatic factory helpers for :class:`~zicato.core.Expectation`.

The board JSONL format encodes matcher specs as a single string field on
:class:`~zicato.core.Expectation` (one wire shape per matcher kind). This
module exposes two namespaced factory classes — :class:`Predicate` and
:class:`Rubric` — so operators authoring boards in Python can spell

    Predicate.regex(r"answered \d+ questions")
    Rubric.score("Score clarity 0-10", threshold=7.0)

instead of hand-constructing ``Expectation(kind=..., spec=...)`` calls
and remembering which spec shape each kind expects. The returned values
are plain :class:`Expectation` instances; nothing about these helpers
extends the wire schema.

Vocabulary
----------

``Predicate`` and ``Rubric`` are the two OUTCOME-check families — they
grade a finished run post-hoc and compile to an
:class:`~zicato.core.Expectation`. PROCESS checks — assertions about how
a run unfolds *while it is still running* — are a separate family,
:class:`zicato.board.judges.Judge`, which compiles to a
:class:`~zicato.core.JudgeSpec`.

Every choice field on these helpers is a typed enum
(:class:`~zicato.core.ExpectationKind`, :class:`~zicato.core.OutputScope`)
rather than a bare string, so there are no magic strings at any call
site. Because those enums subclass ``str``, the produced
:class:`~zicato.core.Expectation` still round-trips through JSON with no
converter.

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

from zicato.core.types import Expectation, ExpectationKind, OutputScope


class Predicate:
    """Static factory helpers for the deterministic expectation kinds.

    The four classmethods cover the OUTCOME matchers that do not need an
    LLM: substring, regex, JSON schema, and dotted-path Python callable.
    All return a fully-formed :class:`~zicato.core.Expectation` ready to
    attach to a :class:`~zicato.core.BoardEntry`.

    The class itself is never instantiated; the helpers are
    ``staticmethod``s for the namespacing.
    """

    def __new__(cls) -> Predicate:  # pragma: no cover — defensive
        raise TypeError("Predicate is a namespace of static helpers; do not instantiate.")

    @staticmethod
    def contains(substring: str, *, reads: OutputScope = OutputScope.FINAL) -> Expectation:
        """Pass iff the read slice contains ``substring`` (case-sensitive).

        Empty substrings are rejected at matcher time (see
        :func:`zicato.board.matchers._eval_expected_text`), so we leave
        the runtime check to the dispatcher rather than duplicating it
        here — the operator should hear about the typo at run time
        regardless of which construction path produced the expectation.
        """
        return Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec=substring, reads=reads)

    @staticmethod
    def regex(pattern: str, *, reads: OutputScope = OutputScope.FINAL) -> Expectation:
        """Pass iff ``re.search(pattern, <read slice>)`` matches."""
        return Expectation(kind=ExpectationKind.REGEX, spec=pattern, reads=reads)

    @staticmethod
    def schema(
        schema_dict: dict[str, Any],
        *,
        reads: OutputScope = OutputScope.FINAL,
    ) -> Expectation:
        """Pass iff ``json.loads(<read slice>)`` validates against ``schema_dict``.

        The schema is JSON-serialised eagerly so the resulting
        :class:`Expectation` round-trips through the board JSONL writer
        without further plumbing. ``sort_keys=True`` is used so two
        equivalent schemas produce identical specs.
        """
        spec = json.dumps(schema_dict, sort_keys=True)
        return Expectation(kind=ExpectationKind.JSON_SCHEMA, spec=spec, reads=reads)

    @staticmethod
    def python(dotted_path: str, *, reads: OutputScope = OutputScope.FINAL) -> Expectation:
        """Pass iff the imported ``dotted_path`` callable returns ``True``.

        The dotted path must resolve to a callable accepting a
        :class:`~zicato.core.RunResult` and returning ``bool`` (sync or
        async — see :func:`zicato.board.matchers._eval_predicate`).
        """
        return Expectation(kind=ExpectationKind.PREDICATE, spec=dotted_path, reads=reads)


class Rubric:
    """Static factory helpers for the built-in LLM-as-judge OUTCOME matcher.

    The single :meth:`score` factory builds an
    :class:`~zicato.core.Expectation` of kind
    :attr:`~zicato.core.ExpectationKind.RUBRIC` whose ``spec`` is a JSON
    document carrying the operator-supplied rubric text, the score
    threshold (or ``None`` for advisory grading), and the numerical
    scale. The runtime side is
    :func:`zicato.board.rubric.evaluate_rubric_judge`.

    ``Rubric.score`` is still an OUTCOME check despite using an LLM — it
    grades the finished run. It is not to be confused with the PROCESS
    family :class:`zicato.board.judges.Judge`, which observes a run as it
    unfolds.
    """

    def __new__(cls) -> Rubric:  # pragma: no cover — defensive
        raise TypeError("Rubric is a namespace of static helpers; do not instantiate.")

    @staticmethod
    def score(
        rubric_text: str,
        *,
        threshold: float | None = None,
        scale: tuple[float, float] = (0.0, 10.0),
        reads: OutputScope = OutputScope.FINAL,
    ) -> Expectation:
        """Build a :attr:`~zicato.core.ExpectationKind.RUBRIC`-kind :class:`Expectation`.

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
        reads:
            Which slice of the run to grade (see
            :class:`~zicato.core.OutputScope`). The rubric judge consumes
            the transcript when present (see
            :func:`zicato.board.rubric.evaluate_rubric_judge`); this
            field steers which slice the dispatcher would normally select
            when sharing infrastructure with the deterministic matchers.
        """
        if scale[0] >= scale[1]:
            raise ValueError(f"Rubric.score: scale must be (lo, hi) with lo < hi, got {scale!r}")
        if threshold is not None and not (scale[0] <= threshold <= scale[1]):
            raise ValueError(f"Rubric.score: threshold {threshold} is outside scale {scale!r}")
        payload = {
            "rubric": rubric_text,
            "threshold": threshold,
            "scale": [float(scale[0]), float(scale[1])],
        }
        # ``sort_keys=True`` so the spec text is canonical and two
        # equivalent rubrics produce byte-identical Expectations.
        spec = json.dumps(payload, sort_keys=True)
        return Expectation(kind=ExpectationKind.RUBRIC, spec=spec, reads=reads)


__all__ = ["Predicate", "Rubric"]
