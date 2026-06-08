"""Tests for the target_1_presentation file-findability process judge.

The judge (``zicato_examples.target_1_presentation.judges.FileFindabilityJudge``)
detects, *in-run*, the dominant presentation-tree failure: the agent
writes its slides under one slug and then cannot reliably FIND them
again, so the reviewer's ``read_presentation_files`` returns
``files_not_found`` / ``<error reading …>``, the debugger's
``find_presentation_files`` fuzzy-locate is invoked, and the reviewer
loops on ``read_presentation_files``. These tests:

1. drive the judge with synthetic :class:`~goldfive.judges.JudgeContext`
   snapshots (NO live LLM) modelling a pathological run and a clean run,
   asserting elevated severity on the former and silence on the latter;
2. confirm the judge attaches via the ``Judge.python`` authoring helper
   and resolves live through :func:`zicato.judge_runtime.judge_spec_to_goldfive`;
3. prove the judge's ``custom`` drift folds into the scalar loss through
   ``per_judge_weights[\"file_findability\"]`` — a run with file-finding
   failures scores strictly worse than a clean one.
"""

from __future__ import annotations

import goldfive
from goldfive.judges import JudgeContext, JudgeVerdict

from zicato.board.judges import Judge
from zicato.core.types import DriftCount, ScoringWeights
from zicato.judge_runtime import judge_spec_to_goldfive
from zicato.telemetry.reducer import compute_drift_loss
from zicato_examples.target_1_presentation.judges import (
    FILE_FINDABILITY_JUDGE_PATH,
    FILE_FINDABILITY_NAME,
    FileFindabilityJudge,
    file_findability_judge,
)

# ---------------------------------------------------------------------------
# Synthetic observation-point helpers (no live LLM)
# ---------------------------------------------------------------------------


def _tool_ctx(tool: str, output: object) -> JudgeContext:
    """A JudgeContext snapshot carrying one structured ``tool_event``."""
    return JudgeContext(extras={"tool_event": {"tool": tool, "output": output}})


def _read_ok() -> JudgeContext:
    """A successful ``read_presentation_files`` observation point."""
    return _tool_ctx(
        "read_presentation_files",
        {"outline.md": "# Waffles\n- slide 1", "speaker_notes.md": "...", "deck.md": "..."},
    )


def _read_not_found() -> JudgeContext:
    """A ``read_presentation_files`` call returning all-files-not-found."""
    return _tool_ctx(
        "read_presentation_files",
        {
            "outline.md": "<error reading /runs/waffle-deck/outline.md: not found>",
            "speaker_notes.md": "<error reading /runs/waffle-deck/speaker_notes.md: not found>",
            "deck.md": "<error reading /runs/waffle-deck/deck.md: not found>",
        },
    )


def _find_invoked() -> JudgeContext:
    """A ``find_presentation_files`` fuzzy-locate observation point."""
    return _tool_ctx(
        "find_presentation_files",
        {"matched_slug": "waffles", "files": ["outline.md"]},
    )


def _files_not_found_report() -> JudgeContext:
    """The reviewer's structured ``files_not_found`` report on the wire."""
    return _tool_ctx(
        "read_presentation_files",
        {"files_not_found": ["outline.md", "speaker_notes.md", "deck.md"]},
    )


async def _drain(judge: FileFindabilityJudge, contexts: list[JudgeContext]) -> list[JudgeVerdict]:
    """Run the judge over a sequence of observation points; collect verdicts."""
    verdicts: list[JudgeVerdict] = []
    for ctx in contexts:
        verdicts.append(await judge.evaluate(ctx))
    return verdicts


# ---------------------------------------------------------------------------
# Detector behaviour
# ---------------------------------------------------------------------------


async def test_clean_run_is_silent() -> None:
    """A write-then-read-at-first-attempt run trips no signal."""
    judge = FileFindabilityJudge()
    verdicts = await _drain(
        judge,
        [
            JudgeContext(reasoning_text="I'll write the waffle deck files now."),
            _read_ok(),
            JudgeContext(reasoning_text="Files read cleanly; reviewing the deck."),
        ],
    )
    assert all(v.drift_emitted is False for v in verdicts)
    assert all(v.drift_kind == "" for v in verdicts)


