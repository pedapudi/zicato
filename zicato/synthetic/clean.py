"""Clean entry support — drive the cooperative reference agent.

A ``synthetic_clean`` board entry is the inverse of
``synthetic_adversarial``: it drives a deliberately cooperative agent
that should produce no warning/critical drift under a well-behaved
steerer. The expectation is :func:`zicato.synthetic.expectations.
evaluate_no_drift` — pass iff every emitted drift event is INFO
severity (those are observational and routinely emitted by the
steerer's own observation paths).

The cooperative agent class lives upstream at
``goldfive.testkit.adversarial.CleanAgent`` (yes, the module is named
``adversarial`` even though ``CleanAgent`` is its non-adversarial
counterpart — that is the goldfive-side naming, kept here for
contract fidelity). When that testkit is unavailable we fall back to
an inline :class:`_FallbackCleanAgent` that simply echoes a canned
response. The fallback exists so v0 dogfooding can proceed before the
goldfive testkit merges; once the testkit is available the operator
sets ``context['clean_agent_spec']`` on the entry to use a real agent.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
from zicato.synthetic.adversarial import (
    AdversarialResolutionError,
    _final_output_from_outcome,
    _parse_agent_args,
    _run_under_wrap,
    _transcript_from_outcome,
    resolve_adversarial_agent,
)


# Default dotted-path spec for the cooperative reference agent. Overridable
# via ``BoardEntry.context['clean_agent_spec']`` so operators who want to
# wire a project-specific clean agent (e.g. one that uses real tool
# calls) can do so without subclassing the runner.
DEFAULT_CLEAN_AGENT_SPEC = "goldfive.testkit.adversarial:CleanAgent"


class _FallbackCleanAgent:
    """Inline cooperative agent for environments without ``goldfive.testkit``.

    The fallback intentionally implements the simplest agent shape
    ``goldfive.wrap`` accepts — an async callable
    ``(task, session, tools) -> InvocationResult``-shaped object. For
    the fallback path we do not have ``InvocationResult`` either
    (goldfive's testkit is what would normally provide the testing
    surface), so we return a minimal duck-typed object goldfive's
    auto-adapter accepts on the "async callable" branch.

    This fallback is suitable for unit tests where the test driver
    bypasses goldfive entirely (see the test stubs in
    ``tests/test_synthetic_adversarial.py``). It is NOT a production
    substitute for ``goldfive.testkit.adversarial.CleanAgent`` — when
    goldfive's testkit lands the operator should remove the fallback
    by setting ``context['clean_agent_spec']`` explicitly.
    """

    canned_response: str = (
        "Acknowledged. The cooperative reference agent has completed the request."
    )

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Duck-typed minimal result shape. goldfive's auto_adapter
        # tolerates any object exposing ``text`` / ``content`` on the
        # async-callable branch.
        return _FallbackInvocationResult(text=self.canned_response)


class _FallbackInvocationResult:
    """Minimal duck-typed result for the fallback clean agent."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text


def _resolve_clean_agent(entry: BoardEntry) -> Any:
    """Pick the clean-agent class.

    Resolution order:

    1. ``entry.context['clean_agent_spec']`` if non-empty — supports
       operator overrides.
    2. ``DEFAULT_CLEAN_AGENT_SPEC`` (``goldfive.testkit.adversarial:CleanAgent``).
    3. :class:`_FallbackCleanAgent` if neither resolves.

    The fallback path swallows :class:`AdversarialResolutionError`
    rather than re-raising — the *point* of having a fallback is that
    clean entries should not block on the testkit's absence. If the
    operator wants strict resolution they set the spec explicitly,
    which removes the fallback path.
    """
    override = entry.context.get("clean_agent_spec") if entry.context else None
    if override:
        # Explicit overrides are strict — operator-typed strings should
        # surface their resolution errors so the operator can fix them.
        return resolve_adversarial_agent(override)
    try:
        return resolve_adversarial_agent(DEFAULT_CLEAN_AGENT_SPEC)
    except AdversarialResolutionError:
        return _FallbackCleanAgent


async def run_clean_entry(
    entry: BoardEntry,
    sinks: list,
    config: RuntimeConfig,
) -> RunResult:
    """Run a ``synthetic_clean`` board entry under ``goldfive.wrap``.

    Same shape as :func:`zicato.synthetic.adversarial.run_adversarial_entry`
    but resolves the cooperative reference agent (see
    :func:`_resolve_clean_agent`) and tolerates the goldfive testkit
    being unavailable by falling back to an inline echo agent.

    Raises
    ------
    AdversarialResolutionError
        Only when ``context['clean_agent_spec']`` is explicitly set
        and cannot be resolved. The default-path fallback never
        raises this.
    ValueError
        If the entry is malformed for this runner.
    """
    if entry.kind != "synthetic_clean":
        raise ValueError(
            f"run_clean_entry called with entry.kind={entry.kind!r}; "
            "expected 'synthetic_clean'"
        )
    if entry.input is None:
        raise ValueError(f"BoardEntry {entry.id!r}: input is required")

    agent_cls = _resolve_clean_agent(entry)
    args, kwargs = _parse_agent_args(entry)
    try:
        agent = agent_cls(*args, **kwargs)
    except TypeError as exc:
        raise AdversarialResolutionError(
            f"resolved clean agent {agent_cls!r} could not be instantiated "
            f"with args={args!r}, kwargs={kwargs!r}: {exc}"
        ) from exc

    run_id = uuid.uuid4().hex
    started = time.monotonic()
    aborted = False
    abort_reason = ""
    final_output = ""
    transcript: tuple[str, ...] = ()

    try:
        outcome = await asyncio.wait_for(
            _run_under_wrap(agent, entry.input, sinks, config),
            timeout=entry.wall_clock_budget_seconds,
        )
    except asyncio.TimeoutError:
        aborted = True
        abort_reason = "wall_clock_budget_exceeded"
    except AdversarialResolutionError:
        raise
    except Exception as exc:
        aborted = True
        abort_reason = f"runner_exception:{type(exc).__name__}"
    else:
        final_output = _final_output_from_outcome(outcome)
        transcript = _transcript_from_outcome(outcome, final_output)

    runtime_ms = int((time.monotonic() - started) * 1000)
    return RunResult(
        run_id=run_id,
        entry_id=entry.id,
        final_output=final_output,
        transcript=transcript,
        runtime_ms=runtime_ms,
        aborted=aborted,
        abort_reason=abort_reason,
    )
