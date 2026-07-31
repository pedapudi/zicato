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
and the loop-health findings there was *no* persisted byte that differed
between a judge that answered "no violation" N times and a judge that
raised N times. The only trace was a transient log line.

The first four tests were written as XPASS-strict pins against that defect
and are now the regression suite for its fix: a per-judge error register at
zicato's judge boundary, an errored verdict, the failed call captured for
reflection, :attr:`~zicato.core.loss.LossProfile.judge_errors` on the
persisted profile, and a distinct ``judge_erroring`` health finding. The
tests after them cover the layers BETWEEN those two ends — the loss.json
round trip, the replicate fold, a judge that errored on only some
invocations, and the terminal warning — because provenance that survives
the judge but not the artifact, the fold, or the report is provenance the
operator never sees.

One deviation from the pinned spelling: :class:`JudgeError` carries
``last_error_type`` (``"RuntimeError"``) rather than a ``last_error``
string holding type + message. ``loss.json`` is a scored, indexed artifact
and an endpoint's error text can carry request ids and URLs; the type is
what routes the operator, and the verbatim message is retained where the
detail belongs — the WARNING log and the ``judge_io.jsonl`` error entry
(pin 2). The behaviour each test asserts is unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

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


def test_loss_profile_records_per_judge_error_provenance() -> None:
    """``loss.json`` must show that a declared judge errored on this run.

    ``per_judge_loss`` covers judges that FIRED; nothing covers judges that
    were invoked and failed. Without this the evidence is gone the moment
    the process log rotates.
    """
    from zicato.core.types import JudgeError  # noqa: PLC0415 — local to the pin

    loss = _loss(
        "e1",
        "v0",
        judge_errors=(
            JudgeError(
                judge_name="no_fabricated_numbers",
                invocations=34,
                errors=34,
                last_error_type="RuntimeError",
            ),
        ),
    )
    assert loss.judge_errors[0].errors == 34
    assert loss.judge_errors[0].invocations == 34


# ---------------------------------------------------------------------------
# Pin 4 — loop health must not call an erroring judge "never fired"
# ---------------------------------------------------------------------------


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

    from zicato.core.types import JudgeError  # noqa: PLC0415 — local to the pin

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
                        last_error_type="RuntimeError",
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


# ---------------------------------------------------------------------------
# The layers between the judge and the finding
# ---------------------------------------------------------------------------


def test_judge_errors_round_trip_through_loss_json(tmp_path: Any) -> None:
    """The provenance has to survive the artifact, not just the process.

    Also pins the back-compat half: a ``loss.json`` written before the field
    existed carries no key and must still load.
    """
    import json

    from zicato.core.types import JudgeError  # noqa: PLC0415 — local to the pin
    from zicato.telemetry.reducer import read_loss_profile, write_loss_profile

    path = tmp_path / "loss.json"
    write_loss_profile(
        _loss(
            "e1",
            "v0",
            judge_errors=(
                JudgeError(
                    judge_name="broken",
                    invocations=34,
                    errors=34,
                    last_error_type="RuntimeError",
                ),
            ),
        ),
        path,
    )
    reloaded = read_loss_profile(path)
    assert reloaded.judge_errors[0].judge_name == "broken"
    assert reloaded.judge_errors[0].errors == 34
    assert reloaded.judge_errors[0].last_error_type == "RuntimeError"

    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["judge_errors"]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    assert read_loss_profile(legacy).judge_errors == ()


def test_replicate_fold_sums_judge_errors_rather_than_meaning_them() -> None:
    """A broken judge must not look less broken as K grows.

    Every other field in the fold is a mean; these are event counts, and
    meaning them would report "8.5 errors" for a duel in which one replicate
    of four failed 34 times — and would shrink toward zero with K.
    """
    from zicato.core.types import JudgeError  # noqa: PLC0415 — local to the pin
    from zicato.tournament.unit_cache import _average_losses

    def _replicate(errors: int) -> LossProfile:
        return _loss(
            "e1",
            "v0",
            judge_errors=(
                JudgeError(
                    judge_name="broken",
                    invocations=10,
                    errors=errors,
                    last_error_type="TimeoutError" if errors else "",
                ),
            )
            if errors
            else (),
        )

    folded = _average_losses([{"e1": _replicate(34)}, {"e1": _replicate(0)}])
    assert folded["e1"].judge_errors[0].errors == 34
    assert folded["e1"].judge_errors[0].invocations == 10
    assert folded["e1"].judge_errors[0].last_error_type == "TimeoutError"

    assert _average_losses([{"e1": _replicate(0)}, {"e1": _replicate(0)}])["e1"].judge_errors == ()


def test_a_partially_erroring_judge_is_broken_not_dead() -> None:
    """Errors on SOME invocations still disqualify the "dead weight" reading.

    A judge that fired once and raised nine times is not dead — it is a
    signal measured on a tenth of the evidence the operator thinks it has.
    """
    from zicato.core.types import JudgeError  # noqa: PLC0415 — local to the pin

    board = [_board_entry("e1", ["flaky", "quiet"])]
    fired = DriftCount(kind="custom:flaky", severity="warning", count=1)
    losses = {
        "v0": [
            _loss(
                "e1",
                "v0",
                drift_counts=(fired,),
                judge_errors=(
                    JudgeError(
                        judge_name="flaky",
                        invocations=10,
                        errors=9,
                        last_error_type="TimeoutError",
                    ),
                ),
            )
        ]
    }
    findings = {f.code: f for f in detect_dead_judge(losses, board)}
    assert set(findings) == {"judge_erroring", "dead_judge"}
    assert findings["judge_erroring"].detail["erroring_judges"] == ["flaky"]
    assert "9/10" in findings["judge_erroring"].summary
    # 'quiet' answered every time; its silence is a real verdict, and the
    # finding now says so rather than leaving the operator to wonder.
    assert findings["dead_judge"].detail["dead_judges"] == ["quiet"]
    assert "NO call failures" in findings["dead_judge"].detail["recommendation"]


def test_orchestrator_lifts_judge_erroring_onto_the_terminal(caplog: Any) -> None:
    """A finding the operator only sees in the round's JSON is not surfaced."""
    import logging
    from dataclasses import dataclass, field

    from zicato.orchestrator import _warn_erroring_judges

    @dataclass
    class _Finding:
        code: str
        summary: str
        detail: dict[str, Any] = field(default_factory=dict)

    @dataclass
    class _Health:
        findings: tuple[_Finding, ...]

    health = _Health(
        findings=(
            _Finding(
                code="judge_erroring",
                summary="1 board-declared judge(s) FAILED to answer: 'safety' raised on 34/34",
                detail={"recommendation": "check the judge endpoint"},
            ),
        )
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        _warn_erroring_judges("epoch_x", 3, health)
    assert "DECLARED JUDGE RAISED" in caplog.text
    assert "34/34" in caplog.text
    assert "check the judge endpoint" in caplog.text