async def test_single_read_no_error_does_not_loop() -> None:
    """One successful read is not a loop and not a not-found."""
    judge = FileFindabilityJudge()
    [verdict] = await _drain(judge, [_read_ok()])
    assert verdict.drift_emitted is False


async def test_pathological_run_fires_and_escalates_to_critical() -> None:
    """The full failure signature trips all four signals, ending CRITICAL.

    Sequence models a real failing transcript: read returns not-found ->
    files_not_found report -> debugger's find -> reviewer retries the
    read (loop). Each *new* distinct signal emits one escalating drift;
    the final verdict is CRITICAL.
    """
    judge = FileFindabilityJudge()
    verdicts = await _drain(
        judge,
        [
            _read_not_found(),  # signals: read_not_found (+ files_not_found marker)
            _files_not_found_report(),  # structured report
            _find_invoked(),  # debugger fuzzy-locate
            _read_not_found(),  # second read -> loop
        ],
    )
    emitted = [v for v in verdicts if v.drift_emitted]
    # At least one drift per *distinct* signal that newly fired.
    assert emitted, "expected the pathological run to emit drift"
    # Every emitted verdict is a CUSTOM-kind drift attributed to this judge.
    assert all(v.drift_kind == str(goldfive.DriftKind.CUSTOM) for v in emitted)
    assert all(v.drift_kind == "custom" for v in emitted)
    # Severity escalates: the run reaches CRITICAL by the time all four
    # distinct signals have fired.
    assert any(v.severity == "critical" for v in emitted)
    # The accumulator saw every distinct signal.
    assert judge._active_signal_count() == 4
    # The first emission is the gentlest (INFO) — a single signal alone
    # is not yet the full pathology.
    assert emitted[0].severity == "info"
    # Detail names the signature for the dashboard / journal.
    assert "file findability" in emitted[-1].detail
    assert "files_not_found" in emitted[-1].detail


async def test_find_tool_alone_is_a_signal() -> None:
    """Invoking find_presentation_files at all is itself a failure signal."""
    judge = FileFindabilityJudge()
    [verdict] = await _drain(judge, [_find_invoked()])
    assert verdict.drift_emitted is True
    assert verdict.drift_kind == "custom"
    assert verdict.severity == "info"  # one signal -> INFO


async def test_emits_each_distinct_signal_exactly_once() -> None:
    """Re-observing an already-fired signal does not re-emit drift."""
    judge = FileFindabilityJudge()
    verdicts = await _drain(judge, [_find_invoked(), _find_invoked(), _find_invoked()])
    emitted = [v for v in verdicts if v.drift_emitted]
    assert len(emitted) == 1  # find fired once; repeats are silent


async def test_free_text_fallback_trips_without_structured_event() -> None:
    """A runner that narrates the failure in text (no tool_event) still trips."""
    judge = FileFindabilityJudge()
    verdicts = await _drain(
        judge,
        [
            JudgeContext(
                transcript=(
                    "reviewer: read_presentation_files returned files_not_found "
                    "for all three files",
                    "coordinator: dispatching find_presentation_files to locate the slug",
                )
            ),
        ],
    )
    emitted = [v for v in verdicts if v.drift_emitted]
    assert emitted
    assert emitted[-1].drift_kind == "custom"


# ---------------------------------------------------------------------------
# Real goldfive tool-ledger path — the artifact-fidelity fix
# ---------------------------------------------------------------------------
#
# goldfive does NOT set ``ctx.extras["tool_event"]`` and does NOT dispatch
# custom judges at tool-observation points; it records every tool call on
# ``session.recent_events`` (``kind == "tool_observed"``) and dispatches
# custom judges only at reasoning points, where that session is reachable
# as ``ctx.session_state``. These tests model that REAL shape — the
# detector must grade the agent's actual tool round-trips, not its
# narration.


class _FakeSession:
    """Minimal stand-in for a goldfive ``Session`` recent-events ledger."""

    def __init__(self, recent_events: list[dict[str, object]]) -> None:
        self.recent_events = recent_events


def _tool_observed(
    tool_name: str,
    *,
    result_preview: str = "",
    error_message: str = "",
    ts_ms: int = 0,
    args_preview: str = "",
) -> dict[str, object]:
    """Build one goldfive ``tool_observed`` recent-events entry."""
    return {
        "kind": "tool_observed",
        "ts_ms": ts_ms,
        "agent_name": "reviewer",
        "task_id": "t1",
        "tool_name": tool_name,
        "args_preview": args_preview,
        "result_preview": result_preview,
        "is_error": bool(error_message) or "not found" in result_preview,
        "error_message": error_message,
    }


