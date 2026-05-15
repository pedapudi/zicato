"""Built-in LLM-as-judge rubric matcher.

The :func:`evaluate_rubric_judge` coroutine implements the runtime side
of the ``"rubric"`` expectation kind built by
:meth:`zicato.board.predicates.Rubric.judge`. It renders the operator's
rubric into a system+user prompt pair, calls the auxiliary LLM, parses
the JSON response into a ``score`` / ``dimensions`` / ``reasoning``
triple, and reports pass/fail against the operator-set threshold.

Failure modes — bad JSON, missing fields, unparseable score, the LLM
call itself raising — are surfaced as ``passed=False``
:class:`~zicato.core.ExpectationResult` instances with a descriptive
:attr:`~zicato.core.ExpectationResult.detail`. The matcher never raises
upward: a buggy judge response should manifest as a failing expectation
on the offending board entry, not as a crash that wedges the whole run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.types import Expectation, ExpectationResult, RunResult

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a rubric judge. The operator-supplied rubric below defines "
    "scoring criteria. Read the agent's transcript and produce a JSON object "
    "with: score (float), dimensions (dict[name, float]), reasoning (string). "
    "Be precise; cite specific transcript lines in reasoning."
)

_USER_TEMPLATE = (
    "## Rubric\n{rubric}\n\n"
    "## Scale\n{lo} to {hi}\n\n"
    "## Transcript\n{transcript}\n\n"
    "Return JSON only."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(raw: str) -> str:
    """Best-effort stripping of Markdown code fences around a JSON payload.

    LLMs frequently wrap structured output in ```json ... ``` fences even
    when explicitly asked for raw JSON. The dispatcher tolerates a few
    common shapes:

    * a leading ``` (with or without a ``json`` language tag) followed by
      the body and a trailing ```
    * extra leading / trailing whitespace
    * no fences at all (returned unchanged)

    Anything else is passed through and will surface as a JSON decode
    error downstream — the goal here is convenience, not bulletproofing.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence; tolerate ``` and ```json (and friends).
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        else:
            text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _select_transcript(result: RunResult) -> str:
    """Pick the slice of the run to hand to the judge.

    Multi-turn runs (``transcript`` is non-empty and has more than one
    entry) get the full conversation joined with newlines. Single-turn
    runs fall back to :attr:`RunResult.final_output` so the judge sees
    the user-facing answer rather than a one-element list.
    """
    if result.transcript and len(result.transcript) > 1:
        return "\n".join(result.transcript)
    return result.final_output


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def evaluate_rubric_judge(
    expectation: Expectation,
    result: RunResult,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]] | None,
) -> ExpectationResult:
    """Evaluate a ``"rubric"``-kind expectation against a :class:`RunResult`.

    Parameters
    ----------
    expectation:
        The expectation to evaluate. ``spec`` must be the JSON document
        produced by :meth:`zicato.board.predicates.Rubric.judge`.
    result:
        The run result to grade.
    aux_call_llm:
        The auxiliary LLM callable. Required — the rubric matcher is an
        LLM-as-judge matcher by definition. Passing ``None`` returns a
        failing :class:`ExpectationResult` rather than raising.

    Returns
    -------
    ExpectationResult
        ``kind="rubric"``. ``passed`` is ``score >= threshold`` when the
        rubric set a threshold, else ``True`` (advisory grading).
        ``detail`` always carries the parsed score and reasoning so the
        journal has something concrete to render.
    """
    if aux_call_llm is None:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail="rubric expectation requires aux_call_llm but none was provided",
        )

    try:
        parsed_spec = json.loads(expectation.spec)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail=f"rubric spec is not valid JSON: {exc.msg}",
        )
    if not isinstance(parsed_spec, dict) or "rubric" not in parsed_spec:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail="rubric spec must be a JSON object with at least 'rubric'",
        )

    rubric_text = parsed_spec["rubric"]
    threshold = parsed_spec.get("threshold")
    scale = parsed_spec.get("scale", [0.0, 10.0])
    if not isinstance(scale, (list, tuple)) or len(scale) != 2:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail=f"rubric spec 'scale' must be [lo, hi], got {scale!r}",
        )
    scale_lo, scale_hi = float(scale[0]), float(scale[1])

    transcript = _select_transcript(result)
    user_prompt = _USER_TEMPLATE.format(
        rubric=rubric_text, lo=scale_lo, hi=scale_hi, transcript=transcript
    )

    try:
        # Model name is opaque to zicato; the aux callable interprets
        # it. An empty string keeps the dispatch model-agnostic.
        raw = await asyncio.wait_for(
            aux_call_llm(_SYSTEM_PROMPT, user_prompt, ""),
            timeout=aux_call_timeout_s(),
        )
    except TimeoutError:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail="rubric_judge_timeout",
        )
    except Exception as exc:  # noqa: BLE001 — surface to caller as detail
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail=f"aux_call_llm raised: {type(exc).__name__}: {exc}",
        )

    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail=f"rubric judge response is not valid JSON: {exc.msg}",
        )
    if not isinstance(data, dict) or "score" not in data:
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail="rubric judge response must be {'score': float, ...}",
        )

    try:
        score = float(data["score"])
    except (TypeError, ValueError):
        return ExpectationResult(
            kind="rubric",
            passed=False,
            detail=f"rubric judge 'score' is not a number: {data.get('score')!r}",
        )

    dimensions_raw = data.get("dimensions", {})
    dimensions: dict[str, float] = {}
    if isinstance(dimensions_raw, dict):
        for name, value in dimensions_raw.items():
            try:
                dimensions[str(name)] = float(value)
            except (TypeError, ValueError):
                # Bad per-dimension scores are advisory noise; we keep
                # the overall result rather than failing the entry.
                continue
    reasoning = str(data.get("reasoning", ""))

    if threshold is None:
        passed = True
    else:
        try:
            threshold_f = float(threshold)
        except (TypeError, ValueError):
            return ExpectationResult(
                kind="rubric",
                passed=False,
                detail=f"rubric spec threshold is not a number: {threshold!r}",
            )
        passed = score >= threshold_f

    detail_parts = [f"score={score:.2f}"]
    if dimensions:
        rendered = ", ".join(f"{k}={v:.2f}" for k, v in dimensions.items())
        detail_parts.append(f"dimensions=[{rendered}]")
    if reasoning:
        detail_parts.append(f"reasoning={reasoning}")
    detail = " ".join(detail_parts)

    return ExpectationResult(kind="rubric", passed=passed, detail=detail)


__all__ = ["evaluate_rubric_judge"]
