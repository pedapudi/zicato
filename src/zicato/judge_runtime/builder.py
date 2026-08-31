"""Turn a :class:`~zicato.core.JudgeSpec` into a live goldfive ``Judge``.

A :class:`JudgeSpec` is zicato's *declarative* description of a quality
signal an operator wants armed on a board entry — a name, a mode
(``"inline"`` or ``"python"``), a ``body`` (a natural-language criterion
for inline, a dotted import path for python), and a
:class:`goldfive.DriftSeverity`. This module is the bridge that makes
that declaration *executable*: :func:`judge_spec_to_goldfive` returns an
object conforming to goldfive's :class:`~goldfive.judges.Judge`
protocol — a stable ``.name`` plus an async ``evaluate(ctx) ->
JudgeVerdict`` — that the goldfive runner installs via
``goldfive.wrap(judges=[...])`` and calls at every reasoning
observation point.

Two-callable rule
-----------------

Inline judges are LLM-as-a-judge: they call an LLM to decide whether a
reasoning trace violates the criterion. The callable they use is
zicato's *auxiliary* callable (``RuntimeConfig.auxiliary_call_llm``) —
NOT the harness callable the inner agent runs on. The judge is a
zicato-internal LLM consumer exactly like the emulator / proposer /
analyzer, so it shares their endpoint and stays identity-distinct from
the harness so a judge cannot trivially collude with the agent it
grades. The adapter owns picking the right callable; this module just
receives whatever ``aux_call_llm`` it is handed and uses it verbatim.

Enum -> string boundary
-----------------------

zicato carries drift taxonomy as :class:`goldfive.DriftKind` /
:class:`goldfive.DriftSeverity` enum members (the typed form every
zicato module passes around). goldfive's :class:`JudgeVerdict`, by
contrast, wants the *lowercase wire string* on its ``drift_kind`` /
``severity`` fields. Both enums are :class:`enum.StrEnum`, so the
conversion is ``str(member)``. This module performs that conversion at
exactly one place — :func:`_kind_str` / :func:`_severity_str`, called
only when a verdict is constructed — so the string form never escapes
upward into zicato code. Callers hand in enums; verdicts go out with
strings; nothing in between sees the string.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from zicato.import_path import explain_attribute_error
from zicato.judge_runtime.error_register import record_judge_error, record_judge_invocation
from zicato.judge_runtime.io_capture import JUDGE_IO_ERROR_KIND

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from collections.abc import Awaitable, Callable

    import goldfive
    from goldfive.judges import JudgeContext, JudgeVerdict

    AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

log = logging.getLogger("zicato.judge_runtime.builder")


# ---------------------------------------------------------------------------
# JudgeSpec structural protocol
# ---------------------------------------------------------------------------
#
# ``JudgeSpec`` is owned by ``zicato/core/types.py`` (a parallel agent
# defines it; this module must not redefine it). We depend
# on it *structurally* rather than importing the concrete dataclass:
#
#   * it keeps ``zicato.judge_runtime`` importable even before the core
#     type lands / in a partial checkout, and
#   * it documents — in one place — exactly which attributes this
#     builder reads, which is the real contract.
#
# Any object exposing ``name`` / ``mode`` / ``body`` / ``severity`` is
# accepted. The concrete ``zicato.core.JudgeSpec`` satisfies it.


@runtime_checkable
class JudgeSpecLike(Protocol):
    """Structural view of the fields :func:`judge_spec_to_goldfive` reads.

    Mirrors ``zicato.core.JudgeSpec``::

        {name: str,
         mode: "inline" | "python",
         body: str,
         severity: goldfive.DriftSeverity}
    """

    name: str
    mode: str
    body: str
    severity: Any  # goldfive.DriftSeverity at runtime


# ---------------------------------------------------------------------------
# Enum -> wire-string boundary
# ---------------------------------------------------------------------------


def _severity_str(severity: Any) -> str:
    """Project a :class:`goldfive.DriftSeverity` onto its wire string.

    ``DriftSeverity`` is a :class:`enum.StrEnum`, so ``str(member)`` is
    the lowercase canonical value (``"info"`` / ``"warning"`` /
    ``"critical"``). Tolerates a bare string too (already-projected
    input) so the function is idempotent. Falls back to ``"warning"``
    for an unrecognised value rather than raising — a judge's verdict
    must never crash the run.
    """
    text = str(severity).strip().lower()
    # ``str(DriftSeverity.WARNING)`` is already ``"warning"``; a stray
    # ``"DriftSeverity.WARNING"`` repr (non-StrEnum enum) is normalised.
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if text in ("info", "warning", "critical"):
        return text
    log.warning(
        "judge_runtime: unrecognised severity %r; defaulting to 'warning'",
        severity,
    )
    return "warning"


#: Cache for the lazily-built errored-verdict subclass (see
#: :func:`_errored_verdict`). goldfive is imported lazily throughout this
#: module so ``zicato.judge_runtime`` stays importable without it; the
#: subclass therefore cannot be declared at module scope.
_ERRORED_VERDICT_CLS: Any = None


def _errored_verdict(exc: BaseException) -> JudgeVerdict:
    """A verdict that says "this judge did not answer", not "no violation".

    goldfive's :class:`~goldfive.judges.JudgeVerdict` has four flavours —
    drift / rubric / boolean / numeric — and an empty-default verdict means
    "the judge had nothing to say". A judge whose callable RAISED had
    nothing to say either, which is the ambiguity issue #121 is
    about: the empty verdict a failed call returns is byte-identical to the
    one a healthy judge returns when the criterion was not violated.

    We cannot add a field to goldfive's dataclass from here, so we return a
    frozen SUBCLASS carrying two extra fields (``errored`` / ``error``). It
    is a :class:`JudgeVerdict` by ``isinstance``, and every goldfive read
    path is a ``getattr`` of a flavour field, so the steerer still derives
    NO ``verdict_kind`` from it and still emits no ``JudgementEmitted``:
    the wire, the reducer's counts and the scalar are unchanged. What
    changes is that an in-process caller — a test, a reliability probe, a
    future adapter — can now tell the two apart, and the durable half of
    the provenance rides the register + ``loss.json`` instead.
    """
    from goldfive.judges import JudgeVerdict  # noqa: PLC0415

    global _ERRORED_VERDICT_CLS  # noqa: PLW0603 — one-time lazy class build
    if _ERRORED_VERDICT_CLS is None or not issubclass(_ERRORED_VERDICT_CLS, JudgeVerdict):
        from dataclasses import dataclass  # noqa: PLC0415

        @dataclass(frozen=True)
        class _ErroredJudgeVerdict(JudgeVerdict):
            """An empty-flavoured verdict whose emptiness is a FAILURE."""

            errored: bool = True
            error: str = ""

        _ERRORED_VERDICT_CLS = _ErroredJudgeVerdict
    verdict: JudgeVerdict = _ERRORED_VERDICT_CLS(error=f"{type(exc).__name__}: {exc}")
    return verdict


def _custom_kind_str() -> str:
    """Return the wire string for the CUSTOM drift kind.

    Inline judges always emit :class:`goldfive.DriftKind.CUSTOM` — the
    taxonomy slot reserved for operator-defined signals. We resolve the
    enum member at call time (so the value tracks goldfive) and project
    it to its :class:`enum.StrEnum` wire string. Falls back to the
    literal ``"custom"`` if goldfive is somehow not importable, which
    keeps the builder usable under a partial environment.
    """
    try:
        import goldfive

        return str(goldfive.DriftKind.CUSTOM)
    except Exception:  # noqa: BLE001 - goldfive optional at import time
        return "custom"


# ---------------------------------------------------------------------------
# Inline (LLM-as-a-judge) judge
# ---------------------------------------------------------------------------


#: System prompt for the inline criterion judge. Deliberately terse and
#: contract-shaped: the model is asked for a single leading token
#: (``VIOLATION`` / ``OK``) followed by a one-line reason, so the
#: response parser in :meth:`_InlineCriterionJudge.evaluate` is a cheap
#: prefix check rather than an NL classifier.
_INLINE_SYSTEM_PROMPT = (
    "You are a strict reviewer auditing an AI agent's chain-of-thought "
    "against a single quality criterion. You are given the criterion and "
    "the agent's reasoning so far. Decide whether the reasoning so far is "
    "violating the criterion.\n\n"
    "Answer on ONE line. Start with the single word VIOLATION if the "
    "reasoning violates the criterion, or OK if it does not. After that "
    "word, add a brief one-clause reason. Do not write anything else."
)


def _build_inline_user_prompt(criterion: str, reasoning_text: str) -> str:
    """Assemble the user-message body for the inline criterion judge."""
    return (
        f"Criterion:\n{criterion}\n\n"
        f"Agent reasoning so far:\n{reasoning_text}\n\n"
        "Is the reasoning so far violating this criterion?"
    )


def _parse_inline_response(response: str) -> tuple[bool, str]:
    """Parse the inline judge LLM response into ``(violation, reason)``.

    The contract (see :data:`_INLINE_SYSTEM_PROMPT`) is a single line
    whose first token is ``VIOLATION`` or ``OK``. We are liberal in what
    we accept: the token match is case-insensitive and tolerates
    surrounding punctuation / a leading bullet. A response that does not
    clearly start with ``VIOLATION`` is treated as *no violation* — the
    judge fails safe (no spurious drift) when the model is unclear.
    """
    text = (response or "").strip()
    if not text:
        return False, ""
    first_line = text.splitlines()[0].strip()
    # Strip a leading bullet / quote so "- VIOLATION ..." still matches.
    stripped = first_line.lstrip("-*>\"' \t")
    head, _, rest = stripped.partition(" ")
    token = head.strip().strip(":.,;").upper()
    reason = rest.strip().strip("-:") or first_line
    if token == "VIOLATION":
        return True, reason
    # Defensive: a model that ignored the format but clearly says the
    # word "violation" anywhere in the first line still trips. ``OK``
    # and everything else is treated as no violation.
    if token != "OK" and "violation" in first_line.lower():
        return True, reason
    return False, ""


class _InlineCriterionJudge:
    """LLM-as-a-judge wrapping a natural-language criterion.

    Conforms to :class:`goldfive.judges.Judge` structurally: a stable
    ``name`` plus an async :meth:`evaluate`. Stateless across calls — the
    only retained state is the criterion, the severity wire string, and
    the auxiliary callable, all fixed at construction.

    :meth:`evaluate` calls the auxiliary LLM with the criterion plus
    ``ctx.reasoning_text`` and asks whether the reasoning so far violates
    the criterion. On a violation it returns a drift-flavoured
    :class:`~goldfive.judges.JudgeVerdict`:

    * ``drift_emitted = True``
    * ``drift_kind`` = the wire string for :class:`goldfive.DriftKind.CUSTOM`
    * ``severity`` = the wire string for the spec's
      :class:`goldfive.DriftSeverity`
    * ``detail`` = ``"<criterion>: <one-line reason>"``

    On no violation — or an empty reasoning trace, or any error from the
    auxiliary callable — it returns an empty-default verdict so the
    steerer emits no :class:`JudgementEmitted` for that observation
    point (no signal == no event). The steerer additionally bounds
    :meth:`evaluate` with its own 30s timeout, so a hung auxiliary
    endpoint degrades to "no signal" rather than wedging the run.

    ``io_sink`` (optional, a
    :class:`zicato.judge_runtime.io_capture.JudgeIOSink`) retains the
    verbatim I/O of every evaluate call that reached the LLM — the exact
    reasoning judged, the raw response, and the parsed verdict (firing
    AND silent) — for board reflection. Strictly best-effort: a sink
    failure is logged and swallowed, never changing the verdict. With
    ``io_sink=None`` (the default, and every pre-existing caller) the
    evaluate path is byte-identical to before the seam existed.
    """

    __slots__ = ("name", "_criterion", "_severity_str", "_aux_call_llm", "_io_sink")

    def __init__(
        self,
        *,
        name: str,
        criterion: str,
        severity: Any,
        aux_call_llm: AuxCallLLM,
        io_sink: Any = None,
    ) -> None:
        self.name: str = name
        self._criterion: str = criterion
        # Project the enum to its wire string once, at construction —
        # the verdict-time path never sees the enum.
        self._severity_str: str = _severity_str(severity)
        self._aux_call_llm: AuxCallLLM = aux_call_llm
        self._io_sink: Any = io_sink

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        """Audit ``ctx.reasoning_text`` against the criterion via the aux LLM."""
        from goldfive.judges import JudgeVerdict

        reasoning_text = (getattr(ctx, "reasoning_text", "") or "").strip()
        if not reasoning_text:
            # Not a reasoning-emit observation point — nothing to judge.
            return JudgeVerdict()

        system = _INLINE_SYSTEM_PROMPT
        user = _build_inline_user_prompt(self._criterion, reasoning_text)
        record_judge_invocation(self.name)
        try:
            # ``model=""`` matches zicato's CallLLM contract: the
            # concrete auxiliary callable resolves its own model. The
            # inline judge never pins a model — routing is the
            # callable's job.
            response = await self._aux_call_llm(system, user, "")
        except Exception as exc:  # noqa: BLE001 - a judge must not crash the run
            # The call failed, so this judge has NO verdict for this
            # observation point. Swallowing stays (a judge must not crash
            # the run) but the failure is now counted in the process
            # register — which rides out to ``loss.json`` and loop health —
            # captured for reflection, and marked on the returned verdict.
            record_judge_error(self.name, exc)
            log.warning(
                "judge_runtime: inline judge %r aux_call_llm raised %s (%s); treating as no signal",
                self.name,
                type(exc).__name__,
                exc,
            )
            self._capture(
                ctx,
                reasoning_text=reasoning_text,
                raw_response="",
                drift_emitted=False,
                kind=JUDGE_IO_ERROR_KIND,
                severity="",
                detail=f"{type(exc).__name__}: {exc}",
            )
            return _errored_verdict(exc)

        violation, reason = _parse_inline_response(response)
        detail = ""
        if violation:
            detail = f"{self._criterion}: {reason}" if reason else self._criterion
        kind = _custom_kind_str() if violation else ""

        self._capture(
            ctx,
            reasoning_text=reasoning_text,
            raw_response=str(response),
            drift_emitted=violation,
            kind=kind,
            severity=self._severity_str if violation else "",
            detail=detail,
        )

        if not violation:
            return JudgeVerdict()

        return JudgeVerdict(
            drift_emitted=True,
            drift_kind=kind,
            severity=self._severity_str,
            detail=detail,
        )

    def _capture(
        self,
        ctx: JudgeContext,
        *,
        reasoning_text: str,
        raw_response: str,
        drift_emitted: bool,
        kind: str,
        severity: str,
        detail: str,
    ) -> None:
        """Retain one evaluate call's verbatim I/O through ``io_sink``.

        Best-effort by hard contract: ANY sink failure is logged and
        swallowed — capture must never change the verdict or crash the
        run. Called for firing AND silent verdicts (the silent ones are
        exactly the missed-fire candidates adjudication needs to re-read)
        AND for a call that RAISED, with
        :data:`~zicato.judge_runtime.io_capture.JUDGE_IO_ERROR_KIND` in
        the ``kind`` slot: a failed call is not a missed fire, and an
        adjudicator that cannot see it re-reads a broken endpoint as a
        criterion that is too narrow.
        """
        if self._io_sink is None:
            return
        try:
            self._io_sink.record(
                self.name,
                reasoning_text=reasoning_text,
                transcript_window=tuple(str(t) for t in (getattr(ctx, "transcript", ()) or ())),
                raw_response=raw_response,
                drift_emitted=drift_emitted,
                kind=kind,
                severity=severity,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — capture must never affect the verdict
            log.warning(
                "judge_runtime: inline judge %r io_sink raised %s (%s); capture skipped",
                self.name,
                type(exc).__name__,
                exc,
            )


# ---------------------------------------------------------------------------
# Python (dotted-path) judge
# ---------------------------------------------------------------------------


def _resolve_dotted_path(path: str) -> Any:
    """Import and return the object at dotted ``path``.

    Accepts both the ``pkg.mod:attr`` (entry-points style, used across
    zicato adapters) and the ``pkg.mod.attr`` forms. Raises
    :class:`ValueError` on a malformed / empty spec and
    :class:`ImportError` / :class:`AttributeError` (chained) on a
    missing module / attribute, so the caller sees an actionable
    message rather than an opaque failure.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            f"python-mode JudgeSpec.body must be a non-empty dotted path, got {path!r}"
        )
    spec = path.strip()
    if ":" in spec:
        module_name, _, attr_name = spec.partition(":")
        module_name = module_name.strip()
        attr_name = attr_name.strip()
        if not module_name or not attr_name:
            raise ValueError(
                f"python-mode JudgeSpec.body uses colon form but module or "
                f"attribute is empty: {spec!r}"
            )
    else:
        if "." not in spec:
            raise ValueError(
                f"python-mode JudgeSpec.body must be 'pkg.mod:attr' or 'pkg.mod.attr'; got {spec!r}"
            )
        module_name, _, attr_name = spec.rpartition(".")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"judge_runtime: could not import module {module_name!r} for "
            f"python-mode JudgeSpec.body {spec!r}: {exc}"
        ) from exc
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        detail = explain_attribute_error(module, attr_name, exc)
        if detail is not None:
            raise AttributeError(
                f"judge_runtime: module {module_name!r}: {detail} "
                f"(python-mode JudgeSpec.body {spec!r})"
            ) from exc
        raise AttributeError(
            f"judge_runtime: module {module_name!r} has no attribute "
            f"{attr_name!r} (python-mode JudgeSpec.body {spec!r})"
        ) from exc