def _reasoning_ctx_with_session(
    reasoning_text: str, recent_events: list[dict[str, object]]
) -> JudgeContext:
    """A reasoning observation point carrying a live session tool ledger.

    This is exactly the JudgeContext shape goldfive's
    ``DriftObserver._dispatch_custom_judges`` builds: reasoning text +
    ``session_state`` (with the real ``recent_events`` buffer) and NO
    ``extras["tool_event"]``.
    """
    return JudgeContext(
        reasoning_text=reasoning_text,
        session_state=_FakeSession(recent_events),  # type: ignore[arg-type]
    )


async def test_reads_real_session_tool_ledger() -> None:
    """The judge trips on goldfive's real ``recent_events`` tool ledger.

    No ``extras["tool_event"]`` is set (goldfive never sets it); the
    structured signal must come from ``ctx.session_state.recent_events``.
    """
    judge = FileFindabilityJudge()
    events: list[dict[str, object]] = [
        _tool_observed(
            "read_presentation_files",
            result_preview="{'outline.md': '<error reading outline.md: not found>'}",
            ts_ms=1,
        ),
        _tool_observed(
            "find_presentation_files",
            result_preview="{'matched_slug': 'waffles'}",
            ts_ms=2,
        ),
    ]
    verdict = await judge.evaluate(_reasoning_ctx_with_session("Reviewing the deck now.", events))
    assert verdict.drift_emitted is True
    assert verdict.drift_kind == "custom"
    assert judge._saw_read_not_found is True
    assert judge._saw_find_tool is True


async def test_session_ledger_dedups_across_observation_points() -> None:
    """Re-snapshotting the same ring buffer never double-counts a read.

    goldfive hands the judge a fresh JudgeContext per reasoning point, but
    the ``recent_events`` ring persists the same entries until trimmed.
    Folding the SAME read twice must not synthesise a phantom retry loop.
    """
    judge = FileFindabilityJudge()
    one_read = [
        _tool_observed("read_presentation_files", result_preview="{'deck.md': 'ok'}", ts_ms=1)
    ]
    # Two consecutive reasoning observation points see the identical ledger.
    await judge.evaluate(_reasoning_ctx_with_session("step 1", one_read))
    await judge.evaluate(_reasoning_ctx_with_session("step 2", list(one_read)))
    # The single real read was counted once — no fabricated loop.
    assert judge._read_call_count == 1
    assert judge._saw_read_loop is False


async def test_two_real_reads_are_a_loop_with_consistent_reason() -> None:
    """Two real reads -> loop signal whose reason matches the real count.

    Regression for the headline defect: the loop reason previously read
    ``called 0× (retry loop)`` because the loop bit came from narration
    while the structured counter stayed at 0. The count and the reason
    can no longer disagree — the loop is derived only from the structured
    read count.
    """
    judge = FileFindabilityJudge()
    events = [
        _tool_observed(
            "read_presentation_files",
            result_preview="{'deck.md': '<error reading: not found>'}",
            ts_ms=1,
        ),
        _tool_observed(
            "read_presentation_files",
            result_preview="{'deck.md': '<error reading: not found>'}",
            ts_ms=2,
        ),
    ]
    await judge.evaluate(_reasoning_ctx_with_session("retrying the read", events))
    assert judge._read_call_count == 2
    assert judge._saw_read_loop is True
    reason = judge._reason(judge._active_signal_count())
    assert "called 2× (retry loop)" in reason
    assert "called 0×" not in reason


async def test_narration_cannot_fabricate_loop_when_tools_are_structured() -> None:
    """With a real tool ledger present, narration cannot manufacture signals.

    The agent's chain-of-thought mentions the tool names repeatedly, but
    the structured ledger shows a single clean read. The detector grades
    the artifact (one clean read), not the narration: no drift, no loop,
    no self-contradictory reason.
    """
    judge = FileFindabilityJudge()
    clean_read = [
        _tool_observed("read_presentation_files", result_preview="{'deck.md': 'ok'}", ts_ms=1)
    ]
    chatty = (
        "I will call read_presentation_files, then read_presentation_files again, "
        "and if that fails files_not_found I'll run find_presentation_files."
    )
    verdict = await judge.evaluate(_reasoning_ctx_with_session(chatty, clean_read))
    # The narration name-drops every failure token; the real ledger is clean.
    assert verdict.drift_emitted is False
    assert judge._saw_read_loop is False
    assert judge._saw_find_tool is False
    assert judge._saw_read_not_found is False


