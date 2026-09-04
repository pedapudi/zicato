"""Tests for :mod:`zicato.judge_runtime` — the JudgeSpec -> goldfive bridge.

Covers:

* an **inline** custom judge fires on a violating reasoning trace and
  produces the right drift-flavoured verdict (``drift_emitted``, CUSTOM
  kind, the spec's severity, a ``"<criterion>: <reason>"`` detail, and
  ``name == spec.name``);
* an inline judge stays silent on a non-violating trace / empty trace /
  a raising evaluation callable — and, for the raising one, that the
  swallowed failure is still counted in the per-judge error register and
  marked on the verdict (issue #121);
* a **python** custom judge loads from a dotted path and runs, with its
  ``name`` re-pinned to the spec and its drift fields normalised to
  strings;
* ``disable_drift`` suppresses the matching built-in judge;
* :func:`assemble_judges` composes built-ins-minus-suppressed + custom.

These exercise the real goldfive judges API (``goldfive.judges`` /
``goldfive.builtin_judges``) — the builder is verified against the
shipped protocol, not a stand-in.

``JudgeSpec`` itself is owned by ``zicato/core/types.py``; this module
must not redefine it. The builder consumes a JudgeSpec *structurally*
(any object with ``name`` / ``mode`` / ``body`` / ``severity``), so the
tests drive it with a tiny local stand-in dataclass whose shape matches
the contract. The runtime code path is identical for the real type.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import goldfive
import pytest
from goldfive.judges import JudgeContext, JudgeVerdict

from zicato.judge_runtime import (
    assemble_judges,
    builtin_judge_names_to_suppress,
    clear_judge_errors,
    default_judges_minus,
    judge_error_snapshot,
    judge_spec_to_goldfive,
)

# ---------------------------------------------------------------------------
# JudgeSpec stand-in (structural match for zicato.core.JudgeSpec)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _SpecStub:
    """Structural stand-in for ``zicato.core.JudgeSpec``.

    Mirrors the contract ``{name, mode, body, severity}``. The builder
    is duck-typed, so this is interchangeable with the real type at the
    call boundary.
    """

    name: str
    mode: str
    body: str
    severity: Any


# ---------------------------------------------------------------------------
# Scripted evaluation callables (the two-callable rule: judges use aux)
# ---------------------------------------------------------------------------


def _aux_returning(reply: str):
    """An ``aux_call_llm`` that always returns ``reply``, recording calls."""
    calls: list[tuple[str, str, str]] = []

    async def _aux(system: str, user: str, model: str) -> str:
        calls.append((system, user, model))
        return reply

    _aux.calls = calls  # type: ignore[attr-defined]
    return _aux


def _aux_raising():
    """An ``aux_call_llm`` that always raises — judge must fail safe."""

    async def _aux(system: str, user: str, model: str) -> str:
        raise RuntimeError("aux endpoint down")

    return _aux


# ---------------------------------------------------------------------------
# Python-mode judge targets (resolved by dotted path)
# ---------------------------------------------------------------------------


class SampleJudgeClassTarget:
    """A python-mode target that is already a goldfive ``Judge``.

    Its ``name`` is intentionally NOT the spec name — the wrapper must
    re-pin it. Returns a drift verdict carrying *enum* drift fields so
    the wrapper's enum->string normalisation is exercised.
    """

    name = "original_name_should_be_overridden"

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        if "danger" in (ctx.reasoning_text or "").lower():
            return JudgeVerdict(
                drift_emitted=True,
                # deliberately enum members, not strings:
                drift_kind=goldfive.DriftKind.CUSTOM,
                severity=goldfive.DriftSeverity.CRITICAL,
                detail="python judge tripped",
            )
        return JudgeVerdict()


async def sample_judge_raising_target(ctx: JudgeContext) -> JudgeVerdict:
    """A python-mode target whose operator code is broken (issue #121)."""
    raise RuntimeError("operator judge blew up")


async def sample_judge_callable_target(ctx: JudgeContext) -> JudgeVerdict:
    """A python-mode target that is a bare ``evaluate`` callable."""
    if "bad" in (ctx.reasoning_text or "").lower():
        return JudgeVerdict(
            drift_emitted=True,
            drift_kind="custom",
            severity="warning",
            detail="callable judge tripped",
        )
    return JudgeVerdict()


_PY_RAISING_PATH = "tests.test_judge_runtime:sample_judge_raising_target"
_PY_CLASS_PATH = "tests.test_judge_runtime:SampleJudgeClassTarget"
_PY_CALLABLE_PATH = "tests.test_judge_runtime:sample_judge_callable_target"


# ---------------------------------------------------------------------------
# Inline judge — fires on a violation
# ---------------------------------------------------------------------------


async def test_inline_judge_fires_on_violating_reasoning() -> None:
    """An inline judge produces the right verdict on a violating trace."""
    spec = _SpecStub(
        name="no_offtopic_tangents",
        mode="inline",
        body="The agent must not go on tangents unrelated to the user's request.",
        severity=goldfive.DriftSeverity.WARNING,
    )
    # The aux LLM (judge endpoint) returns a VIOLATION verdict.
    aux = _aux_returning("VIOLATION the agent abandoned the task to discuss the weather")
    judge = judge_spec_to_goldfive(spec, aux)

    # name is the spec name -> becomes JudgementEmitted.judge_name
    assert judge.name == "no_offtopic_tangents"

    ctx = JudgeContext(
        reasoning_text="Forget the user's question, let me ramble about the weather."
    )
    verdict = await judge.evaluate(ctx)

    assert verdict.drift_emitted is True
    # CUSTOM drift kind — goldfive's JudgeVerdict normalises it to the
    # DriftKind enum (a StrEnum, so it still equals the "custom" wire token).
    assert verdict.drift_kind == str(goldfive.DriftKind.CUSTOM)
    assert verdict.drift_kind == "custom"
    assert isinstance(verdict.drift_kind, goldfive.DriftKind)
    # severity is the spec's severity, likewise normalised to the enum.
    assert verdict.severity == "warning"
    # detail is "<criterion>: <one-line reason>".
    assert verdict.detail.startswith(spec.body)
    assert "weather" in verdict.detail
    # the aux callable was actually invoked with the criterion + reasoning.
    assert len(aux.calls) == 1  # type: ignore[attr-defined]
    _system, user, _model = aux.calls[0]  # type: ignore[attr-defined]
    assert spec.body in user
    assert "ramble about the weather" in user

    # The verdict projects cleanly back onto a goldfive DriftEvent —
    # i.e. the steerer's legacy-path bridge accepts it.
    assert goldfive.DriftKind(verdict.drift_kind) is goldfive.DriftKind.CUSTOM
    assert goldfive.DriftSeverity(verdict.severity) is goldfive.DriftSeverity.WARNING


async def test_inline_judge_honours_spec_severity() -> None:
    """The verdict severity tracks the spec, not a hardcoded default."""
    spec = _SpecStub(
        name="critical_criterion",
        mode="inline",
        body="The agent must never fabricate a citation.",
        severity=goldfive.DriftSeverity.CRITICAL,
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning("VIOLATION fabricated a source"))
    verdict = await judge.evaluate(JudgeContext(reasoning_text="I'll invent a source."))
    assert verdict.drift_emitted is True
    assert verdict.severity == "critical"


# ---------------------------------------------------------------------------
# Inline judge — stays silent
# ---------------------------------------------------------------------------


async def test_inline_judge_silent_on_non_violation() -> None:
    """An OK verdict from the aux LLM yields an empty-default verdict."""
    spec = _SpecStub(
        name="c",
        mode="inline",
        body="stay on task",
        severity=goldfive.DriftSeverity.WARNING,
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning("OK the reasoning is on task"))
    verdict = await judge.evaluate(
        JudgeContext(reasoning_text="Let me carefully address the user's question.")
    )
    assert verdict.drift_emitted is False
    assert verdict.drift_kind == ""
    assert verdict.severity == ""


async def test_inline_judge_silent_on_empty_reasoning() -> None:
    """An empty reasoning trace is not a reasoning-emit point — no aux call."""
    spec = _SpecStub(
        name="c", mode="inline", body="stay on task", severity=goldfive.DriftSeverity.INFO
    )
    aux = _aux_returning("VIOLATION")
    judge = judge_spec_to_goldfive(spec, aux)
    verdict = await judge.evaluate(JudgeContext(reasoning_text=""))
    assert verdict.drift_emitted is False
    # the aux callable must not be invoked when there is nothing to judge.
    assert aux.calls == []  # type: ignore[attr-defined]


async def test_inline_judge_fails_safe_on_aux_error() -> None:
    """A raising aux callable degrades to 'no signal', never crashes."""
    spec = _SpecStub(
        name="c", mode="inline", body="stay on task", severity=goldfive.DriftSeverity.WARNING
    )
    judge = judge_spec_to_goldfive(spec, _aux_raising())
    verdict = await judge.evaluate(JudgeContext(reasoning_text="some reasoning"))
    assert verdict.drift_emitted is False


async def test_inline_judge_error_is_counted_in_the_register() -> None:
    """The swallowed exception still leaves a durable count (issue #121).

    Both invocations and errors are counted, so the finding can say "raised
    on N of N" rather than only "raised".
    """
    clear_judge_errors()
    spec = _SpecStub(
        name="counted", mode="inline", body="stay on task", severity=goldfive.DriftSeverity.WARNING
    )
    judge = judge_spec_to_goldfive(spec, _aux_raising())
    for _ in range(3):
        await judge.evaluate(JudgeContext(reasoning_text="some reasoning"))
    # An observation point with nothing to judge never reaches the callable,
    # so it must not inflate the invocation count.
    await judge.evaluate(JudgeContext(reasoning_text="   "))

    snapshot = {je.judge_name: je for je in judge_error_snapshot()}
    assert snapshot["counted"].invocations == 3
    assert snapshot["counted"].errors == 3
    assert snapshot["counted"].last_error_type == "RuntimeError"
    clear_judge_errors()


async def test_healthy_inline_judge_records_no_error_entry() -> None:
    """A judge that answers — fire or silence — never enters the register."""
    clear_judge_errors()
    spec = _SpecStub(
        name="healthy", mode="inline", body="stay on task", severity=goldfive.DriftSeverity.WARNING
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning("OK nothing wrong"))
    verdict = await judge.evaluate(JudgeContext(reasoning_text="some reasoning"))
    assert verdict.drift_emitted is False
    assert getattr(verdict, "errored", False) is False
    assert judge_error_snapshot() == ()
    clear_judge_errors()


async def test_python_judge_error_is_counted_and_marked() -> None:
    """Operator code that raises is caught HERE, not only by goldfive.

    goldfive's steerer catches it too, but its catch leaves no zicato-side
    trace: the run then carries a judge that decided nothing and looks
    exactly like one that decided "no violation".
    """
    clear_judge_errors()
    spec = _SpecStub(
        name="py_broken",
        mode="python",
        body=_PY_RAISING_PATH,
        severity=goldfive.DriftSeverity.WARNING,
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning(""))
    verdict = await judge.evaluate(JudgeContext(reasoning_text="some reasoning"))

    # Behaviourally neutral on the wire: no flavour is populated, so
    # goldfive still derives no verdict_kind and emits no JudgementEmitted.
    assert verdict.drift_emitted is False
    assert verdict.rubric_score is None
    assert verdict.boolean_result is None
    assert verdict.numeric_value is None
    assert isinstance(verdict, JudgeVerdict)
    # ...but the failure is now recoverable.
    assert getattr(verdict, "errored", False) is True
    snapshot = {je.judge_name: je for je in judge_error_snapshot()}
    assert snapshot["py_broken"].errors == 1
    assert snapshot["py_broken"].last_error_type == "RuntimeError"
    clear_judge_errors()


async def test_errored_verdict_emits_no_judgement_through_goldfive() -> None:
    """The steerer's own emission path must not see a new event.

    The errored verdict is a JudgeVerdict SUBCLASS, so this pins the thing
    that subclass could plausibly break: goldfive picks ``verdict_kind``
    from the populated flavour, finds none, and stays silent.
    """
    from goldfive.steerer import DefaultSteerer

    clear_judge_errors()
    spec = _SpecStub(
        name="wire", mode="inline", body="stay on task", severity=goldfive.DriftSeverity.WARNING
    )
    judge = judge_spec_to_goldfive(spec, _aux_raising())
    emitted: list[Any] = []

    class _Sink:
        async def emit(self, event: Any) -> None:
            emitted.append(event)

    steerer = DefaultSteerer()
    steerer._sinks = [_Sink()]  # type: ignore[attr-defined]
    verdicts = await steerer.evaluate_judges(
        JudgeContext(reasoning_text="some reasoning"), judges=[judge]
    )
    assert getattr(verdicts[0], "errored", False) is True
    assert emitted == []
    clear_judge_errors()


# ---------------------------------------------------------------------------
# Python judge — loads + runs
# ---------------------------------------------------------------------------


async def test_python_judge_class_target_loads_and_runs() -> None:
    """A python-mode spec resolving to a Judge class loads, runs, re-pins name."""
    spec = _SpecStub(
        name="my_python_judge",
        mode="python",
        body=_PY_CLASS_PATH,
        severity=goldfive.DriftSeverity.WARNING,
    )
    # aux callable is irrelevant for python judges — pass a stub.
    judge = judge_spec_to_goldfive(spec, _aux_returning(""))

    # name is re-pinned to the spec, overriding the target's own name.
    assert judge.name == "my_python_judge"

    verdict = await judge.evaluate(JudgeContext(reasoning_text="this is DANGER territory"))
    assert verdict.drift_emitted is True
    # goldfive's JudgeVerdict normalises drift fields to the DriftKind /
    # DriftSeverity enums (StrEnums — still equal to their wire tokens).
    assert verdict.drift_kind == "custom"
    assert isinstance(verdict.drift_kind, str)
    assert isinstance(verdict.drift_kind, goldfive.DriftKind)
    assert verdict.severity == "critical"
    assert isinstance(verdict.severity, str)
    assert verdict.detail == "python judge tripped"

    # non-violating trace -> empty verdict.
    quiet = await judge.evaluate(JudgeContext(reasoning_text="all calm here"))
    assert quiet.drift_emitted is False


async def test_python_judge_callable_target_loads_and_runs() -> None:
    """A python-mode spec resolving to a bare evaluate callable runs."""
    spec = _SpecStub(
        name="callable_judge",
        mode="python",
        body=_PY_CALLABLE_PATH,
        severity=goldfive.DriftSeverity.INFO,
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning(""))
    assert judge.name == "callable_judge"
    verdict = await judge.evaluate(JudgeContext(reasoning_text="this is a BAD idea"))
    assert verdict.drift_emitted is True
    assert verdict.drift_kind == "custom"
    assert verdict.severity == "warning"
    assert verdict.detail == "callable judge tripped"


async def test_python_judge_dotted_form_also_resolves() -> None:
    """The ``pkg.mod.attr`` form (no colon) resolves too."""
    spec = _SpecStub(
        name="dotted",
        mode="python",
        body="tests.test_judge_runtime.sample_judge_callable_target",
        severity=goldfive.DriftSeverity.INFO,
    )
    judge = judge_spec_to_goldfive(spec, _aux_returning(""))
    verdict = await judge.evaluate(JudgeContext(reasoning_text="a BAD plan"))
    assert verdict.drift_emitted is True


# ---------------------------------------------------------------------------
# Builder — error handling
# ---------------------------------------------------------------------------


def test_builder_rejects_unknown_mode() -> None:
    spec = _SpecStub(name="x", mode="telepathy", body="...", severity=goldfive.DriftSeverity.INFO)
    with pytest.raises(ValueError, match="unknown mode"):
        judge_spec_to_goldfive(spec, _aux_returning(""))


def test_builder_rejects_empty_inline_body() -> None:
    spec = _SpecStub(name="x", mode="inline", body="   ", severity=goldfive.DriftSeverity.INFO)
    with pytest.raises(ValueError, match="criterion"):
        judge_spec_to_goldfive(spec, _aux_returning(""))


def test_builder_rejects_missing_python_module() -> None:
    spec = _SpecStub(
        name="x",
        mode="python",
        body="zicato.does_not_exist:Thing",
        severity=goldfive.DriftSeverity.INFO,
    )
    with pytest.raises(ImportError, match="does_not_exist"):
        judge_spec_to_goldfive(spec, _aux_returning(""))


def test_builder_rejects_missing_python_attr() -> None:
    spec = _SpecStub(
        name="x",
        mode="python",
        body="tests.test_judge_runtime:NoSuchSymbol",
        severity=goldfive.DriftSeverity.INFO,
    )
    with pytest.raises(AttributeError, match="NoSuchSymbol"):
        judge_spec_to_goldfive(spec, _aux_returning(""))


# ---------------------------------------------------------------------------
# disable_drift -> built-in suppression
# ---------------------------------------------------------------------------


def test_disable_drift_suppresses_matching_builtin() -> None:
    """A ``disable_drift`` kind drops the built-in judge that emits it."""
    full = {j.name for j in goldfive.builtin_judges.default_judges()}
    assert "tool_error" in full  # baseline: the built-in is normally present

    suppressed = builtin_judge_names_to_suppress((goldfive.DriftKind.TOOL_ERROR,))
    assert suppressed == {"tool_error"}

    kept = [j.name for j in default_judges_minus(suppressed)]
    assert "tool_error" not in kept
    # every OTHER built-in stays default-on.
    assert set(kept) == full - {"tool_error"}


def test_disable_drift_maps_reasoning_kind_to_reasoning_drift_judge() -> None:
    """OFF_TOPIC (a reasoning-judge kind) suppresses the reasoning_drift judge."""
    suppressed = builtin_judge_names_to_suppress((goldfive.DriftKind.OFF_TOPIC,))
    assert suppressed == {"reasoning_drift"}


def test_disable_drift_unknown_kind_is_noop() -> None:
    """A drift kind no built-in judge emits suppresses nothing (not an error)."""
    # NEW_WORK_DISCOVERED is not emitted by any built-in judge wrapper.
    suppressed = builtin_judge_names_to_suppress((goldfive.DriftKind.NEW_WORK_DISCOVERED,))
    assert suppressed == set()
    kept = [j.name for j in default_judges_minus(suppressed)]
    assert set(kept) == {j.name for j in goldfive.builtin_judges.default_judges()}


def test_disable_drift_accepts_string_kinds() -> None:
    """``disable_drift`` tolerates bare wire strings, not just enum members."""
    assert builtin_judge_names_to_suppress(("tool_error",)) == {"tool_error"}


def test_disable_drift_empty_keeps_all_builtins() -> None:
    assert builtin_judge_names_to_suppress(None) == set()
    assert builtin_judge_names_to_suppress(()) == set()
    kept = [j.name for j in default_judges_minus(set())]
    assert set(kept) == {j.name for j in goldfive.builtin_judges.default_judges()}


# ---------------------------------------------------------------------------
# assemble_judges — built-ins minus suppressed + custom
# ---------------------------------------------------------------------------


def test_assemble_judges_composes_builtins_and_custom() -> None:
    """``assemble_judges`` = (default builtins - suppressed) ++ custom judges."""
    custom = _SpecStub(
        name="my_inline",
        mode="inline",
        body="stay on task",
        severity=goldfive.DriftSeverity.WARNING,
    )
    judges = assemble_judges(
        entry_judges=(custom,),
        disable_drift=(goldfive.DriftKind.TOOL_ERROR,),
        aux_call_llm=_aux_returning(""),
    )
    names = [j.name for j in judges]
    # the suppressed built-in is gone...
    assert "tool_error" not in names
    # ...the other built-ins remain default-on...
    assert "reasoning_drift" in names
    assert "refusal" in names
    # ...and the custom inline judge is appended.
    assert "my_inline" in names
    # custom judge comes after the built-ins.
    assert names.index("my_inline") > names.index("reasoning_drift")


def test_assemble_judges_default_when_nothing_declared() -> None:
    """No custom judges + no disable_drift -> exactly goldfive's defaults."""
    judges = assemble_judges(entry_judges=None, disable_drift=None, aux_call_llm=_aux_returning(""))
    assert [j.name for j in judges] == [j.name for j in goldfive.builtin_judges.default_judges()]


def test_assemble_judges_all_conform_to_judge_protocol() -> None:
    """Every assembled judge satisfies the goldfive Judge protocol."""
    from goldfive.judges import Judge

    custom = _SpecStub(
        name="j",
        mode="python",
        body=_PY_CALLABLE_PATH,
        severity=goldfive.DriftSeverity.INFO,
    )
    judges = assemble_judges(
        entry_judges=(custom,), disable_drift=None, aux_call_llm=_aux_returning("")
    )
    for judge in judges:
        assert isinstance(judge, Judge)