class _PythonJudgeWrapper:
    """Wrap an operator-supplied callable / Judge as a goldfive ``Judge``.

    ``python``-mode :class:`JudgeSpec` bodies are dotted paths to
    operator code. The resolved object may be any of:

    * a :class:`~goldfive.judges.Judge` *instance* (has an async
      ``evaluate`` bound method) — used directly;
    * a :class:`~goldfive.judges.Judge` *class* — instantiated with no
      arguments, then used as an instance (the common "point the spec
      at a judge class" case); or
    * a plain ``async def evaluate(ctx) -> JudgeVerdict`` callable — this
      wrapper supplies the ``name`` and adapts it.

    In every case the wrapper guarantees ``name == spec.name`` (so the
    :class:`JudgementEmitted` envelope keys on the operator-chosen name,
    matching the inline case) and normalises the verdict so its
    drift-flavoured fields carry *strings* rather than enums — operator code is
    free to return either, and the enum->string boundary stays inside
    :mod:`zicato.judge_runtime`.

    A python judge's verdict is drift-flavoured by contract: zicato
    judges feed the drift loss signal. When the resolved callable
    returns a non-drift verdict (rubric / boolean / numeric only) the
    wrapper passes it through untouched — goldfive still emits the
    :class:`JudgementEmitted` envelope for it — but does not synthesise
    a drift flavour it was not given.
    """

    __slots__ = ("name", "_inner_evaluate")

    def __init__(self, *, name: str, resolved: Any) -> None:
        self.name: str = name
        target = resolved
        # A dotted path to a class -> instantiate it, so ``evaluate`` is
        # a bound method (an unbound class method would still want
        # ``self`` and blow up on the first call). Operator judge
        # classes take no constructor args by the JudgeSpec contract;
        # a class that needs args is a malformed spec and surfaces as a
        # clear TypeError here rather than deep in the run.
        if isinstance(resolved, type):
            try:
                target = resolved()
            except TypeError as exc:
                raise TypeError(
                    f"judge_runtime: python-mode JudgeSpec resolved to class "
                    f"{resolved!r}, which could not be instantiated with no "
                    f"arguments: {exc}"
                ) from exc
        inner_evaluate = getattr(target, "evaluate", None)
        if callable(inner_evaluate):
            # Target is a Judge (or Judge-shaped) instance.
            self._inner_evaluate = inner_evaluate
        elif callable(target):
            # Target is a bare ``async def evaluate`` callable.
            self._inner_evaluate = target
        else:
            raise TypeError(
                f"judge_runtime: python-mode JudgeSpec resolved to "
                f"{resolved!r}, which is neither a Judge (no callable "
                f"'evaluate') nor a callable"
            )

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        """Delegate to the resolved callable and normalise the verdict.

        Operator code that RAISES is caught here rather than left to
        goldfive's ``evaluate_judges``, which logs it and ``continue``s.
        The catch is behaviourally neutral on the wire: the errored verdict
        it returns populates no flavour, so goldfive derives no
        ``verdict_kind`` and emits no ``JudgementEmitted``. What it adds is
        that the failure lands in the process register, so a python judge
        that raised on every
        invocation is distinguishable in ``loss.json`` from one whose
        criterion was never met.
        """
        from goldfive.judges import JudgeVerdict

        record_judge_invocation(self.name)
        try:
            result = self._inner_evaluate(ctx)
            # Tolerate both sync and async operator callables.
            if hasattr(result, "__await__"):
                verdict = await result
            else:
                verdict = result
        except Exception as exc:  # noqa: BLE001 - a judge must not crash the run
            record_judge_error(self.name, exc)
            log.warning(
                "judge_runtime: python judge %r raised %s (%s); treating as no signal",
                self.name,
                type(exc).__name__,
                exc,
            )
            return _errored_verdict(exc)
        if verdict is None:
            return JudgeVerdict()
        return _normalise_verdict(verdict)


