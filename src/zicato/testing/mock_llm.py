"""Deterministic ``CallLLM`` doubles for tests.

The zicato runtime threads two LLM callables through every flow (see
:class:`zicato.core.types.RuntimeConfig`): one drives the inner harness,
one drives the auxiliary path (emulator / proposer / judge / analysis).
The shape is fixed by :data:`zicato.core.types.CallLLM`::

    Callable[[str, str, str], Awaitable[str]]
        # (system, user, model) -> response

That is the only contract this module targets. Every double here
implements ``__call__`` as an ``async`` method so it is interchangeable
with a real LLM client wrapper at call sites.

Three flavors cover the practical needs of zicato's downstream tests:

* :class:`CannedCallLLM` — fixed ordered list of responses; useful when
  the test knows the call order (single-turn entries, scripted
  multi-turn entries, proposer happy-path tests).
* :class:`RecordingCallLLM` — wraps an inner callable and records every
  ``(system, user, model)`` triple plus the response it produced. Used
  to assert ON the prompts a module sends.
* :class:`ScriptedCallLLM` — dispatch on ``(system_substring,
  user_substring)`` rules. Convenient when one test exercises a flow
  that mixes emulator and inner-harness calls and the test does not
  want to encode the interleave order.

All three keep their wire-level contract intentionally narrow — no
streaming, no retries, no model routing. Tests that need richer
behavior compose these doubles with their own glue.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


class CannedCallLLM:
    """Returns the next response from a fixed list on each call.

    Construction is cheap and side-effect free. Each ``__call__``
    advances an internal index; once exhausted the next call raises
    :class:`RuntimeError`. The error message names how many responses
    the double was constructed with so a misconfigured test surfaces
    immediately rather than silently re-using the last response.

    Parameters
    ----------
    responses:
        The exact strings to return, in order.
    model:
        Free-text identifier surfaced in :attr:`model`. Not inspected at
        call time — purely informational so a test can assert that the
        double's nominal identity is what it intended.

    Notes
    -----
    The doubles are not thread-safe; tests construct one per logical
    consumer. The ``async def __call__`` signature exists so the type
    matches :data:`zicato.core.types.CallLLM` and so call sites can use
    ``await`` uniformly across real and faked LLMs.
    """

    def __init__(self, responses: list[str], model: str = "mock") -> None:
        # Copy so callers mutating the source list post-construction do
        # not change the script the double will replay.
        self._responses = list(responses)
        self._index = 0
        self.model = model

    async def __call__(self, system: str, user: str, model: str) -> str:
        if self._index >= len(self._responses):
            raise RuntimeError(
                f"CannedCallLLM exhausted after {len(self._responses)} "
                f"response(s); test issued an extra call with "
                f"system={system!r}, user={user!r}, model={model!r}"
            )
        response = self._responses[self._index]
        self._index += 1
        return response


class RecordingCallLLM:
    """Records every call's inputs and outputs while delegating to an inner callable.

    Used to assert that a module-under-test sent the system / user /
    model strings the test expected. The inner callable provides the
    response — any :data:`zicato.core.types.CallLLM`-shaped callable
    works, including a :class:`CannedCallLLM`.

    Parameters
    ----------
    inner:
        The callable that produces the actual response. Must be async
        and match the :data:`zicato.core.types.CallLLM` shape. The
        wrapper does not validate the shape — it forwards args verbatim.

    Notes
    -----
    Recording happens AFTER the inner call returns successfully. If the
    inner raises, no entry is appended; tests asserting "the module
    called the LLM N times before failing" should wrap the inner
    callable to record-on-entry instead.
    """

    def __init__(self, inner: Callable[[str, str, str], Awaitable[str]]) -> None:
        self._inner = inner
        self._calls: list[dict[str, str]] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        response = await self._inner(system, user, model)
        self._calls.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "response": response,
            }
        )
        return response

    @property
    def calls(self) -> list[dict[str, str]]:
        """The recorded calls, in order.

        Returns a reference to the underlying list so tests can use
        common patterns like ``assert len(llm.calls) == 3``. The list
        is owned by the recorder; tests must not mutate it.
        """
        return self._calls


class ScriptedCallLLM:
    """Dispatches on (system_substring, user_substring) rules.

    Each rule is a ``(system_substring, user_substring, response)``
    triple. On every call, the rules are scanned in order and the first
    rule whose substrings both appear in the call's ``system`` and
    ``user`` arguments wins. An empty substring is treated as a
    wildcard for that side — so ``("", "", "default")`` matches every
    call and is useful as a final fallback rule.

    Parameters
    ----------
    rules:
        Ordered list of ``(system_substring, user_substring, response)``
        triples. The double does not deduplicate or reorder.

    Raises
    ------
    RuntimeError
        On a call that matches no rule. The error names the unmatched
        ``(system, user)`` so a misconfigured test points at the exact
        call site.
    """

    def __init__(self, rules: list[tuple[str, str, str]]) -> None:
        # Copy so post-construction list mutation does not change
        # dispatch behavior.
        self._rules = list(rules)

    async def __call__(self, system: str, user: str, model: str) -> str:
        for system_sub, user_sub, response in self._rules:
            if system_sub in system and user_sub in user:
                return response
        raise RuntimeError(
            f"ScriptedCallLLM: no rule matched call with "
            f"system={system!r}, user={user!r}, model={model!r}"
        )


__all__ = [
    "CannedCallLLM",
    "RecordingCallLLM",
    "ScriptedCallLLM",
]
