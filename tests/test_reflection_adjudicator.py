"""Adjudicator — the oracle known-answer, cache idempotency, collusion guards.

The spine of pillar 3. The ORACLE: a corpus with a PLANTED violation in the
captured judge input + a judge double that NEVER fired + an always-correct
adjudicator ⇒ the verdict is FN and names the planted span. Its MIRROR: a
trigger-happy judge that fired on a clean transcript ⇒ FP. Plus the
non-negotiable guards — HARD identity refusal, SOFT model-string warning, the
idempotent adjudication cache (second pass = zero adjudicator calls), the
one-retry-then-ambiguous protocol (never raises), severity tracked apart from
detection, and the fidelity ladder riding through onto every verdict.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from zicato.core import RuntimeConfig, ScoringWeights
from zicato.core.workspace import reflection_adjudication_path
from zicato.judge_runtime.io_capture import JudgeIOFileSink, judge_io_path_for_loss
from zicato.reflection.adjudicator import (
    ADJUDICATOR_PROMPT_VERSION,
    VERDICT_AMBIGUOUS,
    VERDICT_FN,
    VERDICT_FP,
    VERDICT_TN,
    VERDICT_TP,
    JudgeAdjudication,
    adjudicate_corpus,
    adjudicate_decision,
    observation_to_judge_context,
    read_adjudication,
    run_ref_for,
    warn_on_adjudicator_collusion,
    write_adjudication,
)
from zicato.reflection.corpus import FIDELITY_PREVIEW, FIDELITY_VERBATIM, ingest_lineage
from zicato.testing.adjudicators import (
    AlwaysConfirm,
    AlwaysRefute,
    MalformedThenValid,
    ScriptedTable,
    SpanQuoting,
)
from zicato.tournament.unit_cache import _unit_loss_path, unit_events_path

EPOCH = "epoch-1"
REFL = "refl-oracle"
PLANTED = "PLANTED-VIOLATION-uncited-claim-42"
CLEAN = "the assistant answered plainly and cited every source it used"


def _run(coro):
    return asyncio.run(coro)


def _write_loss(workspace: Path, gen: str, entry: str, replicate: int, *, drift: bool) -> Path:
    from zicato.core import DriftCount, JudgeLoss, LossProfile
    from zicato.telemetry import reducer

    loss = LossProfile(
        run_id=f"run-{gen}-{entry}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=EPOCH,
        drift_counts=((DriftCount(kind="custom:j", severity="warning", count=1),) if drift else ()),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=1.0 if drift else 0.0,
        pass_fail=not drift,
        per_judge_loss=(
            JudgeLoss(
                "j",
                raw_loss=1.0 if drift else 0.0,
                weight=1.0,
                weighted_loss=1.0 if drift else 0.0,
            ),
        ),
    )
    path = _unit_loss_path(workspace, EPOCH, gen, entry, replicate)
    reducer.write_loss_profile(loss, path)
    return path


def _plant_judge_io(
    loss_path: Path, *, judge_name: str, fired: bool, reasoning: str, severity: str = "warning"
) -> None:
    sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))
    sink.record(
        judge_name,
        reasoning_text=reasoning,
        transcript_window=(reasoning,),
        raw_response="{}",
        drift_emitted=fired,
        kind=f"custom:{judge_name}",
        severity=severity if fired else "info",
        detail="claim" if fired else "",
    )


def _ingest(workspace: Path, candidates, entries):
    return ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id=REFL,
        candidates=candidates,
        entries=entries,
        weights=ScoringWeights(),
    )


def _config(workspace: Path, *, adjudicator, judge=None) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="default",
        workspace_root=workspace,
        target_call_llm=_h1,
        evaluation_call_llm=_h2,
        adjudicator_call_llm=adjudicator,
        judge_call_llm=judge,
    )


async def _h1(system, user, model):  # pragma: no cover - identity placeholder
    return ""


async def _h2(system, user, model):  # pragma: no cover - identity placeholder
    return ""


# ---------------------------------------------------------------------------
# THE ORACLE — planted violation + never-fires judge + correct adjudicator ⇒ FN
# ---------------------------------------------------------------------------


def test_oracle_missed_fire_yields_fn_naming_the_span(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    # The judge NEVER fired, but its captured input carries the planted violation.
    _plant_judge_io(loss_path, judge_name="sleepy", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])
    assert corpus[0].fidelity == FIDELITY_VERBATIM
    assert corpus[0].judge_decisions[0]["fired"] is False

    adjudicator = SpanQuoting(should_fire=True)  # always-correct: the transcript DOES exhibit it
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=adjudicator),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="meta-strong",
            workspace_root=workspace,
        )
    )
    assert len(results) == 1
    verdict = results[0]
    assert verdict.observed == "silent"
    assert verdict.adjudicated == "should_fire"
    assert verdict.verdict == VERDICT_FN
    # The finding-grounding span quotes the planted marker verbatim.
    assert PLANTED[:20] in verdict.evidence_span
    assert verdict.fidelity == FIDELITY_VERBATIM
    assert verdict.prompt_version == ADJUDICATOR_PROMPT_VERSION


def test_oracle_mirror_false_fire_yields_fp(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=True)
    # Trigger-happy judge FIRED on a clean transcript.
    _plant_judge_io(loss_path, judge_name="trigger", fired=True, reasoning=CLEAN)
    corpus = _ingest(workspace, ["v1"], ["entryA"])
    assert corpus[0].judge_decisions[0]["fired"] is True

    # AlwaysRefute is correct here: the clean transcript should NOT have fired.
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=AlwaysRefute()),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="meta-strong",
            workspace_root=workspace,
        )
    )
    assert results[0].observed == "fired"
    assert results[0].adjudicated == "should_be_silent"
    assert results[0].verdict == VERDICT_FP


# ---------------------------------------------------------------------------
# Independence guards
# ---------------------------------------------------------------------------


def test_hard_guard_same_callable_raises(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    shared = SpanQuoting()
    config = _config(workspace, adjudicator=shared, judge=shared)  # SAME object both seams
    with pytest.raises(RuntimeError):
        _run(
            adjudicate_corpus(
                corpus=[],
                config=config,
                epoch_id=EPOCH,
                reflection_id=REFL,
                adjudicator_model="m",
                workspace_root=workspace,
            )
        )


def test_soft_guard_same_model_string_warns_and_proceeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    with caplog.at_level(logging.WARNING):
        results = _run(
            adjudicate_corpus(
                corpus=corpus,
                config=_config(workspace, adjudicator=SpanQuoting()),
                epoch_id=EPOCH,
                reflection_id=REFL,
                adjudicator_model="model-x",
                judge_models=["model-x"],  # collides with the adjudicator model string
                workspace_root=workspace,
            )
        )
    assert any("distinct" in r.message or "independent" in r.message for r in caplog.records)
    # It WARNS but PROCEEDS — the adjudication still ran.
    assert len(results) == 1


def test_warn_helper_returns_true_only_on_collision() -> None:
    assert warn_on_adjudicator_collusion("m", ["m", "other"]) is True
    assert warn_on_adjudicator_collusion("m", ["other"]) is False
    assert warn_on_adjudicator_collusion(None, ["m"]) is False


# ---------------------------------------------------------------------------
# Cache idempotency — second pass = zero adjudicator calls
# ---------------------------------------------------------------------------


def test_cache_idempotency_second_pass_zero_calls(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    for entry in ("entryA", "entryB"):
        loss_path = _write_loss(workspace, "v1", entry, 0, drift=False)
        _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA", "entryB"])

    adjudicator = SpanQuoting()
    kwargs = dict(
        corpus=corpus,
        config=_config(workspace, adjudicator=adjudicator),
        epoch_id=EPOCH,
        reflection_id=REFL,
        adjudicator_model="m",
        workspace_root=workspace,
    )
    first = _run(adjudicate_corpus(**kwargs))
    assert adjudicator.calls == 2  # one per decision

    # Every verdict was persisted to its cache slot.
    for obs in corpus:
        path = reflection_adjudication_path(workspace, EPOCH, REFL, "j", run_ref_for(obs))
        assert path.exists()

    adjudicator.calls = 0
    second = _run(adjudicate_corpus(**kwargs))
    assert adjudicator.calls == 0  # ALL cache HITs
    assert [v.to_json() for v in second] == [v.to_json() for v in first]


# ---------------------------------------------------------------------------
# Malformed protocol — one retry, then ambiguous; never raises
# ---------------------------------------------------------------------------


def test_retry_recovers_on_second_valid_response(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    double = MalformedThenValid(should_fire=True)
    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=double,
            adjudicator_model="m",
        )
    )
    assert double.calls == 2  # garbage, then valid on retry
    assert verdict.verdict == VERDICT_FN  # silent judge, should_fire ⇒ FN
    assert verdict.raw_response is None  # recovered, not ambiguous


def test_two_malformed_responses_yield_ambiguous_and_retain_raw(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    calls = {"n": 0}

    async def always_garbage(system, user, model):
        calls["n"] += 1
        return "definitely not json"

    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=always_garbage,
            adjudicator_model="m",
        )
    )
    assert calls["n"] == 2  # exactly one retry
    assert verdict.verdict == VERDICT_AMBIGUOUS
    assert verdict.adjudicated == "ambiguous"
    assert verdict.raw_response == "definitely not json"  # raw bytes retained


# ---------------------------------------------------------------------------
# Severity tracked apart from detection
# ---------------------------------------------------------------------------


def test_severity_mismatch_tracked_apart_from_detection(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=True)
    _plant_judge_io(loss_path, judge_name="j", fired=True, reasoning=PLANTED, severity="critical")
    corpus = _ingest(workspace, ["v1"], ["entryA"])
    decision = dict(corpus[0].judge_decisions[0])
    assert decision["severity"] == "critical"

    # Confirms firing (TP) but at 'warning' — a severity defect, not a detection one.
    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=decision,
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=AlwaysConfirm(severity="warning"),
            adjudicator_model="m",
        )
    )
    assert verdict.verdict == VERDICT_TP  # detection is correct
    assert verdict.severity_match is False  # severity is wrong, tracked separately


# ---------------------------------------------------------------------------
# Adjudicator self-agreement (replication) + span-quoting verbatim proof
# ---------------------------------------------------------------------------


def test_replication_reports_self_agreement(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=SpanQuoting(should_fire=True),
            adjudicator_model="m",
            k_adj=3,
        )
    )
    # Three consistent should_fire=True replicates ⇒ perfect self-agreement.
    assert verdict.adjudicator_self_agreement == 1.0


def test_span_quoting_proves_verbatim_path(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    double = SpanQuoting(should_fire=True, quote_len=200)
    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=double,
            adjudicator_model="m",
        )
    )
    # The double quoted the transcript it saw; that text is the verbatim
    # judge_io reasoning_text on disk.
    records = json.loads
    io_path = judge_io_path_for_loss(loss_path)
    reasoning = ""
    for line in io_path.read_text(encoding="utf-8").splitlines():
        reasoning = records(line)["input"]["reasoning_text"]
    assert double.last_transcript  # the double saw a non-empty transcript
    assert double.last_transcript in reasoning
    assert verdict.evidence_span in reasoning


# ---------------------------------------------------------------------------
# Fidelity ladder — preview fallback rides through
# ---------------------------------------------------------------------------


def test_preview_fidelity_rides_through(tmp_path: Path) -> None:
    from zicato.reflection.corpus import ObservationRun
    from zicato.testing import make_synthetic_events_jsonl

    workspace = tmp_path / ".zicato"
    # A loss with NO judge_io and NO result.json — only an events.jsonl preview.
    loss_path = _write_loss(workspace, "v1", "entryA", 5000, drift=False)
    (judge_io_path_for_loss(loss_path)).unlink(missing_ok=True)
    events_path = unit_events_path(loss_path)
    make_synthetic_events_jsonl(events_path, drift_events=[("off_topic", "warning")])

    obs = ObservationRun(
        reflection_id=REFL,
        candidate_id="v1",
        entry_id="entryA",
        replicate=5000,
        scalar=0.0,
        drift_loss=0.0,
        pass_fail=True,
        runtime_ms=10,
        aborted=False,
        abort_cause=None,
        fidelity=FIDELITY_PREVIEW,
        has_result=False,
        has_judge_io=False,
        loss_ref=str(loss_path),
        transcript_ref=str(events_path),
        judge_decisions=({"judge_name": "j", "fired": False, "severity": None, "claim": None},),
    )
    _ctx, tier = observation_to_judge_context(obs, "j")
    assert tier == FIDELITY_PREVIEW

    verdict = _run(
        adjudicate_decision(
            obs=obs,
            judge_name="j",
            decision=dict(obs.judge_decisions[0]),
            run_ref=run_ref_for(obs),
            adjudicator_call_llm=SpanQuoting(should_fire=True),
            adjudicator_model="m",
        )
    )
    assert verdict.fidelity == FIDELITY_PREVIEW  # the label rides through


def test_scripted_table_verdicts(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])
    run_ref = run_ref_for(corpus[0])

    table = ScriptedTable({("j", run_ref): True})
    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref,
            adjudicator_call_llm=table,
            adjudicator_model="m",
        )
    )
    assert verdict.verdict == VERDICT_FN  # silent + scripted should_fire=True

    # An unscripted key raises loudly (the ScriptedCallLLM precedent).
    empty = ScriptedTable({})
    with pytest.raises(RuntimeError):
        _run(
            adjudicate_decision(
                obs=corpus[0],
                judge_name="j",
                decision=dict(corpus[0].judge_decisions[0]),
                run_ref=run_ref,
                adjudicator_call_llm=empty,
                adjudicator_model="m",
            )
        )


def test_scripted_table_yields_tp_and_tn_per_decision(tmp_path: Path) -> None:
    """A per-decision scripted table (the way to vary blind verdicts): fired +
    should_fire ⇒ TP; silent + should_be_silent ⇒ TN."""
    workspace = tmp_path / ".zicato"
    loss_fired = _write_loss(workspace, "v1", "entryA", 0, drift=True)
    _plant_judge_io(loss_fired, judge_name="j", fired=True, reasoning=PLANTED)
    loss_silent = _write_loss(workspace, "v1", "entryB", 0, drift=False)
    _plant_judge_io(loss_silent, judge_name="j", fired=False, reasoning=CLEAN)
    corpus = _ingest(workspace, ["v1"], ["entryA", "entryB"])

    # Blind, content-correct verdicts scripted per run_ref (the prompt no longer
    # reveals the judge's action, so confirm/refute cannot vary per decision).
    table = ScriptedTable({("j", "v1:entryA:r0"): True, ("j", "v1:entryB:r0"): False})
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=table),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="m",
            workspace_root=workspace,
        )
    )
    by_ref = {r.run_ref: r.verdict for r in results}
    assert by_ref["v1:entryA:r0"] == VERDICT_TP
    assert by_ref["v1:entryB:r0"] == VERDICT_TN


def test_double_protocol_markers_match_production() -> None:
    """The doubles inline the protocol markers to avoid an import edge — pin them."""
    import zicato.reflection.adjudicator as A
    import zicato.testing.adjudicators as D

    assert D.OBSERVED_FIRED == A.OBSERVED_FIRED
    assert D.OBSERVED_SILENT == A.OBSERVED_SILENT
    assert D.TRANSCRIPT_OPEN == A.TRANSCRIPT_OPEN
    assert D.TRANSCRIPT_CLOSE == A.TRANSCRIPT_CLOSE
    # Extended (review round): the strict-JSON verdict keys + severity vocabulary.
    assert D.VERDICT_JSON_KEYS == A.VERDICT_JSON_KEYS
    assert D.SEVERITY_VOCAB == A.SEVERITY_VOCAB


# ---------------------------------------------------------------------------
# ANCHORING — the de-anchored prompt leaks neither the verdict nor the severity
# ---------------------------------------------------------------------------


def test_prompt_is_deanchored_no_verdict_no_severity(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=True)
    _plant_judge_io(loss_path, judge_name="j", fired=True, reasoning=PLANTED, severity="critical")
    corpus = _ingest(workspace, ["v1"], ["entryA"])
    decision = dict(corpus[0].judge_decisions[0])
    assert decision["severity"] == "critical"  # the judge DID claim critical

    double = SpanQuoting(should_fire=True)
    _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=decision,
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=double,
            adjudicator_model="m",
        )
    )
    prompt = double.prompts[0]
    # The judge's action + its claimed severity are WITHHELD; the criterion stays.
    assert "OBSERVED" not in prompt
    assert "critical" not in prompt
    assert "CRITERION" in prompt


# ---------------------------------------------------------------------------
# RETRY — the single parse-retry names the failure
# ---------------------------------------------------------------------------


def test_retry_appends_corrective_suffix(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    double = MalformedThenValid(should_fire=True)
    _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=double,
            adjudicator_model="m",
        )
    )
    assert double.calls == 2
    assert len(double.prompts) == 2
    # The first attempt is the plain prompt; the retry names the parse failure.
    assert "not valid JSON" not in double.prompts[0]
    assert "not valid JSON" in double.prompts[1]
    assert double.prompts[1].startswith(double.prompts[0])  # suffix appended, not rebuilt


# ---------------------------------------------------------------------------
# CACHE staleness — a stale verdict is re-adjudicated + overwritten
# ---------------------------------------------------------------------------


def _seed_cache(
    workspace: Path,
    *,
    judge: str,
    run_ref: str,
    model: str,
    prompt_version: int,
    k_adj: int,
    fidelity: str,
) -> Path:
    """Write a cache file matching the current request on every dim but the one
    a staleness test perturbs (verdict body is a distinct sentinel)."""
    path = reflection_adjudication_path(workspace, EPOCH, REFL, judge, run_ref)
    write_adjudication(
        path,
        JudgeAdjudication(
            judge_name=judge,
            run_ref=run_ref,
            observed="silent",
            adjudicated="should_fire",
            verdict=VERDICT_FN,
            severity_match=None,
            evidence_span="STALE-CACHED-SPAN",
            meta_judge_rationale="stale",
            meta_judge_model=model,
            adjudicator_self_agreement=None,
            operator_confirmed=None,
            fidelity=fidelity,
            prompt_version=prompt_version,
            k_adj=k_adj,
        ),
    )
    return path


def _one_verbatim_corpus(workspace: Path):
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    return _ingest(workspace, ["v1"], ["entryA"])


def test_cache_hit_when_every_dimension_matches(tmp_path: Path) -> None:
    # Positive control: a fully-matching cache is a HIT (zero adjudicator calls).
    workspace = tmp_path / ".zicato"
    corpus = _one_verbatim_corpus(workspace)
    run_ref = run_ref_for(corpus[0])
    _seed_cache(
        workspace,
        judge="j",
        run_ref=run_ref,
        model="m",
        prompt_version=ADJUDICATOR_PROMPT_VERSION,
        k_adj=1,
        fidelity=FIDELITY_VERBATIM,
    )
    double = SpanQuoting(should_fire=True)
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=double),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="m",
            workspace_root=workspace,
        )
    )
    assert double.calls == 0  # HIT
    assert results[0].evidence_span == "STALE-CACHED-SPAN"  # served from cache


def test_cache_stale_on_model_swap(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    corpus = _one_verbatim_corpus(workspace)
    run_ref = run_ref_for(corpus[0])
    _seed_cache(
        workspace,
        judge="j",
        run_ref=run_ref,
        model="model-A",
        prompt_version=ADJUDICATOR_PROMPT_VERSION,
        k_adj=1,
        fidelity=FIDELITY_VERBATIM,
    )
    double = SpanQuoting(should_fire=True)
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=double),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="model-B",  # swapped
            workspace_root=workspace,
        )
    )
    assert double.calls == 1  # re-adjudicated
    assert results[0].meta_judge_model == "model-B"
    assert results[0].evidence_span != "STALE-CACHED-SPAN"
    # Overwritten on disk.
    on_disk = read_adjudication(reflection_adjudication_path(workspace, EPOCH, REFL, "j", run_ref))
    assert on_disk is not None and on_disk.meta_judge_model == "model-B"


def test_cache_stale_on_prompt_version_bump(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    corpus = _one_verbatim_corpus(workspace)
    run_ref = run_ref_for(corpus[0])
    # A pre-fix (v1) cache — everything else matches the current request.
    _seed_cache(
        workspace,
        judge="j",
        run_ref=run_ref,
        model="m",
        prompt_version=ADJUDICATOR_PROMPT_VERSION - 1,
        k_adj=1,
        fidelity=FIDELITY_VERBATIM,
    )
    double = SpanQuoting(should_fire=True)
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=double),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="m",
            workspace_root=workspace,
        )
    )
    assert double.calls == 1  # re-adjudicated at the current prompt version
    assert results[0].prompt_version == ADJUDICATOR_PROMPT_VERSION


def test_cache_stale_on_k_adj_change_surfaces_self_agreement(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    corpus = _one_verbatim_corpus(workspace)
    run_ref = run_ref_for(corpus[0])
    # Cached at k_adj=1 (single shot ⇒ no self-agreement).
    _seed_cache(
        workspace,
        judge="j",
        run_ref=run_ref,
        model="m",
        prompt_version=ADJUDICATOR_PROMPT_VERSION,
        k_adj=1,
        fidelity=FIDELITY_VERBATIM,
    )
    double = SpanQuoting(should_fire=True)
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=double),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="m",
            k_adj=3,  # replication bumped
            workspace_root=workspace,
        )
    )
    assert double.calls == 3  # re-adjudicated with three replicates
    assert results[0].k_adj == 3
    assert results[0].adjudicator_self_agreement == 1.0  # now measured


def test_cache_stale_on_fidelity_upgrade(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    corpus = _one_verbatim_corpus(workspace)  # a verbatim judge_io sidecar exists
    run_ref = run_ref_for(corpus[0])
    # Cached at the weaker PREVIEW tier; the verbatim sidecar is now on disk.
    _seed_cache(
        workspace,
        judge="j",
        run_ref=run_ref,
        model="m",
        prompt_version=ADJUDICATOR_PROMPT_VERSION,
        k_adj=1,
        fidelity=FIDELITY_PREVIEW,
    )
    double = SpanQuoting(should_fire=True)
    results = _run(
        adjudicate_corpus(
            corpus=corpus,
            config=_config(workspace, adjudicator=double),
            epoch_id=EPOCH,
            reflection_id=REFL,
            adjudicator_model="m",
            workspace_root=workspace,
        )
    )
    assert double.calls == 1  # re-adjudicated at the higher fidelity
    assert results[0].fidelity == FIDELITY_VERBATIM


def test_read_adjudication_tolerates_defects(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert read_adjudication(missing) is None
    garbage = tmp_path / "g.json"
    garbage.write_text("not json", encoding="utf-8")
    assert read_adjudication(garbage) is None
    wrong_version = tmp_path / "wv.json"
    wrong_version.write_text(json.dumps({"format_version": 999}), encoding="utf-8")
    assert read_adjudication(wrong_version) is None


# ---------------------------------------------------------------------------
# A hung adjudicator is bounded — it degrades like a malformed one, never wedges
# ---------------------------------------------------------------------------


def test_a_hung_adjudicator_times_out_into_an_ambiguous_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-answering meta-judge yields ambiguous, not a wedged `reflect run`.

    Both attempts are bounded by the shared evaluation budget, and the retry is
    still EXACTLY ONE — a first attempt that hangs gets the same single second
    chance a first attempt that returns garbage gets. Nothing propagates: this
    path's contract is that it never raises, and a TimeoutError escaping it
    would take the whole corpus adjudication down on one unlucky decision.
    """
    monkeypatch.setattr("zicato.reflection.adjudicator.aux_call_timeout_s", lambda: 0.01)

    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    calls = {"n": 0}

    async def never_answers(system, user, model):
        calls["n"] += 1
        await asyncio.sleep(30)
        return '{"should_fire": true}'  # pragma: no cover - never reached

    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=never_answers,
            adjudicator_model="m",
        )
    )

    assert calls["n"] == 2  # bounded, retried exactly once, then given up on
    assert verdict.verdict == VERDICT_AMBIGUOUS
    assert verdict.adjudicated == "ambiguous"
    # The raw response names the timeout, so the operator reading the ambiguous
    # verdict can tell "did not answer in time" from "answered with prose".
    assert "timed out" in (verdict.raw_response or "")


def test_a_slow_but_answering_adjudicator_still_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is a ceiling, not a floor — a call inside the budget is normal."""
    monkeypatch.setattr("zicato.reflection.adjudicator.aux_call_timeout_s", lambda: 5.0)

    workspace = tmp_path / ".zicato"
    loss_path = _write_loss(workspace, "v1", "entryA", 0, drift=False)
    _plant_judge_io(loss_path, judge_name="j", fired=False, reasoning=PLANTED)
    corpus = _ingest(workspace, ["v1"], ["entryA"])

    calls = {"n": 0}

    async def slow_but_valid(system, user, model):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return '{"should_fire": true, "evidence_span": "' + PLANTED + '"}'

    verdict = _run(
        adjudicate_decision(
            obs=corpus[0],
            judge_name="j",
            decision=dict(corpus[0].judge_decisions[0]),
            run_ref=run_ref_for(corpus[0]),
            adjudicator_call_llm=slow_but_valid,
            adjudicator_model="m",
        )
    )

    assert calls["n"] == 1  # no retry — the first attempt parsed
    assert verdict.verdict == VERDICT_FN
    assert verdict.raw_response is None