def _normalise_verdict(verdict: Any) -> JudgeVerdict:
    """Re-stamp a verdict's drift fields as wire strings.

    Operator-supplied python judges may set ``drift_kind`` / ``severity``
    as :class:`goldfive.DriftKind` / :class:`goldfive.DriftSeverity`
    enum members (the natural thing to reach for) or as the raw wire
    strings. goldfive's steerer wants strings. We rebuild the verdict
    with ``str(...)``-projected drift fields when it is drift-flavoured;
    a non-drift verdict is returned unchanged.
    """
    from goldfive.judges import JudgeVerdict

    if not getattr(verdict, "drift_emitted", False):
        # rubric / boolean / numeric — leave untouched. Operator python
        # judges are contracted to return a JudgeVerdict; cast narrows
        # the ``Any`` parameter back to the declared return type.
        return cast("JudgeVerdict", verdict)
    return JudgeVerdict(
        drift_emitted=True,
        drift_kind=str(getattr(verdict, "drift_kind", "") or ""),
        severity=_severity_str(getattr(verdict, "severity", "") or ""),
        rubric_score=getattr(verdict, "rubric_score", None),
        rubric_dimensions=dict(getattr(verdict, "rubric_dimensions", {}) or {}),
        boolean_result=getattr(verdict, "boolean_result", None),
        numeric_value=getattr(verdict, "numeric_value", None),
        metric_name=str(getattr(verdict, "metric_name", "") or ""),
        detail=str(getattr(verdict, "detail", "") or ""),
    )


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def judge_spec_to_goldfive(
    spec: JudgeSpecLike,
    aux_call_llm: AuxCallLLM,
    io_sink: Any = None,
) -> goldfive.Judge:
    """Build a live goldfive :class:`~goldfive.judges.Judge` from ``spec``.

    Parameters
    ----------
    spec:
        A :class:`zicato.core.JudgeSpec` (consumed structurally — any
        object with ``name`` / ``mode`` / ``body`` / ``severity``).

        * ``mode="inline"`` — ``body`` is a natural-language criterion.
          Returns an LLM-as-a-judge that, on each reasoning observation,
          asks ``aux_call_llm`` whether the reasoning so far violates the
          criterion and emits a :class:`goldfive.DriftKind.CUSTOM`
          drift verdict at the spec's severity when it does.
        * ``mode="python"`` — ``body`` is a dotted import path
          (``pkg.mod:attr`` or ``pkg.mod.attr``) to operator code that
          is a :class:`~goldfive.judges.Judge` instance, a Judge class
          (instantiated with no arguments), or a bare ``evaluate``
          callable. Returns a wrapper with ``name == spec.name``.
    aux_call_llm:
        zicato's auxiliary LLM callable
        (``RuntimeConfig.auxiliary_call_llm``) — ``(system, user, model)
        -> str``. Used only by inline judges; ignored for python judges
        (their code brings its own dependencies). The two-callable rule
        means this is NOT the harness callable.
    io_sink:
        Optional :class:`zicato.judge_runtime.io_capture.JudgeIOSink`.
        INLINE judges emit each evaluate call's verbatim I/O through it
        (best-effort — board reflection's capture seam); ``None`` (the
        default) captures nothing and is byte-identical to before the
        parameter existed. Ignored for ``python``-mode judges — capture
        is inline-only for now: operator python judges have no
        zicato-visible LLM call, so there is no raw response to retain
        (see :mod:`zicato.judge_runtime.io_capture`'s scope note).

    Returns
    -------
    goldfive.Judge
        An object conforming to goldfive's :class:`Judge` protocol,
        ready to drop into ``goldfive.wrap(judges=[...])``. Its ``name``
        equals ``spec.name`` so the resulting
        :class:`JudgementEmitted.judge_name` is the operator-chosen
        name.

    Raises
    ------
    ValueError
        On an empty ``name``, an unknown ``mode``, or an empty / malformed
        python-mode ``body``.
    ImportError / AttributeError
        On a python-mode ``body`` whose module / attribute cannot be
        resolved (chained to the original cause).
    TypeError
        On a python-mode ``body`` resolving to something that is neither
        a Judge nor a callable.
    """
    name = str(getattr(spec, "name", "") or "").strip()
    if not name:
        raise ValueError("JudgeSpec.name must be a non-empty string")
    mode = str(getattr(spec, "mode", "") or "").strip().lower()
    body = getattr(spec, "body", "")
    severity = getattr(spec, "severity", "")

    if mode == "inline":
        criterion = str(body or "").strip()
        if not criterion:
            raise ValueError(
                f"inline JudgeSpec {name!r}: body (the criterion) must be a non-empty string"
            )
        return _InlineCriterionJudge(
            name=name,
            criterion=criterion,
            severity=severity,
            aux_call_llm=aux_call_llm,
            io_sink=io_sink,
        )

    if mode == "python":
        resolved = _resolve_dotted_path(str(body or ""))
        return _PythonJudgeWrapper(name=name, resolved=resolved)

    raise ValueError(f"JudgeSpec {name!r}: unknown mode {mode!r}; expected 'inline' or 'python'")


__all__ = ["JudgeSpecLike", "judge_spec_to_goldfive"]
