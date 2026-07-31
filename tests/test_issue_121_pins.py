"""Strict-xfail pins for issue #121 — a raising judge must not read as a silent one.

A board-declared process judge has exactly two honest answers: "the
criterion was violated" (drift) and "the criterion was not violated"
(silence). Today it has a third *outcome* — the judge's callable raised —
that is persisted as the second answer. :meth:`_InlineCriterionJudge.evaluate`
catches every exception from ``aux_call_llm``, logs a WARNING, and returns
a bare ``JudgeVerdict()``; goldfive's ``DefaultSteerer._emit_judgement``
emits NO event for an empty-default verdict; the reducer therefore writes
no ``custom:<judge_name>`` drift count; and
:func:`zicato.health.diagnostics.detect_dead_judge` — which infers "fired"
purely from those drift counts — reports the judge as one that "never
fired", the same words it uses for a healthy judge whose criterion was
never met.

Net: across ``loss.json``, ``events.jsonl``, the reflection capture corpus
and the loop-health findings there is *no* persisted byte that differs
between a judge that answered "no violation" N times and a judge that
raised N times. The only trace is a transient log line.

Each test below is an XPASS-strict pin: it fails today and must start
passing when the defect is fixed. They deliberately assert on BEHAVIOUR
(is the error distinguishable, does the health finding say so) rather than
on one particular field spelling, but they do commit to the names the
issue proposes so the fix has a concrete target.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from zicato.core.types import BoardEntry, DriftCount, JudgeSpec, LossProfile
from zicato.health.diagnostics import detect_dead_judge
from zicato.judge_runtime import judge_spec_to_goldfive

# ---------------------------------------------------------------------------
# Factories — real shapes, real constructor signatures
# ---------------------------------------------------------------------------


def _inline_spec(name: str = "no_fabricated_numbers") -> JudgeSpec:
    """A real inline :class:`JudgeSpec`, the shape a board declares."""
    return JudgeSpec(
        name=name,
        mode="inline",
        body="the reasoning must not invent a numeric figure it cannot source",
        severity="warning",
    )


def _ctx() -> Any:
    """A real goldfive :class:`JudgeContext` with judgeable reasoning."""
    from goldfive.judges import JudgeContext

    text = "The Q3 figure is 41.2% — I am fairly sure that is right."
    return JudgeContext(reasoning_text=text, transcript=(text,))


async def _raising_aux(system: str, user: str, model: str) -> str:
    """An ``aux_call_llm`` whose endpoint is misconfigured — the #121 scenario."""
    raise RuntimeError("ClientError: 404 model not found")


async def _ok_aux(system: str, user: str, model: str) -> str:
    """A healthy ``aux_call_llm`` whose judge finds no violation."""
    return "OK the reasoning sources its figure"


class _RecordingSink:
    """A structural :class:`~zicato.judge_runtime.io_capture.JudgeIOSink`."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self,
        judge_name: str,
        *,
        reasoning_text: str,
        transcript_window: tuple[str, ...],
        raw_response: str,
        drift_emitted: bool,
        kind: str,
        severity: str,
        detail: str,
    ) -> None:
        self.records.append(
            {
                "judge_name": judge_name,
                "reasoning_text": reasoning_text,
                "raw_response": raw_response,
                "drift_emitted": drift_emitted,
                "kind": kind,
                "severity": severity,
                "detail": detail,
            }
        )


def _board_entry(entry_id: str, judge_names: list[str]) -> BoardEntry:
    """A single-turn entry declaring the named in-run judges."""
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="summarise the quarter",
        judges=tuple(_inline_spec(name) for name in judge_names),
    )


def _loss(entry_id: str, generation_id: str, **extra: Any) -> LossProfile:
    """A minimal :class:`LossProfile` — the reducer's per-run output."""
    return LossProfile(
        run_id=f"run_{generation_id}_{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e1",
        drift_counts=extra.pop("drift_counts", ()),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=True,
        **extra,
    )


