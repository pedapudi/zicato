"""Scripted multi-turn driver.

A :class:`ScriptedMultiTurnDriver` walks a
:class:`~zicato.core.BoardEntry` of kind ``"multi_turn_scripted"`` and
plays its pre-written user turns against an inner harness, accumulating
the agent's user-facing replies into a single
:class:`~zicato.core.RunResult`.

Why scripted multi-turn at all
------------------------------

Multi-turn scripted entries are the cheap, deterministic complement to
the emulated ones. The user side is rigidly fixed — turn ``i`` is sent
verbatim regardless of what the agent said — so the same agent
generation produces the same transcript every run, modulo agent
nondeterminism. That is the whole point: scripted entries are
regression tests for multi-turn behaviour rather than exploration of the
agent's response space.

Termination
-----------

The driver stops when any of the following is true:

* All :attr:`BoardEntry.turns` have been consumed.
* :attr:`BoardEntry.max_turns` has been reached.
* The conversation's accumulated wall-clock time exceeds
  :attr:`BoardEntry.wall_clock_budget_seconds`. The budget covers the
  **whole** conversation rather than each turn, so a slow first turn directly
  shrinks the budget the rest of the conversation has to fit in.

When the budget is exceeded mid-conversation, the driver returns a
:class:`RunResult` with ``aborted=True`` and
``abort_reason="wall_clock_budget"``. The final-output and transcript
fields contain whatever the driver collected before the abort.

Harness contract
----------------

The driver expects ``harness`` to expose either:

* an async ``run(user_message: str) -> RunResult``-shaped object whose
  ``final_output`` is the agent's reply, or
* a callable ``call(user_message: str)`` returning the same shape.

The duck-typing is intentional — different adapters (ADK, in-process
stub, etc.) implement the surface in slightly different ways, and the
driver should not require a particular base class. The actual goldfive
``run`` entry point will be wired in by the runner; this driver just
asks for "something callable that returns a per-turn result".
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from zicato.core.types import BoardEntry, RunResult, RuntimeConfig


class ScriptedMultiTurnDriver:
    """Drive a scripted multi-turn entry against an inner harness.

    The driver is intentionally tiny — it owns turn iteration, budget
    accounting, and transcript accumulation. Concerns it does NOT own:
    applying patches, persisting events, computing drift loss. Those happen
    one layer up (the runner) and one layer down (the harness adapter).

    The driver carries no state between :meth:`drive` calls; an
    instance can be reused across entries within one runner pass.
    """

    async def drive(
        self,
        harness: Any,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Play ``entry.turns`` against ``harness`` and return the accumulated result.

        Parameters
        ----------
        harness:
            The inner harness reference. Must expose either an async
            ``run(user_message)`` method or an async ``call(user_message)``
            method. The driver awaits whichever is present.
        entry:
            The scripted multi-turn :class:`BoardEntry`. Must have
            ``kind="multi_turn_scripted"``; the driver asserts this.
        sinks:
            Event-sink list passed through to the harness if it
            accepts it. Treated as opaque by the driver — the
            telemetry / persistence path handles meaning.
        config:
            The :class:`RuntimeConfig` for this zicato instance. The
            driver inspects the seed only when the harness adapter
            accepts it; the LLM callables are not used by the driver
            itself (the agent's callable is on ``harness``).

        Returns
        -------
        RunResult
            ``final_output`` carries the agent's reply on the LAST
            turn played. ``transcript`` is the tuple of every agent
            reply in order. ``runtime_ms`` is the elapsed wall clock.
            ``aborted`` flags whether the budget tripped; the
            ``abort_reason`` is ``"wall_clock_budget"`` when so.
        """
        if entry.kind != "multi_turn_scripted":
            raise ValueError(
                f"ScriptedMultiTurnDriver requires entry.kind=='multi_turn_scripted'; "
                f"got {entry.kind!r} on {entry.id!r}"
            )
        if entry.turns is None or len(entry.turns) == 0:
            raise ValueError(f"ScriptedMultiTurnDriver: {entry.id!r} has no turns to play")
        if entry.max_turns is None or entry.max_turns <= 0:
            raise ValueError(f"ScriptedMultiTurnDriver: {entry.id!r} has no max_turns")

        run_id = uuid.uuid4().hex
        budget_seconds = entry.wall_clock_budget_seconds
        max_turns = entry.max_turns
        scripted_turns = entry.turns
        # The conversation stops when EITHER turns run out OR max_turns
        # is reached — whichever first.
        turn_limit = min(len(scripted_turns), max_turns)

        invoker = _resolve_invoker(harness, sinks, config)

        transcript: list[str] = []
        final_output = ""
        aborted = False
        abort_reason = ""

        start = time.monotonic()
        for turn_index in range(turn_limit):
            elapsed = time.monotonic() - start
            remaining = budget_seconds - elapsed
            if remaining <= 0:
                aborted = True
                abort_reason = "wall_clock_budget"
                break

            user_message = scripted_turns[turn_index].user
            try:
                reply = await asyncio.wait_for(
                    invoker(user_message),
                    timeout=remaining,
                )
            except TimeoutError:
                aborted = True
                abort_reason = "wall_clock_budget"
                break
            except Exception as exc:  # noqa: BLE001
                # The harness blew up. Record what we have and surface
                # the exception via abort_reason — the runner decides
                # how to score it.
                aborted = True
                abort_reason = f"harness_error: {type(exc).__name__}: {exc}"
                break

            reply_str = _coerce_reply(reply)
            transcript.append(reply_str)
            final_output = reply_str

        # Re-check the budget once more — a slow final turn that just
        # fit within ``remaining`` still counts if the overall budget
        # tripped. Tracked here so callers see a consistent abort
        # signal even when the wait_for didn't fire.
        total_elapsed_seconds = time.monotonic() - start
        if not aborted and total_elapsed_seconds > budget_seconds:
            aborted = True
            abort_reason = "wall_clock_budget"

        runtime_ms = int(total_elapsed_seconds * 1000)
        return RunResult(
            run_id=run_id,
            entry_id=entry.id,
            final_output=final_output,
            transcript=tuple(transcript),
            runtime_ms=runtime_ms,
            aborted=aborted,
            abort_reason=abort_reason,
        )


