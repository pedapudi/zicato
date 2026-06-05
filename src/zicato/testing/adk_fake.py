"""A scriptable fake ADK model for deterministic proposer-agent tests.

:class:`FakeADKModel` is a :class:`~google.adk.models.BaseLlm` that yields
canned :class:`~google.adk.models.LlmResponse`s from a pre-loaded script —
no network, no real model. It exists to drive
:class:`~zicato.proposer.adk_agent.ADKProposerAgent` (and any other
ADK-agent test) through a deterministic conversation: a tool-call turn
followed by a final JSON turn, multiple tool turns, or a single final
turn, exactly as the test scripts it.

Each script step is one of:

* :class:`FunctionCallTurn(name, args)` — yields a response whose content
  carries a single ``function_call`` part. ADK's runner invokes the named
  tool, appends the tool's ``FunctionResponse``, and re-invokes the model;
  the NEXT script step answers that re-invocation. This is how a test
  proves a :data:`~zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS` tool is
  actually called.
* :class:`TextTurn(text)` — yields a response whose content is a single
  text part (the proposer's final ``{hypothesis, patches}`` JSON, or a
  retry's malformed body).

The model advances through the script one step per
:meth:`generate_content_async` invocation; if the script is exhausted it
keeps yielding the last text turn (or an empty text part) so a stray extra
invocation does not raise.

google-adk guard
----------------
The ADK imports are LOCAL to the methods / factory that need them, so
``import zicato.testing.adk_fake`` (and ``import zicato.testing``) does not
require the optional ``google-adk`` extra — only constructing or running a
:class:`FakeADKModel` does. Tests gate construction behind
``pytest.importorskip("google.adk")``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FunctionCallTurn:
    """A scripted turn that emits one tool (function) call.

    ADK's runner dispatches the named tool with ``args``, appends the
    tool's result as a ``FunctionResponse``, and re-invokes the model — so
    the next script step answers the post-tool re-invocation.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextTurn:
    """A scripted turn that emits a single text part (the final answer)."""

    text: str


#: One step of a :class:`FakeADKModel` script.
ScriptTurn = FunctionCallTurn | TextTurn


def _build_base_llm_class() -> type:
    """Build the :class:`FakeADKModel` class with ADK bases resolved lazily.

    Defined as a factory so the ``BaseLlm`` / response symbols are imported
    only when a fake model is actually constructed — keeping
    ``zicato.testing`` importable without the ``google-adk`` extra.
    """
    from google.adk.models import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    class _FakeADKModel(BaseLlm):
        """A :class:`BaseLlm` that replays a fixed script of canned turns.

        Implements the single abstract method
        :meth:`generate_content_async`. ``model`` is a plain string label
        (the author-owned model identifier in the real path); tests set it
        to whatever they like. The ``script`` is consumed one step per
        invocation; ``invocations`` records the per-call requests so a test
        can assert how many model round-trips a run took.
        """

        model: str = "fake-proposer-model"
        # Pydantic model fields (BaseLlm is a pydantic BaseModel): declare
        # the script + cursor + recorder as fields so assignment validates.
        script: list[Any] = []  # noqa: RUF012 — pydantic field default
        cursor: int = 0
        invocations: list[Any] = []  # noqa: RUF012 — pydantic field default

        async def generate_content_async(
            self,
            llm_request: LlmRequest,
            stream: bool = False,
        ) -> AsyncGenerator[LlmResponse, None]:
            """Yield the next scripted response for this invocation.

            Records ``llm_request`` (so tests can inspect the per-call
            payload / assert the round count), advances the script cursor,
            and yields exactly one :class:`LlmResponse` built from the
            current step. A :class:`FunctionCallTurn` yields a
            ``function_call`` part; a :class:`TextTurn` yields a text part.
            When the script is exhausted, an empty text part is yielded so
            a stray extra invocation degrades gracefully instead of
            raising.
            """
            self.invocations.append(llm_request)
            idx = self.cursor
            self.cursor = idx + 1
            if idx < len(self.script):
                turn = self.script[idx]
            else:
                turn = TextTurn(text="")

            if isinstance(turn, FunctionCallTurn):
                part = genai_types.Part.from_function_call(name=turn.name, args=turn.args)
            else:
                part = genai_types.Part(text=turn.text)
            yield LlmResponse(content=genai_types.Content(role="model", parts=[part]))

    return _FakeADKModel


def make_fake_adk_model(script: list[ScriptTurn], *, model: str = "fake-proposer-model") -> Any:
    """Construct a :class:`FakeADKModel` over ``script``.

    ``script`` is the ordered list of turns the model replays — typically a
    :class:`FunctionCallTurn` (to exercise a tool round) followed by a
    :class:`TextTurn` carrying the final ``{hypothesis, patches}`` JSON. The
    returned model is wired into an ``LlmAgent(model=...)`` by the caller.
    ADK is imported here (lazily) so importing this module needs no extra.
    """
    cls = _build_base_llm_class()
    return cls(model=model, script=list(script), cursor=0, invocations=[])


__all__ = ["FunctionCallTurn", "TextTurn", "make_fake_adk_model"]
