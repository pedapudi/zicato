"""Async evaluator for the five expectation kinds.

Every kind dispatches through :func:`evaluate_expectation` and returns a
uniform :class:`~zicato.core.ExpectationResult`. The dispatcher is async
because the ``judge`` kind needs the auxiliary LLM callable, and the
``predicate`` kind tolerates async predicates by design so projects can
write predicates that hit their own backends.

Matcher dispatch
----------------

* ``"predicate"``
    ``spec`` is a dotted Python path. The dispatcher imports it, calls
    it with the :class:`~zicato.core.RunResult`, and awaits the result
    if it is a coroutine. The callable must return :class:`bool`.

* ``"expected_text"``
    Substring containment check against
    :attr:`RunResult.final_output`. Empty ``spec`` is rejected to catch
    operator typos.

* ``"regex"``
    ``re.search`` against :attr:`RunResult.final_output`, compiled with
    :data:`re.DOTALL` so ``.`` spans newlines. Anchoring is up to the
    operator (the regex is matched anywhere unless ``^`` / ``$`` are
    present).

* ``"json_schema"``
    The final output is parsed as JSON, then validated against the
    schema in ``spec``. Non-JSON output, or output that fails schema
    validation, fails. ``jsonschema`` is the validator.

* ``"judge"``
    ``spec`` is a dotted Python path to a function returning
    ``{"system": str, "user_template": str}``. The dispatcher renders
    the user template with ``.format(result=...)``, calls
    ``aux_call_llm(system, user, model)``, and expects a JSON response
    of shape ``{"pass": bool, "reason": str}``. The judge never sees
    the harness callable — collusion-proofing happens because the
    aux callable is distinct (enforced by the workspace helper).

Returned :class:`ExpectationResult.detail` carries enough information
to debug a failing expectation without re-running it: regex match span,
schema-validation error path, judge rationale, etc.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable

import jsonschema

from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.types import Expectation, ExpectationResult, RunResult


def _import_dotted(path: str) -> object:
    """Import a dotted path like ``pkg.module.attr`` and return the attr."""
    if "." not in path:
        raise ValueError(f"dotted path {path!r} has no module component; expected 'pkg.mod.attr'")
    module_path, _, attr_name = path.rpartition(".")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ValueError(
            f"dotted path {path!r}: module {module_path!r} has no attribute {attr_name!r}"
        ) from exc


async def _eval_predicate(expectation: Expectation, result: RunResult) -> ExpectationResult:
    try:
        fn = _import_dotted(expectation.spec)
    except (ImportError, ValueError) as exc:
        return ExpectationResult(
            kind="predicate",
            passed=False,
            detail=f"predicate import failed: {exc}",
        )
    if not callable(fn):
        return ExpectationResult(
            kind="predicate",
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
            kind="predicate",
            passed=False,
            detail=f"predicate raised: {type(exc).__name__}: {exc}",
        )
    if not isinstance(outcome, bool):
        return ExpectationResult(
            kind="predicate",
            passed=False,
            detail=(
                f"predicate {expectation.spec!r} returned {type(outcome).__name__}, expected bool"
            ),
        )
    return ExpectationResult(
        kind="predicate",
        passed=outcome,
        detail="" if outcome else "predicate returned False",
    )


def _eval_expected_text(expectation: Expectation, result: RunResult) -> ExpectationResult:
    spec = expectation.spec
    if spec == "":
        return ExpectationResult(
            kind="expected_text",
            passed=False,
            detail="expected_text spec is empty",
        )
    passed = spec in result.final_output
    detail = "" if passed else f"expected substring {spec!r} not found in final_output"
    return ExpectationResult(kind="expected_text", passed=passed, detail=detail)


def _eval_regex(expectation: Expectation, result: RunResult) -> ExpectationResult:
    try:
        pattern = re.compile(expectation.spec, re.DOTALL)
    except re.error as exc:
        return ExpectationResult(
            kind="regex",
            passed=False,
            detail=f"invalid regex {expectation.spec!r}: {exc}",
        )
    match = pattern.search(result.final_output)
    if match is None:
        return ExpectationResult(
            kind="regex",
            passed=False,
            detail=f"regex {expectation.spec!r} did not match final_output",
        )
    return ExpectationResult(
        kind="regex",
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
            kind="json_schema",
            passed=False,
            detail=f"schema spec is not valid JSON: {exc.msg}",
        )
    try:
        payload = json.loads(result.final_output)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind="json_schema",
            passed=False,
            detail=f"final_output is not valid JSON: {exc.msg}",
        )
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return ExpectationResult(
            kind="json_schema",
            passed=False,
            detail=f"schema validation failed at {path}: {exc.message}",
        )
    except jsonschema.SchemaError as exc:
        return ExpectationResult(
            kind="json_schema",
            passed=False,
            detail=f"schema is itself invalid: {exc.message}",
        )
    return ExpectationResult(kind="json_schema", passed=True, detail="")


async def _eval_judge(
    expectation: Expectation,
    result: RunResult,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]] | None,
) -> ExpectationResult:
    if aux_call_llm is None:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail="judge expectation requires aux_call_llm but none was provided",
        )
    try:
        prompt_factory = _import_dotted(expectation.spec)
    except (ImportError, ValueError) as exc:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge prompt factory import failed: {exc}",
        )
    if not callable(prompt_factory):
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge spec {expectation.spec!r} is not callable",
        )
    try:
        prompts = prompt_factory()
        if inspect.isawaitable(prompts):
            prompts = await prompts
    except Exception as exc:  # noqa: BLE001
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge prompt factory raised: {type(exc).__name__}: {exc}",
        )
    if not isinstance(prompts, dict):
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=(
                f"judge prompt factory returned {type(prompts).__name__}, "
                "expected dict with 'system' and 'user_template'"
            ),
        )
    system = prompts.get("system")
    user_template = prompts.get("user_template")
    if not isinstance(system, str) or not isinstance(user_template, str):
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail="judge prompt factory must return {'system': str, 'user_template': str}",
        )
    try:
        user = user_template.format(result=result)
    except (KeyError, IndexError) as exc:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge user_template format failed: {exc}",
        )
    try:
        # Model name is opaque to zicato; the aux callable interprets
        # it. We pass an empty string to keep this dispatcher
        # model-agnostic; configuration of which model the judge runs
        # against lives on the aux callable.
        raw = await asyncio.wait_for(aux_call_llm(system, user, ""), timeout=aux_call_timeout_s())
    except TimeoutError:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail="judge_timeout",
        )
    except Exception as exc:  # noqa: BLE001
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"aux_call_llm raised: {type(exc).__name__}: {exc}",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge response is not valid JSON: {exc.msg}",
        )
    if not isinstance(parsed, dict) or "pass" not in parsed:
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail="judge response must be {'pass': bool, 'reason': str}",
        )
    passed_value = parsed.get("pass")
    reason = parsed.get("reason", "")
    if not isinstance(passed_value, bool):
        return ExpectationResult(
            kind="judge",
            passed=False,
            detail=f"judge 'pass' field is {type(passed_value).__name__}, expected bool",
        )
    return ExpectationResult(
        kind="judge",
        passed=passed_value,
        detail=str(reason) if reason else "",
    )


async def evaluate_expectation(
    expectation: Expectation,
    result: RunResult,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]] | None = None,
) -> ExpectationResult:
    """Dispatch on ``expectation.kind`` and return an :class:`ExpectationResult`.

    Parameters
    ----------
    expectation:
        The matcher to evaluate.
    result:
        The run result the matcher fires against. For multi-turn
        entries, the caller is responsible for selecting which slice of
        the transcript to pass in :attr:`RunResult.final_output`.
    aux_call_llm:
        Required for ``"judge"`` expectations; ignored for the rest.
        Must be the auxiliary callable from
        :class:`~zicato.core.RuntimeConfig` (the harness callable would
        invite collusion — the workspace helper enforces distinctness).

    Returns
    -------
    ExpectationResult
        Carries ``passed`` and a human-readable ``detail`` string. The
        dispatcher never raises for matcher-internal errors; it
        captures them in :attr:`ExpectationResult.detail` and flags
        ``passed=False`` so the reducer can record the failure shape
        rather than crashing the run.
    """
    if expectation.kind == "predicate":
        return await _eval_predicate(expectation, result)
    if expectation.kind == "expected_text":
        return _eval_expected_text(expectation, result)
    if expectation.kind == "regex":
        return _eval_regex(expectation, result)
    if expectation.kind == "json_schema":
        return _eval_json_schema(expectation, result)
    if expectation.kind == "judge":
        return await _eval_judge(expectation, result, aux_call_llm)
    if expectation.kind == "rubric":
        # Local import keeps the matchers module decoupled from the
        # rubric module's prompt strings — anyone hot-swapping the
        # rubric judge implementation only edits :mod:`zicato.board.rubric`.
        from zicato.board.rubric import evaluate_rubric_judge  # noqa: PLC0415

        return await evaluate_rubric_judge(expectation, result, aux_call_llm)
    # Literal-typed; belt-and-braces for forward compatibility.
    raise ValueError(f"unknown expectation kind {expectation.kind!r}")


__all__ = ["evaluate_expectation"]
