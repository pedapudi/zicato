"""Async evaluator for the five OUTCOME-expectation kinds.

Every kind dispatches through :func:`evaluate_expectation` and returns a
uniform :class:`~zicato.core.ExpectationResult`. The dispatcher is async
because the ``RUBRIC`` kind needs the auxiliary LLM callable, and the
``PREDICATE`` kind tolerates async predicates by design so projects can
write predicates that hit their own backends.

This module covers OUTCOME checks only — the post-hoc grading of a
finished run. PROCESS checks (assertions about how a run unfolds while it
is still running) are carried by :class:`~zicato.core.JudgeSpec` and
evaluated elsewhere.

Matcher dispatch — keyed on the :class:`~zicato.core.ExpectationKind` enum
-------------------------------------------------------------------------

* :attr:`~zicato.core.ExpectationKind.PREDICATE`
    ``spec`` is a dotted Python path. The dispatcher imports it, calls
    it with the :class:`~zicato.core.RunResult`, and awaits the result
    if it is a coroutine. This is BOTH the binary-predicate seam and the
    continuous-SCORER seam: the callable may return a ``bool`` (the
    historical pass/fail), a ``float`` in ``[0, 1]`` (a continuous score),
    or a ``(float, metrics)`` 2-tuple (a score plus a decomposition such
    as ``{"precision": .., "recall": ..}``). See
    :func:`_predicate_outcome_to_result`.

* :attr:`~zicato.core.ExpectationKind.EXPECTED_TEXT`
    Substring containment check against
    :attr:`RunResult.final_output`. Empty ``spec`` is rejected to catch
    operator typos.

* :attr:`~zicato.core.ExpectationKind.REGEX`
    ``re.search`` against :attr:`RunResult.final_output`, compiled with
    :data:`re.DOTALL` so ``.`` spans newlines. Anchoring is up to the
    operator (the regex is matched anywhere unless ``^`` / ``$`` are
    present).

* :attr:`~zicato.core.ExpectationKind.JSON_SCHEMA`
    The final output is parsed as JSON, then validated against the
    schema in ``spec``. Non-JSON output, or output that fails schema
    validation, fails. ``jsonschema`` is the validator.

* :attr:`~zicato.core.ExpectationKind.RUBRIC`
    The built-in LLM-as-judge rubric matcher. ``spec`` is the JSON
    rubric document produced by
    :meth:`zicato.board.predicates.Rubric.score`. Delegates to
    :func:`zicato.board.rubric.evaluate_rubric_judge`. The matcher never
    sees the harness callable — collusion-proofing happens because the
    aux callable is distinct (enforced by the workspace helper).

Returned :class:`ExpectationResult.detail` carries enough information
to debug a failing expectation without re-running it: regex match span,
schema-validation error path, rubric reasoning, etc.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable

import jsonschema

from zicato.core.types import Expectation, ExpectationKind, ExpectationResult, RunResult
from zicato.import_path import import_dotted_path


def _predicate_outcome_to_result(spec: str, outcome: object) -> ExpectationResult:
    """Map a predicate / SCORER callable's return into an :class:`ExpectationResult`.

    The dotted-path :attr:`~zicato.core.ExpectationKind.PREDICATE` seam is
    also the SCORER seam: an operator's callable may return any of three
    shapes, in addition to the historical ``bool``.

    * ``bool`` — the historical binary verdict. ``passed`` is the bit and
      ``score`` is left ``None`` (the reducer derives ``1.0`` / ``0.0``);
      byte-identical to the pre-score path.
    * ``float`` / ``int`` — a CONTINUOUS score. It is clamped to
      ``[0.0, 1.0]`` and recorded as :attr:`ExpectationResult.score`;
      ``passed`` is a display-only derivation (``score > 0.0`` — i.e. the
      entry earned any credit), since the scalar and gate now run on the
      continuous score rather than on this bit.
    * ``(score, metrics)`` — a 2-tuple of ``(float, Mapping[str, float])``.
      The float is the score (clamped as above); the mapping is recorded
      verbatim as :attr:`ExpectationResult.metrics` (e.g.
      ``{"precision": .., "recall": ..}``) so a later aggregation step can
      read the decomposition as numbers. This is how the F1 operator scorer
      feeds precision/recall out to ``loss.json``.

    Any other return type is a contract error and fails the entry with a
    descriptive ``detail`` (mirroring the historical non-bool rejection).
    """
    metrics: dict[str, float] | None = None
    # (score, metrics) tuple form.
    if isinstance(outcome, tuple) and len(outcome) == 2:
        raw_score, raw_metrics = outcome
        if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
            return ExpectationResult(
                kind=ExpectationKind.PREDICATE,
                passed=False,
                detail=(
                    f"scorer {spec!r} returned a 2-tuple whose first element is "
                    f"{type(raw_score).__name__}, expected a float score"
                ),
            )
        if not hasattr(raw_metrics, "items"):
            return ExpectationResult(
                kind=ExpectationKind.PREDICATE,
                passed=False,
                detail=(
                    f"scorer {spec!r} returned a 2-tuple whose second element is "
                    f"{type(raw_metrics).__name__}, expected a metrics mapping"
                ),
            )
        metrics = {str(k): float(v) for k, v in raw_metrics.items()}
        outcome = raw_score

    # bool is the historical path — keep score=None so the reducer derives
    # 1.0/0.0 and the all-bool scalar stays byte-identical. (bool is a
    # subclass of int, so this check MUST precede the float branch.)
    if isinstance(outcome, bool):
        if metrics is not None:
            # A (bool, metrics) tuple is ill-formed for the scorer seam.
            return ExpectationResult(
                kind=ExpectationKind.PREDICATE,
                passed=False,
                detail=f"scorer {spec!r} paired a bool score with metrics; expected a float score",
            )
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=outcome,
            detail="" if outcome else "predicate returned False",
        )

    # float / int continuous score.
    if isinstance(outcome, int | float):
        score = float(outcome)
        # Clamp here too so a downstream consumer that reads
        # ExpectationResult.score directly (not via the reducer) sees a
        # well-formed [0,1] value; the reducer clamps again defensively.
        clamped = max(0.0, min(1.0, score)) if score == score else 0.0  # score==score guards NaN
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=clamped > 0.0,
            detail=f"scorer returned {score:.6f}" if score == score else "scorer returned NaN",
            score=clamped,
            metrics=metrics,
        )

    return ExpectationResult(
        kind=ExpectationKind.PREDICATE,
        passed=False,
        detail=(
            f"predicate {spec!r} returned {type(outcome).__name__}, "
            f"expected bool, float, or (float, metrics)"
        ),
    )


async def _eval_predicate(expectation: Expectation, result: RunResult) -> ExpectationResult:
    try:
        fn = import_dotted_path(expectation.spec, label=f"dotted path {expectation.spec!r}")
    except (ImportError, ValueError) as exc:
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=False,
            detail=f"predicate import failed: {exc}",
        )
    if not callable(fn):
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=False,
            detail=f"predicate {expectation.spec!r} is not callable",
        )
    try:
        maybe_awaitable = fn(result)
        if inspect.isawaitable(maybe_awaitable):
            outcome = await maybe_awaitable
        else:
            outcome = maybe_awaitable
    except Exception as exc:  # noqa: BLE001 — surface to caller as detail
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=False,
            detail=f"predicate raised: {type(exc).__name__}: {exc}",
        )
    return _predicate_outcome_to_result(expectation.spec, outcome)


def _eval_expected_text(expectation: Expectation, result: RunResult) -> ExpectationResult:
    spec = expectation.spec
    if spec == "":
        return ExpectationResult(
            kind=ExpectationKind.EXPECTED_TEXT,
            passed=False,
            detail="expected_text spec is empty",
        )
    passed = spec in result.final_output
    detail = "" if passed else f"expected substring {spec!r} not found in final_output"
    return ExpectationResult(kind=ExpectationKind.EXPECTED_TEXT, passed=passed, detail=detail)


def _eval_regex(expectation: Expectation, result: RunResult) -> ExpectationResult:
    try:
        pattern = re.compile(expectation.spec, re.DOTALL)
    except re.error as exc:
        return ExpectationResult(
            kind=ExpectationKind.REGEX,
            passed=False,
            detail=f"invalid regex {expectation.spec!r}: {exc}",
        )
    match = pattern.search(result.final_output)
    if match is None:
        return ExpectationResult(
            kind=ExpectationKind.REGEX,
            passed=False,
            detail=f"regex {expectation.spec!r} did not match final_output",
        )
    return ExpectationResult(
        kind=ExpectationKind.REGEX,
        passed=True,
        detail=f"matched at [{match.start()}:{match.end()}]",
    )


def _eval_json_schema(expectation: Expectation, result: RunResult) -> ExpectationResult:
    # spec is either a JSON string of the schema, or (when constructed
    # in-memory) already a JSON-shaped string. We always treat it as
    # JSON-text here because :class:`Expectation` typed it as ``str``.
    try:
        schema = json.loads(expectation.spec)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind=ExpectationKind.JSON_SCHEMA,
            passed=False,
            detail=f"schema spec is not valid JSON: {exc.msg}",
        )
    try:
        payload = json.loads(result.final_output)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind=ExpectationKind.JSON_SCHEMA,
            passed=False,
            detail=f"final_output is not valid JSON: {exc.msg}",
        )
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ExpectationResult(
            kind=ExpectationKind.JSON_SCHEMA,
            passed=False,
            detail=f"schema validation failed at {path}: {exc.message}",
        )
    except jsonschema.SchemaError as exc:
        return ExpectationResult(
            kind=ExpectationKind.JSON_SCHEMA,
            passed=False,
            detail=f"schema is itself invalid: {exc.message}",
        )
    return ExpectationResult(kind=ExpectationKind.JSON_SCHEMA, passed=True, detail="")


async def _eval_rubric(
    expectation: Expectation,
    result: RunResult,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]] | None,
) -> ExpectationResult:
    """Dispatch the :attr:`~zicato.core.ExpectationKind.RUBRIC` kind.

    Thin forwarder to :func:`zicato.board.rubric.evaluate_rubric_judge`.
    The local import keeps this module decoupled from the rubric module's
    prompt strings — anyone hot-swapping the rubric judge implementation
    only edits :mod:`zicato.board.rubric`.
    """
    from zicato.board.rubric import evaluate_rubric_judge  # noqa: PLC0415

    return await evaluate_rubric_judge(expectation, result, aux_call_llm)


async def evaluate_expectation(
    expectation: Expectation,
    result: RunResult,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]] | None = None,
) -> ExpectationResult:
    """Dispatch on ``expectation.kind`` and return an :class:`ExpectationResult`.

    Parameters
    ----------
    expectation:
        The OUTCOME matcher to evaluate.
    result:
        The run result the matcher fires against. For multi-turn
        entries, the caller is responsible for selecting which slice of
        the transcript to pass in :attr:`RunResult.final_output`.
    aux_call_llm:
        Required for :attr:`~zicato.core.ExpectationKind.RUBRIC`
        expectations; ignored for the rest. Must be the auxiliary
        callable from :class:`~zicato.core.RuntimeConfig` (the harness
        callable would invite collusion — the workspace helper enforces
        distinctness).

    Returns
    -------
    ExpectationResult
        Carries ``passed`` and a human-readable ``detail`` string. The
        dispatcher never raises for matcher-internal errors; it
        captures them in :attr:`ExpectationResult.detail` and flags
        ``passed=False`` so the reducer can record the failure shape
        rather than crashing the run.

    Raises
    ------
    ValueError
        If ``expectation.kind`` is not a recognised
        :class:`~zicato.core.ExpectationKind`.
    """
    # ``ExpectationKind`` subclasses ``str``; coerce so a producer that
    # passed the bare wire token still dispatches correctly.
    kind = ExpectationKind(expectation.kind)
    if kind is ExpectationKind.PREDICATE:
        return await _eval_predicate(expectation, result)
    if kind is ExpectationKind.EXPECTED_TEXT:
        return _eval_expected_text(expectation, result)
    if kind is ExpectationKind.REGEX:
        return _eval_regex(expectation, result)
    if kind is ExpectationKind.JSON_SCHEMA:
        return _eval_json_schema(expectation, result)
    if kind is ExpectationKind.RUBRIC:
        return await _eval_rubric(expectation, result, aux_call_llm)
    # Enum-typed; belt-and-braces for forward compatibility.
    raise ValueError(f"unknown expectation kind {expectation.kind!r}")


__all__ = ["evaluate_expectation"]
