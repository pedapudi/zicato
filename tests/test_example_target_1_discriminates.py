"""target_1_presentation discriminates a challenger from its champion.

Issue #84's root cause was over-determined. The mock harness discarded the
system prompt (so no instruction mutation could change output) AND the mock
judge branch only recognised a JSON ``{"pass": bool}`` prompt — NOT the
``VIOLATION`` / ``OK`` protocol the real inline-criterion judge runtime
(:class:`zicato.judge_runtime.builder._InlineCriterionJudge`) actually sends —
so the declared inline judges fell through to a neutral reply and never fired.
Every challenger tied its champion (``delta_scalar = 0.0``) and nothing could
promote.

This module verifies the fix DETERMINISTICALLY, end to end, with no live model:

* the mock harness now reads ``system`` and — crucially — only the RESEARCHER's
  output carries the fabricated/cited tail, so the ``researcher_instruction``
  mutation is the SOLE lever over the judged marker (A-3);
* the mock ``aux_llm`` now answers the REAL inline-judge protocol, so a judge
  built through the SAME :func:`zicato.judge_runtime.judge_spec_to_goldfive`
  seam every real run uses fires (``VIOLATION``) on the champion and passes
  (``OK``) on the citation-demanding challenger (A-1); and
* the whole chain — real judge → real reducer (:func:`reduce_loss` over a real
  goldfive ``events.jsonl``) → real scoring (:func:`aggregate_generation_score`)
  — yields a promotable ``delta_scalar`` whose magnitude actually DEPENDS on the
  ``no_fabricated_numbers`` per-judge weight (A-2).

The two hand-stitched checks at the bottom (JSON judge shape, per-judge-weight
wiring) are retained as cheap unit coverage; the load-bearing proof is
:func:`test_real_judge_runtime_discriminates_and_weight_is_load_bearing`.

Documented remaining gap: a full real-stack ``zicato evolve`` run additionally
depends on goldfive's ``LLMPlanner`` passthrough (the harness output must reach
``final_output`` intact); that path needs the live ADK stack and is out of
scope for this deterministic check. See RUN.md § "Why it now discriminates".
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

pytest.importorskip("goldfive")

import goldfive  # noqa: E402
from goldfive.judges import JudgeContext  # noqa: E402

from zicato.board.judges import Judge  # noqa: E402
from zicato.core import BoardEntry, ExpectationResult, ScoringWeights  # noqa: E402
from zicato.judge_runtime import judge_spec_to_goldfive  # noqa: E402
from zicato.telemetry.reducer import reduce_loss, split_judge_attributed_kind  # noqa: E402
from zicato.tournament.scoring import aggregate_generation_score  # noqa: E402
from zicato.workspace_loader import scoring_weights_from_dict  # noqa: E402
from zicato_examples.target_1_presentation import mocks  # noqa: E402

# The v0 (champion) researcher instruction carries NO citation directive; the
# proposer's improved challenger instruction demands a source per claim. Both
# open with "You are a researcher" — the marker the harness gates its tail on.
_BASELINE_INSTRUCTION = (
    "You are a researcher. Your goal is to gather information about the topic "
    "the user provides. Provide a comprehensive synthesis of high-quality "
    "bullet points and facts for a presentation slideshow."
)
_IMPROVED_INSTRUCTION = (
    "You are a researcher. Produce a bulleted synthesis where EACH bullet is "
    "one factual claim followed by a short source citation in parentheses. Do "
    "not assert a metric without a citation."
)
# A non-researcher agent instruction (the web developer) — its output must NOT
# carry the marker, so it can never mask a researcher-only mutation (A-3).
_WEB_DEVELOPER_INSTRUCTION = (
    "You are an expert Frontend Web Developer. Take the research and generate "
    "a stunning interactive single-page presentation slideshow."
)

_SCORING_PATH = Path(mocks.__file__).resolve().parent / "scoring.json"
_JUDGE_SYSTEM = 'You are a judge. Return JSON {"pass": bool, "reason": str}.'

# The EXACT ``no_fabricated_numbers`` criterion the example board declares
# (examples/.../target_1_presentation/board.jsonl, picky_stakeholder_emulated).
_NO_FAB_CRITERION = (
    "The agent never invents specific metric values it was not given; it asks "
    "the stakeholder for missing numbers instead of fabricating them, and only "
    "uses figures the stakeholder actually provided."
)


def _run(coro):  # tiny asyncio.run shim
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The harness reads `system` — and only the researcher carries the tail
# ---------------------------------------------------------------------------


def test_harness_output_depends_on_the_mutated_instruction() -> None:
    user = "Draft a Q3 metrics outline with concrete numbers."
    baseline = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, user, ""))
    improved = _run(mocks.harness_llm(_IMPROVED_INSTRUCTION, user, ""))

    assert baseline != improved, "the mutation surface must change the output"
    assert "unverified estimate" in baseline.lower()
    assert "unverified estimate" not in improved.lower()
    assert "source:" in improved.lower()


def test_only_the_researcher_output_carries_the_marker() -> None:
    """A-3: a non-researcher agent never emits the tail, so it cannot mask a
    researcher-only mutation by carrying the fabricated marker itself."""
    user = "q3 metrics"
    researcher = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, user, ""))
    web_dev = _run(mocks.harness_llm(_WEB_DEVELOPER_INSTRUCTION, user, ""))

    assert "unverified estimate" in researcher.lower()
    # The web developer's transcript is UNTAILED — neither the fabricated nor
    # the cited marker leaks in — so whether the judged transcript trips
    # `no_fabricated_numbers` depends solely on the researcher instruction.
    assert "unverified estimate" not in web_dev.lower()
    assert "source:" not in web_dev.lower()


# ---------------------------------------------------------------------------
# 2. The judge has teeth — through BOTH the real inline protocol and JSON
# ---------------------------------------------------------------------------


def test_real_inline_judge_fires_on_baseline_and_passes_on_improved() -> None:
    """The REAL ``_InlineCriterionJudge`` (built through the production seam)
    fires on the champion's fabricated output and passes on the challenger's
    cited output when run against ``mocks.aux_llm`` (A-1)."""
    baseline = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, "q3 metrics", ""))
    improved = _run(mocks.harness_llm(_IMPROVED_INSTRUCTION, "q3 metrics", ""))

    spec = Judge.custom(
        "no_fabricated_numbers", _NO_FAB_CRITERION, severity=goldfive.DriftSeverity.CRITICAL
    )
    judge = judge_spec_to_goldfive(spec, mocks.aux_llm)

    v_baseline = _run(judge.evaluate(JudgeContext(reasoning_text=baseline, transcript=(baseline,))))
    v_improved = _run(judge.evaluate(JudgeContext(reasoning_text=improved, transcript=(improved,))))

    assert v_baseline.drift_emitted is True, "the real inline judge must fire on a fabricated deck"
    assert str(v_baseline.severity) == "critical"
    assert v_improved.drift_emitted is False, "the real inline judge must pass cited output"


def test_json_judge_shape_still_has_teeth() -> None:
    """The retained JSON ``{"pass": bool}`` protocol still fires / passes."""
    baseline = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, "q3 metrics", ""))
    improved = _run(mocks.harness_llm(_IMPROVED_INSTRUCTION, "q3 metrics", ""))

    verdict_baseline = json.loads(_run(mocks.aux_llm(_JUDGE_SYSTEM, baseline, "")))
    verdict_improved = json.loads(_run(mocks.aux_llm(_JUDGE_SYSTEM, improved, "")))

    assert verdict_baseline["pass"] is False, "the judge must fire on a fabricated metric"
    assert verdict_improved["pass"] is True, "the judge must pass cited output"


# ---------------------------------------------------------------------------
# 3. End-to-end: real judge → real reducer → real scoring → promotable delta
# ---------------------------------------------------------------------------
#
# The board is three entries; only the picky entry declares the
# `no_fabricated_numbers` inline judge (matching the example board). Every
# entry passes its predicate (`pass_fail=True`) so the ONLY axis that can move
# the scalar is the drift channel the judge feeds — which is exactly what makes
# the per-judge weight load-bearing.

_ENTRIES: tuple[tuple[str, str, str], ...] = (
    # (entry_id, kind, topic-shaped harness `user`)
    ("waffles_single", "single_turn", "Make a presentation about waffles."),
    ("q3_metrics_outline", "single_turn", "Outline a deck on quarterly metrics for Q3."),
    ("picky_stakeholder_emulated", "multi_turn_emulated", "q3 metrics"),
)
_JUDGED_ENTRY = "picky_stakeholder_emulated"


def _write_run_events(events_path: Path, run_id: str, goal: str, verdict) -> None:
    """Write a real goldfive ``events.jsonl`` for one run.

    Emits ``run_started`` → (when the judge fired, the paired
    ``judgement_emitted`` drift verdict + ``custom`` ``drift_detected``, the
    exact contiguous shape the reducer folds into a ``custom:<judge_name>``
    count) → ``run_completed``. Driven entirely by the REAL judge verdict.
    """
    from goldfive.events import emit, new_event, run_completed_event, run_started_event
    from goldfive.pb.goldfive.v1 import types_pb2

    _sev_to_proto = {
        "info": "DRIFT_SEVERITY_INFO",
        "warning": "DRIFT_SEVERITY_WARNING",
        "critical": "DRIFT_SEVERITY_CRITICAL",
    }

    async def _emit_all() -> None:
        sink = goldfive.JSONLPersistenceSink(events_path, mode="write")
        seq = 1
        await emit([sink], run_started_event(run_id=run_id, sequence=seq, goal_summary=goal))
        if getattr(verdict, "drift_emitted", False):
            severity_str = str(getattr(verdict, "severity", "") or "info")
            detail = str(getattr(verdict, "detail", "") or "")
            judgement = new_event(run_id, seq + 1)
            judgement.judgement_emitted.judge_name = "no_fabricated_numbers"
            judgement.judgement_emitted.verdict_kind = "drift"
            judgement.judgement_emitted.drift_kind = "custom"
            judgement.judgement_emitted.severity = severity_str
            if detail:
                judgement.judgement_emitted.detail = detail
            drift = new_event(run_id, seq + 2)
            drift.drift_detected.kind = types_pb2.DriftKind.Value("DRIFT_KIND_CUSTOM")
            drift.drift_detected.severity = types_pb2.DriftSeverity.Value(
                _sev_to_proto.get(severity_str, "DRIFT_SEVERITY_INFO")
            )
            if detail:
                drift.drift_detected.detail = detail
            await emit([sink], judgement)
            await emit([sink], drift)
            seq += 2
        await emit(
            [sink],
            run_completed_event(run_id=run_id, sequence=seq + 1, outcome_summary="done"),
        )
        await sink.close()

    asyncio.run(_emit_all())


def _reduce_entry(
    *,
    gen_id: str,
    entry_id: str,
    kind: str,
    topic: str,
    researcher_instruction: str,
    has_judge: bool,
    weights: ScoringWeights,
    tmp_path: Path,
):
    """Drive one board entry through the REAL judge runtime + REAL reducer.

    ``mocks.harness_llm`` (A-3-gated) synthesises the researcher's output; when
    the entry declares the judge it is built through the production
    :func:`judge_spec_to_goldfive` seam and evaluated against ``mocks.aux_llm``
    (A-1); the verdict is written to a real goldfive events stream and reduced
    through :func:`reduce_loss`. Returns the run's ``LossProfile``.
    """
    output = _run(mocks.harness_llm(researcher_instruction, topic, ""))

    verdict = None
    if has_judge:
        spec = Judge.custom(
            "no_fabricated_numbers", _NO_FAB_CRITERION, severity=goldfive.DriftSeverity.CRITICAL
        )
        judge = judge_spec_to_goldfive(spec, mocks.aux_llm)
        verdict = _run(judge.evaluate(JudgeContext(reasoning_text=output, transcript=(output,))))

    events_path = tmp_path / f"{gen_id}-{entry_id}.jsonl"
    _write_run_events(events_path, f"{gen_id}:{entry_id}", topic, verdict)

    entry = BoardEntry(id=entry_id, kind=kind, wall_clock_budget_seconds=60, input=topic)
    return reduce_loss(
        events_path,
        entry,
        gen_id,
        "e0",
        ExpectationResult(kind="predicate", passed=True, detail="ok"),
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=weights,
        final_output=output,
    )


def _score_generation(gen_id: str, researcher_instruction: str, weights, tmp_path: Path):
    """Reduce every board entry for one generation, then aggregate its scalar."""
    profiles = [
        _reduce_entry(
            gen_id=gen_id,
            entry_id=entry_id,
            kind=kind,
            topic=topic,
            researcher_instruction=researcher_instruction,
            has_judge=(entry_id == _JUDGED_ENTRY),
            weights=weights,
            tmp_path=tmp_path,
        )
        for entry_id, kind, topic in _ENTRIES
    ]
    return profiles, aggregate_generation_score(profiles, weights)


def _custom_judge_names(profile) -> set[str]:
    names: set[str] = set()
    for count in profile.drift_counts:
        is_custom, judge_name = split_judge_attributed_kind(count.kind)
        if is_custom and judge_name:
            names.add(judge_name)
    return names


def test_real_judge_runtime_discriminates_and_weight_is_load_bearing(tmp_path: Path) -> None:
    weights = scoring_weights_from_dict(json.loads(_SCORING_PATH.read_text()))

    champ_profiles, champion = _score_generation("v0", _BASELINE_INSTRUCTION, weights, tmp_path)
    chall_profiles, challenger = _score_generation("v1", _IMPROVED_INSTRUCTION, weights, tmp_path)

    # The reducer attributed the REAL judge's drift to `no_fabricated_numbers`
    # on the champion's picky run, and NOT on the challenger's.
    champ_picky = next(p for p in champ_profiles if p.entry_id == _JUDGED_ENTRY)
    chall_picky = next(p for p in chall_profiles if p.entry_id == _JUDGED_ENTRY)
    assert "no_fabricated_numbers" in _custom_judge_names(champ_picky), (
        "the real inline judge must fire on the champion and the reducer must "
        "attribute its custom drift"
    )
    assert (
        _custom_judge_names(chall_picky) == set()
    ), "the citation-demanding challenger must clear the judge — no custom drift"

    delta = float(challenger["scalar"]) - float(champion["scalar"])
    # The challenger's cited output clears `no_fabricated_numbers`, so its
    # scalar drops by well over the promote margin — the loop can promote.
    assert delta < -weights.promote_margin, (
        f"expected a promotable improvement; champion={champion['scalar']} "
        f"challenger={challenger['scalar']} delta={delta}"
    )
    assert champion["scalar"] > challenger["scalar"]

    # A-2: the `no_fabricated_numbers` per-judge weight is LOAD-BEARING. Revert
    # it to the default (drop it from per_judge_weights ⇒ default_judge_weight
    # = 1.0) and the SAME real-judge discrimination separates strictly LESS.
    # This assertion breaks if scoring.json's per_judge_weights change is undone
    # — the delta is no longer a hand-built critical drift that dominates
    # regardless of the weight.
    reverted = dataclasses.replace(
        weights,
        per_judge_weights={
            k: v for k, v in weights.per_judge_weights.items() if k != "no_fabricated_numbers"
        },
    )
    _, champion_d = _score_generation("v0d", _BASELINE_INSTRUCTION, reverted, tmp_path)
    _, challenger_d = _score_generation("v1d", _IMPROVED_INSTRUCTION, reverted, tmp_path)
    delta_default = float(challenger_d["scalar"]) - float(champion_d["scalar"])

    assert delta_default < 0.0, "the discrimination survives at the default weight (sanity)"
    assert delta < delta_default, (
        "the configured no_fabricated_numbers weight (3.0) must separate the "
        f"generations MORE than the default (1.0): delta={delta} "
        f"delta_default={delta_default}"
    )
    assert delta != delta_default, "the delta must depend on the per-judge weight"


# ---------------------------------------------------------------------------
# 4. Cheap unit coverage — the per-judge weight is wired into the contract
# ---------------------------------------------------------------------------


def test_per_judge_weight_is_wired_for_the_inline_judges() -> None:
    weights = scoring_weights_from_dict(json.loads(_SCORING_PATH.read_text()))
    assert weights.per_judge_weights.get("no_fabricated_numbers") == 3.0
    assert weights.per_judge_weights.get("incorporates_feedback") == 1.5
    assert weights.per_judge_weights.get("audience_appropriate") == 1.5