# ---------------------------------------------------------------------------
# Attach surface — Judge.python + judge_runtime resolution
# ---------------------------------------------------------------------------


def test_factory_helper_constructs_judge() -> None:
    judge = file_findability_judge()
    assert isinstance(judge, FileFindabilityJudge)
    assert judge.name == FILE_FINDABILITY_NAME


def test_judge_python_spec_round_trips() -> None:
    """``Judge.python`` builds a valid JudgeSpec pointing at the detector."""
    spec = Judge.python(
        FILE_FINDABILITY_NAME,
        FILE_FINDABILITY_JUDGE_PATH,
        severity=goldfive.DriftSeverity.WARNING,
    )
    assert spec.name == FILE_FINDABILITY_NAME
    assert spec.mode == "python"
    assert spec.body == FILE_FINDABILITY_JUDGE_PATH


async def test_resolves_live_through_judge_runtime() -> None:
    """The spec resolves to a live goldfive judge with the spec's name."""

    async def _never_called(system: str, user: str, model: str) -> str:  # noqa: ARG001
        raise AssertionError("python judge must not touch the aux LLM")

    spec = Judge.python(
        FILE_FINDABILITY_NAME,
        FILE_FINDABILITY_JUDGE_PATH,
        severity=goldfive.DriftSeverity.WARNING,
    )
    live = judge_spec_to_goldfive(spec, _never_called)
    # name is re-pinned to the spec name -> JudgementEmitted.judge_name.
    assert live.name == FILE_FINDABILITY_NAME
    # And it actually evaluates: a not-found read trips a custom drift.
    verdict = await live.evaluate(_read_not_found())
    assert verdict.drift_emitted is True
    assert verdict.drift_kind == "custom"


# ---------------------------------------------------------------------------
# Fold into the scalar loss
# ---------------------------------------------------------------------------


def _weights(judge_weight: float) -> ScoringWeights:
    """ScoringWeights mirroring the example scoring.json, parametric weight."""
    return ScoringWeights(
        severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0},
        per_judge_weights={FILE_FINDABILITY_NAME: judge_weight},
        default_judge_weight=1.0,
    )


def test_file_finding_failure_scores_worse_than_clean() -> None:
    """A run with file-finding failures has strictly higher (worse) loss.

    The judge's adverse verdicts are attributed by the reducer to the
    ``custom:file_findability`` drift kind; ``compute_drift_loss`` folds
    that through ``severity_weights[severity] * per_judge_weights[name] *
    count``. Here we feed the two outcomes' drift_counts directly to
    isolate the loss-fold contract.
    """
    weights = _weights(2.0)

    # Clean run: no file-findability drift at all.
    clean_loss = compute_drift_loss(
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )

    # Failing run: the four distinct signals fired as the judge would
    # have emitted them (one INFO, one WARNING, two CRITICAL as severity
    # escalated), all attributed to custom:file_findability.
    failing_counts = (
        DriftCount(kind=f"custom:{FILE_FINDABILITY_NAME}", severity="info", count=1),
        DriftCount(kind=f"custom:{FILE_FINDABILITY_NAME}", severity="warning", count=1),
        DriftCount(kind=f"custom:{FILE_FINDABILITY_NAME}", severity="critical", count=2),
    )
    failing_loss = compute_drift_loss(
        drift_counts=failing_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
    )

    assert clean_loss == 0.0
    assert failing_loss > clean_loss
    # Exact fold: 2.0 * (1*1 + 3*1 + 10*2) = 2.0 * 24 = 48.0
    assert failing_loss == 48.0


def test_per_judge_weight_amplifies_the_penalty() -> None:
    """Raising per_judge_weights[file_findability] increases the loss."""
    counts = (DriftCount(kind=f"custom:{FILE_FINDABILITY_NAME}", severity="warning", count=1),)
    low = compute_drift_loss(counts, 0, 0.0, 0, _weights(1.0))
    high = compute_drift_loss(counts, 0, 0.0, 0, _weights(2.0))
    assert high > low
    assert high == 2.0 * low
