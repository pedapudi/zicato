"""Adversarial entry support — resolve and run known-bad agents.

A ``synthetic_adversarial`` board entry names an adversarial agent
class by **dotted path** (e.g.
``"goldfive.testkit.adversarial:LoopingAgent"``). The runner here
resolves the spec, instantiates the class, and runs it under
``goldfive.wrap`` with the entry's user message. The expectation that
goldfive's steerer actually fires the right drift kinds lives in
:mod:`zicato.synthetic.expectations`; this module only owns the
**execution** side.

Defensive imports
-----------------
``goldfive`` itself is a hard dependency of zicato (declared in
``pyproject.toml``) so importing ``goldfive`` at runtime is fine.
However, the **testkit** submodule that ships the
``LoopingAgent``/``HallucinatingAgent``/``CleanAgent`` zoo is being
implemented in parallel by another team and is not guaranteed to be
present when zicato is installed. We therefore:

* Lazy-import goldfive only inside :func:`run_adversarial_entry` so
  pure type-resolution paths (e.g. tests that exercise
  :func:`resolve_adversarial_agent` with a local stub) do not require
  goldfive to be importable.
* Wrap the dotted-path import in a try/except that re-raises
  :class:`AdversarialResolutionError` with the original
  :class:`ImportError`/:class:`AttributeError` chained, so the operator
  sees the actionable cause rather than a stack trace ending in
  goldfive internals.

Spec grammar
------------
The supported spec form is ``"module.path:Attribute"`` (PEP 451 entry-
point style) OR plain ``"module.path.Attribute"`` (dotted form). The
colon form is preferred because it disambiguates ``pkg.sub.Class``
from ``pkg.sub_class`` without heuristics. The dotted form is accepted
as a convenience for hand-edited board JSONL.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import uuid
from typing import Any

from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
from zicato.import_path import explain_attribute_error


class AdversarialResolutionError(RuntimeError):
    """Raised when an adversarial agent spec cannot be resolved.

    Construction-time problems (bad dotted path, missing attribute,
    module fails to import) all raise this error type with a single
    actionable message. The original cause is chained via ``from`` so
    the operator's traceback shows the real root.
    """


def resolve_adversarial_agent(spec: str) -> Any:
    """Resolve an adversarial agent class/factory from a dotted-path spec.

    Accepts both forms:

    * ``"pkg.mod:Attribute"`` — colon-separated (preferred).
    * ``"pkg.mod.Attribute"`` — pure-dotted (convenience).

    For the dotted form we split on the last dot; the right-hand piece
    becomes the attribute and the left-hand piece is the module to
    import. This is correct for the vast majority of agent-class names
    (CamelCase attributes living in lowercase modules) but the colon
    form is unambiguous and recommended for board entries.

    Parameters
    ----------
    spec:
        The dotted-path spec.

    Returns
    -------
    Any
        The resolved class or factory callable. Not instantiated — the
        caller is responsible for invoking it with the right args.

    Raises
    ------
    AdversarialResolutionError
        On empty spec, malformed spec, missing module, or missing
        attribute. The original cause is chained.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise AdversarialResolutionError(
            "adversarial agent spec must be a non-empty string, " f"got {spec!r}"
        )

    spec = spec.strip()
    if ":" in spec:
        module_name, _, attr_name = spec.partition(":")
        module_name = module_name.strip()
        attr_name = attr_name.strip()
        if not module_name or not attr_name:
            raise AdversarialResolutionError(
                "adversarial agent spec uses colon form but module or "
                f"attribute is empty: {spec!r}"
            )
    else:
        if "." not in spec:
            raise AdversarialResolutionError(
                "adversarial agent spec must be either 'pkg.mod:Attr' or "
                f"'pkg.mod.Attr'; got {spec!r}"
            )
        module_name, _, attr_name = spec.rpartition(".")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AdversarialResolutionError(
            f"could not import module {module_name!r} for adversarial "
            f"agent spec {spec!r}: {exc}"
        ) from exc

    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        detail = explain_attribute_error(module, attr_name, exc)
        if detail is not None:
            raise AdversarialResolutionError(
                f"module {module_name!r}: {detail} (adversarial agent spec {spec!r})"
            ) from exc
        raise AdversarialResolutionError(
            f"module {module_name!r} has no attribute {attr_name!r} "
            f"(adversarial agent spec {spec!r})"
        ) from exc