# ---------------------------------------------------------------------------
# Pin 1 — the verdict itself must carry the third state
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="issue #121: a raised aux call returns a bare JudgeVerdict(), "
    "byte-identical to a healthy judge's no-violation verdict",
)
def test_raising_judge_verdict_is_distinguishable_from_a_silent_one() -> None:
    """An exception and a negative result must not share a representation.

    Both halves run through the SAME builder every real run uses. The
    healthy half already holds today; only the raising half is the pin.
    """
    ctx = _ctx()

    healthy = judge_spec_to_goldfive(_inline_spec(), _ok_aux)
    healthy_verdict = asyncio.run(healthy.evaluate(ctx))
    assert healthy_verdict.drift_emitted is False
    assert getattr(healthy_verdict, "errored", False) is False

    broken = judge_spec_to_goldfive(_inline_spec(), _raising_aux)
    broken_verdict = asyncio.run(broken.evaluate(ctx))
    # It did not find a violation — but it did not find "no violation" either.
    assert broken_verdict.drift_emitted is False
    assert getattr(broken_verdict, "errored", False) is True
    assert "RuntimeError" in str(getattr(broken_verdict, "error", ""))


# ---------------------------------------------------------------------------
# Pin 2 — the reflection corpus must retain the failed call
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="issue #121: the except branch returns before the io_sink block, "
    "so a failed judge call leaves no record in the reflection corpus",
)
def test_raising_judge_call_is_captured_for_reflection() -> None:
    """Board reflection re-reads silent verdicts as missed-fire candidates.

    A call that never reached a verdict is not a missed fire, and an
    adjudicator that cannot see it will mis-read a broken endpoint as a
    judge whose criterion is too narrow.
    """
    sink = _RecordingSink()
    broken = judge_spec_to_goldfive(_inline_spec(), _raising_aux, io_sink=sink)
    asyncio.run(broken.evaluate(_ctx()))

    assert len(sink.records) == 1, "the failed call left no trace in the corpus"
    assert sink.records[0]["judge_name"] == "no_fabricated_numbers"
    assert sink.records[0]["drift_emitted"] is False


# ---------------------------------------------------------------------------
# Pin 3 — the error must survive into the run's persisted evidence
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="issue #121: LossProfile has no per-judge error provenance, so a "
    "judge that raised on every invocation is unrecoverable from artifacts",
)
def test_loss_profile_records_per_judge_error_provenance() -> None:
    """``loss.json`` must show that a declared judge errored on this run.

    ``per_judge_loss`` covers judges that FIRED; nothing covers judges that
    were invoked and failed. Without this the evidence is gone the moment
    the process log rotates.
    """
    from zicato.core.types import JudgeError  # noqa: PLC0415 — pinned future symbol

    loss = _loss(
        "e1",
        "v0",
        judge_errors=(
            JudgeError(
                judge_name="no_fabricated_numbers",
                invocations=34,
                errors=34,
                last_error="RuntimeError: ClientError: 404 model not found",
            ),
        ),
    )
    assert loss.judge_errors[0].errors == 34
    assert loss.judge_errors[0].invocations == 34


# ---------------------------------------------------------------------------
# Pin 4 — loop health must not call an erroring judge "never fired"
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="issue #121: detect_dead_judge infers 'fired' from drift counts "
    "alone, so a judge that raised 34/34 times reports as dead weight",
)
def test_health_distinguishes_an_erroring_judge_from_an_unmet_criterion() -> None:
    """ "raised on 34 of 34 invocations" is actionable; "never fired" is not.

    Two epochs, identical at the drift-count level: in both, judge
    ``broken`` produced no ``custom:`` drift. They must not produce the
    same finding.
    """
    board = [_board_entry("e1", ["lives", "broken"])]
    fired = DriftCount(kind="custom:lives", severity="warning", count=1)

    # Epoch A: 'broken' ran fine and its criterion was simply never met.
    # This half holds TODAY — it is what makes the contrast below a real one.
    quiet = {"v0": [_loss("e1", "v0", drift_counts=(fired,))]}
    quiet_findings = detect_dead_judge(quiet, board)
    assert [f.code for f in quiet_findings] == ["dead_judge"]
    assert quiet_findings[0].detail["dead_judges"] == ["broken"]

    from zicato.core.types import JudgeError  # noqa: PLC0415 — pinned future symbol

    # Epoch B: 'broken' raised on every single invocation.
    errored = {
        "v0": [
            _loss(
                "e1",
                "v0",
                drift_counts=(fired,),
                judge_errors=(
                    JudgeError(
                        judge_name="broken",
                        invocations=34,
                        errors=34,
                        last_error="RuntimeError: ClientError: 404 model not found",
                    ),
                ),
            )
        ]
    }
    errored_findings = detect_dead_judge(errored, board)
    codes = [f.code for f in errored_findings]
    assert "dead_judge" not in codes, "an erroring judge is broken, not dead weight"
    assert codes, "a judge that raised on every invocation must raise a finding"
    summary = " ".join(f.summary for f in errored_findings)
    assert "34" in summary and "broken" in summary