def _resolve_invoker(
    harness: Any,
    sinks: list[Any],
    config: RuntimeConfig,
) -> Callable[[str], Awaitable[Any]]:
    """Return an async callable that takes a user message and returns the agent's reply.

    The driver duck-types ``harness``: any callable named ``run`` or
    ``call`` is accepted, and the sinks / config are forwarded as
    keyword arguments where the callable's signature accepts them.
    Signatures that don't accept ``sinks`` or ``config`` get the bare
    one-positional invocation — adapters opting in to the richer
    surface can take advantage; minimal adapters don't have to.
    """
    method = None
    for name in ("run", "call"):
        candidate = getattr(harness, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        if callable(harness):
            method = harness
        else:
            raise TypeError(
                "harness must expose run(...) or call(...) or be callable; "
                f"got {type(harness).__name__}"
            )

    params: Mapping[str, Any]
    try:
        sig = inspect.signature(method)
        params = sig.parameters
    except (TypeError, ValueError):
        params = {}

    accepts_sinks = "sinks" in params
    accepts_config = "config" in params

    async def _invoke(user_message: str) -> Any:
        kwargs: dict[str, Any] = {}
        if accepts_sinks:
            kwargs["sinks"] = sinks
        if accepts_config:
            kwargs["config"] = config
        outcome = method(user_message, **kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    return _invoke


def _coerce_reply(reply: Any) -> str:
    """Turn whatever the harness handed back into a string.

    Adapters return one of:

    * A bare string — taken as-is.
    * An object with a ``final_output`` attribute (e.g. a goldfive
      ``RunResult``) — that attribute is used.
    * Anything else — coerced with :func:`str`.
    """
    if isinstance(reply, str):
        return reply
    final = getattr(reply, "final_output", None)
    if isinstance(final, str):
        return final
    return str(reply)


async def run_scripted(
    agent: Any,
    entry: BoardEntry,
    sinks: list[Any],
    config: RuntimeConfig,
    run_id: str,
) -> RunResult:
    """Free-function entrypoint over :class:`ScriptedMultiTurnDriver`.

    Provided for harness adapters that want to call a single function
    rather than instantiating the driver themselves. The ``run_id``
    argument is accepted for compatibility with the adapter surface
    but is currently informational — the driver mints its own
    correlation id internally.
    """
    del run_id  # accepted for API parity with run_emulated; not used yet
    driver = ScriptedMultiTurnDriver()
    return await driver.drive(harness=agent, entry=entry, sinks=sinks, config=config)


__all__ = ["ScriptedMultiTurnDriver", "run_scripted"]