def _parse_agent_args(entry: BoardEntry) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Extract constructor args from ``entry.context['args']`` if present.

    ``BoardEntry.context`` is a ``Mapping[str, str]`` by contract — every
    value is a string. To support structured kwargs we look for an
    ``"args"`` key whose value is a JSON object. The shape supported:

    * ``{"args": [...], "kwargs": {...}}`` — fully explicit.
    * ``{"kwargs": {...}}`` — kwargs only.
    * ``[...]`` — positional only.
    * ``{...}`` (no ``args``/``kwargs`` keys) — treated as kwargs.

    Anything malformed produces ``((), {})`` silently — the runner
    then constructs the agent with no arguments, which is the
    documented default for testkit adversarial agents.
    """
    raw = entry.context.get("args") if entry.context else None
    if not raw:
        return ((), {})
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ((), {})

    if isinstance(parsed, list):
        return (tuple(parsed), {})
    if isinstance(parsed, dict):
        if "args" in parsed or "kwargs" in parsed:
            args = tuple(parsed.get("args") or ())
            kwargs = dict(parsed.get("kwargs") or {})
            return (args, kwargs)
        return ((), dict(parsed))
    return ((), {})


def _final_output_from_outcome(outcome: Any) -> str:
    """Best-effort extraction of the user-facing final assistant turn.

    goldfive's ``ExecutionOutcome`` shape exposes the final transcript
    through one of several attributes depending on which adapter and
    planner produced it. We probe in priority order so this works
    against both real goldfive outcomes and the test-side stubs that
    only populate ``final_output`` or ``transcript``.
    """
    for attr in ("final_output", "output", "response", "result"):
        value = getattr(outcome, attr, None)
        if isinstance(value, str) and value:
            return value
    transcript = getattr(outcome, "transcript", None)
    if isinstance(transcript, list | tuple) and transcript:
        last = transcript[-1]
        if isinstance(last, str):
            return last
        text = getattr(last, "text", None) or getattr(last, "content", None)
        if isinstance(text, str):
            return text
    return ""


def _transcript_from_outcome(outcome: Any, fallback_final: str) -> tuple[str, ...]:
    """Extract user-facing assistant turns from an outcome.

    Falls back to a length-1 tuple of the final output for outcomes
    that do not expose a transcript — keeps the single-turn invariant
    documented on :class:`zicato.core.types.RunResult`.
    """
    transcript = getattr(outcome, "transcript", None)
    if isinstance(transcript, list | tuple):
        out: list[str] = []
        for turn in transcript:
            if isinstance(turn, str):
                out.append(turn)
            else:
                text = getattr(turn, "text", None) or getattr(turn, "content", None)
                if isinstance(text, str):
                    out.append(text)
        if out:
            return tuple(out)
    if fallback_final:
        return (fallback_final,)
    return ()


async def _run_under_wrap(
    agent: Any,
    user_input: str,
    sinks: list[Any],
    config: RuntimeConfig,
) -> Any:
    """Drive ``agent`` through ``goldfive.wrap(...).run(user_input)``.

    Lazy-imports goldfive so module import of zicato.synthetic does
    not require goldfive to be available — only this function does.
    """
    try:
        import goldfive
    except ImportError as exc:  # pragma: no cover - exercised when goldfive missing
        raise AdversarialResolutionError(
            "goldfive is not importable; install the 'goldfive' package "
            "before running synthetic adversarial/clean entries"
        ) from exc

    runner = goldfive.wrap(
        agent,
        sinks=list(sinks),
        call_llm=config.target_call_llm,
    )
    return await runner.run(user_input)


async def run_adversarial_entry(
    entry: BoardEntry,
    sinks: list[Any],
    config: RuntimeConfig,
) -> RunResult:
    """Run a ``synthetic_adversarial`` board entry under ``goldfive.wrap``.

    Steps:

    1. Validate the entry is ``synthetic_adversarial``.
    2. Resolve ``entry.adversarial_agent_spec`` via
       :func:`resolve_adversarial_agent`.
    3. Parse optional constructor args from ``entry.context['args']``
       (see :func:`_parse_agent_args`).
    4. Instantiate the agent.
    5. Run it under ``goldfive.wrap`` with ``entry.input`` as the user
       message and ``entry.wall_clock_budget_seconds`` as the hard
       wall-clock ceiling. Exceeding the budget produces a
       ``RunResult`` with ``aborted=True`` and
       ``abort_reason="wall_clock_budget_exceeded"``.

    Returns
    -------
    RunResult
        The transcript-shape result. Internal goldfive events live in
        the sinks the caller supplied; this function does not surface
        them directly.

    Raises
    ------
    AdversarialResolutionError
        If the spec cannot be resolved or goldfive cannot be imported.
    ValueError
        If the entry is malformed for this runner.
    """
    if entry.kind != "synthetic_adversarial":
        raise ValueError(
            f"run_adversarial_entry called with entry.kind={entry.kind!r}; "
            "expected 'synthetic_adversarial'"
        )
    if not entry.adversarial_agent_spec:
        raise ValueError(f"BoardEntry {entry.id!r}: adversarial_agent_spec is required")
    if entry.input is None:
        raise ValueError(f"BoardEntry {entry.id!r}: input is required")

    agent_cls = resolve_adversarial_agent(entry.adversarial_agent_spec)
    args, kwargs = _parse_agent_args(entry)
    try:
        agent = agent_cls(*args, **kwargs)
    except TypeError as exc:
        raise AdversarialResolutionError(
            f"resolved adversarial agent {entry.adversarial_agent_spec!r} "
            f"could not be instantiated with args={args!r}, kwargs={kwargs!r}: {exc}"
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
    except TimeoutError:
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
