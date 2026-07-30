"""Pins for the replication / gate-recording cluster (#108, #109, #111).

All three shared one root theme: the loop ran and reported, but a quantity a
consumer reasonably believed was being measured was not the quantity
recorded. Each is now FIXED, and these are the pins that keep it fixed — the
original strict-xfail reproductions plus the properties the fixes establish.

* **#108** — :func:`zicato.tournament.unit_cache._average_losses` folded
  ``drift_loss`` and majority-voted ``pass_fail``, then took every other
  field from replicate 0. ``score`` was one of those, and ``score`` is what
  :func:`zicato.tournament.scoring.entry_score` reads FIRST, so the
  continuous outcome axis of a K-replicate duel was replicate 0 verbatim.
  The fold now aggregates every scalar-bearing field.
* **#109** — :func:`zicato.tournament.runner.run_fast_mode` (the gauntlet
  under the default ``--mode fast``) took no ``replicates`` parameter at
  all, so the contract's knob was inert on that path. It is honoured now,
  on the challenger side, and the residual one-sided asymmetry is logged
  rather than implied.
* **#111** — the gate recorded the compared scalars only inside the
  human-readable REJECT text, so a promoted duel carried no numbers and any
  downstream effect-size analysis was missing exactly its promotions. The
  scalars are recorded structurally on ``gate_evaluated`` for both
  decisions. NOTE: the original pin asked for ``GateOutcome.reason`` to be
  populated on promote; that was reshaped to the structural form because the
  empty-reason-on-promote invariant is load-bearing across five persisted
  fields — see ``test_promoted_duel_is_reconstructable_from_the_round_log``
  and ``test_promoted_gate_outcome_records_no_reason``.
"""

from __future__ import annotations

from typing import Any

import pytest

from zicato.core import (
    DriftCount,
    ExpectationResult,
    JudgeLoss,
    LossProfile,
    MetricCount,
    ScoringWeights,
)


def _loss(
    *,
    entry_id: str = "e",
    drift_loss: float = 1.0,
    score: float | None = None,
    pass_fail: bool | None = None,
    tokens_spent: int = 0,
    metrics: dict[str, float] | None = None,
    metric_counts: tuple[MetricCount, ...] = (),
    per_judge_loss: tuple[JudgeLoss, ...] = (),
) -> LossProfile:
    """A LossProfile carrying an explicit continuous ``score``.

    The existing ``_average_losses`` tests build profiles WITHOUT a score,
    which is exactly why they pass today.
    """
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail), score=score)
        if (pass_fail is not None or score is not None)
        else None
    )
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        score=score,
        metrics=metrics,
        tokens_spent=tokens_spent,
        metric_counts=metric_counts,
        per_judge_loss=per_judge_loss,
    )


# ---------------------------------------------------------------------------
# Issue #108 — the outcome axis must actually average
# ---------------------------------------------------------------------------


def test_average_losses_averages_score() -> None:
    """A 4-replicate list scoring [1, 0, 0, 0] averages to 0.25, not 1.0."""
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=s, pass_fail=bool(s))} for s in (1.0, 0.0, 0.0, 0.0)]
    out = _average_losses(runs)
    assert out["e"].score == pytest.approx(0.25)


def test_averaged_entry_score_reflects_every_replicate() -> None:
    """``entry_score`` over the averaged profile is the mean, not replicate 0.

    ``entry_score`` is the single uniform mapping the scalar and the gate
    read, so this — not the raw field — is the axis the duel turns on.
    """
    from zicato.tournament.scoring import entry_score
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=s, pass_fail=bool(s))} for s in (1.0, 0.0, 0.0, 0.0)]
    out = _average_losses(runs)
    assert entry_score(out["e"]) == pytest.approx(0.25)


def test_average_losses_all_none_score_stays_none() -> None:
    """A board with no expectations must be unchanged by the #108 fix."""
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=None, pass_fail=None)} for _ in range(3)]
    out = _average_losses(runs)
    assert out["e"].score is None


def test_average_losses_averages_namespace_bearing_counters() -> None:
    """``tokens_spent`` feeds the ``cost:`` namespace term of the scalar."""
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(tokens_spent=t, score=0.5)} for t in (400, 0, 0, 0)]
    out = _average_losses(runs)
    assert out["e"].tokens_spent == pytest.approx(100)


def test_average_losses_score_means_only_the_replicates_that_have_one() -> None:
    """An UNMEASURED replicate must not be read as zero on the outcome axis.

    "Unmeasured" here means no expectation was recorded at all (no score AND
    no pass/fail): nothing was observed, so the replicate abstains from the
    mean rather than dragging it toward a miss. Contrast
    ``test_average_losses_counts_an_aborted_replicate_as_a_miss``: a replicate
    that recorded a FAILURE without a score is not unmeasured and does vote.
    """
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=s)} for s in (1.0, None, 0.5, None)]
    out = _average_losses(runs)
    assert out["e"].score == pytest.approx(0.75)


def test_average_losses_counts_an_aborted_replicate_as_a_miss() -> None:
    """An ABORTED replicate votes ``0.0``; it does not abstain.

    ``score`` is unset in two materially different situations and only one is
    an abstention. :func:`_aborted_loss_profile` — a spent wall-clock/token
    budget, an infra kill — records ``score=None`` with ``pass_fail=False``:
    the run observed a failure, not nothing.

    Folding the raw ``score`` field treats that as an abstention, which is
    how a K-replicate duel silently reverts to the single-replicate behaviour
    #108 removed. One clean pass + one abort reported the clean replicate's
    ``1.0`` VERBATIM on the outcome axis while ``pass_fail``'s majority said
    ``False`` — a folded profile contradicting itself, whose ``mean_score``
    was a perfect ``1.0`` off a duel half of which never ran. Folding
    ``entry_score`` (the mapping every consumer already reads) instead of the
    raw field is what makes the abort vote.

    Reachable under the default configuration: the gauntlet defaults to
    ``replicates=2``, and any budget that clips mid-round synthesises exactly
    this profile for the un-run slot.
    """
    from zicato.core import BoardEntry, Expectation, ExpectationKind
    from zicato.core import ScoringWeights as _Weights
    from zicato.tournament.scoring import aggregate_generation_score, entry_score
    from zicato.tournament.unit_cache import _average_losses
    from zicato.tournament.worker_transport import _aborted_loss_profile

    entry = BoardEntry(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hi",
        expectation=Expectation(kind=ExpectationKind.PREDICATE, spec="contains:x"),
    )
    clean = _loss(entry_id="e", drift_loss=0.0, pass_fail=True, score=1.0)
    aborted = _aborted_loss_profile(
        run_id="run-abort",
        entry=entry,
        generation_id="v1",
        epoch_id="e0",
        weights=_Weights(),
        runtime_ms=0,
        match_id="",
        abort_cause="budget_exhausted",
    )
    # The premise: the abort DID record an expectation failure, with no score.
    assert aborted.score is None
    assert aborted.pass_fail is False
    assert entry_score(aborted) == pytest.approx(0.0)

    folded = _average_losses([{"e": clean}, {"e": aborted}])["e"]
    assert folded.score == pytest.approx(0.5)
    assert entry_score(folded) == pytest.approx(0.5)
    # ...and the continuous axis the scalar runs on agrees with the vote's sign.
    agg = aggregate_generation_score([folded], _Weights(drift_weight=0.0, pass_weight=1.0))
    assert agg["mean_score"] == pytest.approx(0.5)


def test_average_losses_folds_the_outcome_on_a_score_less_bool_board() -> None:
    """K replicates move the axis on an all-bool board too, not just a scored one.

    A profile carrying only ``pass_fail`` resolves through ``entry_score``'s
    bool path, so folding the resolved outcome means 1-of-4 passes reads
    ``0.25`` — the same arithmetic a float-scorer board gets. Folding the raw
    ``score`` field instead left this case at ``None`` and let ``pass_fail``'s
    single majority bit decide, which is the replicate-0-verbatim failure mode
    one field over.
    """
    from zicato.tournament.scoring import entry_score
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(pass_fail=p, score=None)} for p in (True, False, False, False)]
    out = _average_losses(runs)
    assert entry_score(out["e"]) == pytest.approx(0.25)
    # The binary view is still the strict-majority vote.
    assert out["e"].pass_fail is False


def test_average_losses_no_expectation_board_still_folds_to_none() -> None:
    """The back-compat floor the outcome fold must not disturb.

    An entry with no expectation at all produces neither a score nor a
    pass/fail on any replicate, so it must fold to ``None`` and stay EXCLUDED
    from ``mean_score`` — exactly as before replication existed.
    """
    from zicato.tournament.scoring import entry_score
    from zicato.tournament.unit_cache import _average_losses

    out = _average_losses([{"e": _loss(score=None, pass_fail=None)} for _ in range(3)])
    assert out["e"].score is None
    assert entry_score(out["e"]) is None


def test_average_losses_folds_the_metrics_decomposition() -> None:
    """The folded ``metrics`` must decompose the folded ``score`` beside it.

    Per-key mean over the replicates REPORTING the key: ``recall`` is on
    two of the three replicates, so its mean is over those two.
    """
    from zicato.tournament.unit_cache import _average_losses

    runs = [
        {"e": _loss(score=1.0, metrics={"precision": 1.0, "recall": 0.5})},
        {"e": _loss(score=0.0, metrics={"precision": 0.4})},
        {"e": _loss(score=0.5, metrics={"precision": 0.1, "recall": 0.1})},
    ]
    out = _average_losses(runs)
    assert out["e"].metrics == {
        "precision": pytest.approx(0.5),
        "recall": pytest.approx(0.3),
    }


def test_average_losses_no_metrics_stays_none() -> None:
    """A board whose scorers expose no decomposition folds unchanged."""
    from zicato.tournament.unit_cache import _average_losses

    out = _average_losses([{"e": _loss(score=0.5)} for _ in range(3)])
    assert out["e"].metrics is None


def test_folded_namespace_aggregate_matches_the_per_replicate_aggregate() -> None:
    """The load-bearing invariant behind the counter fold.

    In production the reducer ALWAYS populates ``metric_counts``, and
    :meth:`LossProfile.unified_metrics` then reads it in preference to
    synthesising from the int scalars — so ``metric_counts`` is what the
    ``cost:`` namespace term of the scalar actually runs on. Folding it
    must be aggregate-preserving: the namespace aggregate over the ONE
    folded profile has to equal the aggregate over the K replicate runs
    it folded, or replication silently moves the scalar.
    """
    from zicato.core import MetricCount
    from zicato.tournament.scoring import aggregate_namespaced_metrics
    from zicato.tournament.unit_cache import _average_losses

    weights = ScoringWeights(namespace_weights={"cost:": 1.0})
    profiles = [
        _loss(score=0.5, metric_counts=(MetricCount(name="cost:tokens_spent", count=c),))
        for c in (300.0, 0.0, 0.0)
    ]
    folded = _average_losses([{"e": p} for p in profiles])
    assert aggregate_namespaced_metrics([folded["e"]], weights)["cost:"] == pytest.approx(
        aggregate_namespaced_metrics(profiles, weights)["cost:"]
    )
    # And concretely: 300 tokens on one of three replicates is a mean of 100.
    assert aggregate_namespaced_metrics([folded["e"]], weights)["cost:"] == pytest.approx(100.0)


def test_average_losses_folds_per_judge_loss() -> None:
    """``per_judge_loss`` rides :class:`ScalarContext`, so a plugin can read it.

    Meaned over ALL replicates with an absent judge contributing zero —
    the same divisor :func:`_per_judge_loss_aggregate` applies, so the
    aggregate is identical taken over the fold or over the replicates.
    """
    from zicato.core import JudgeLoss
    from zicato.tournament.scoring import _per_judge_loss_aggregate
    from zicato.tournament.unit_cache import _average_losses

    def _jl(raw: float) -> tuple[JudgeLoss, ...]:
        return (JudgeLoss(judge_name="tone", raw_loss=raw, weight=2.0, weighted_loss=raw * 2.0),)

    profiles = [_loss(score=0.5, per_judge_loss=_jl(r)) for r in (3.0, 0.0)]
    folded = _average_losses([{"e": p} for p in profiles])
    assert folded["e"].per_judge_loss == (
        JudgeLoss(judge_name="tone", raw_loss=1.5, weight=2.0, weighted_loss=3.0),
    )
    assert _per_judge_loss_aggregate([folded["e"]]) == _per_judge_loss_aggregate(profiles)


def test_average_losses_leaves_the_raw_matcher_evidence_alone() -> None:
    """``expectation_result`` is replicate 0's raw verdict, by design.

    The fold is not a run and has no matcher verdict of its own; the
    AGGREGATED outcome lives in the first-class ``score`` / ``pass_fail``
    fields that scoring and the gate read. Pinned so a later change that
    starts synthesising a fake ``ExpectationResult`` is a deliberate one.
    """
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=s, pass_fail=bool(s))} for s in (1.0, 0.0)]
    out = _average_losses(runs)
    assert out["e"].expectation_result == runs[0]["e"].expectation_result
    assert out["e"].score == pytest.approx(0.5)


def test_replicated_mean_score_is_the_replicate_mean_end_to_end() -> None:
    """The whole point: K replicates move the axis the duel turns on.

    ``mean_score`` is what the scalar's outcome term
    (``pass_weight * (1 - mean_score)``) runs on. A board entry that
    passes 1 of 4 replicates must score 0.25 there, not replicate 0's 1.0.
    """
    from zicato.tournament.scoring import aggregate_generation_score
    from zicato.tournament.unit_cache import _average_losses

    weights = ScoringWeights(drift_weight=0.0, pass_weight=1.0)
    runs = [{"e": _loss(drift_loss=0.0, score=s, pass_fail=bool(s))} for s in (1.0, 0.0, 0.0, 0.0)]
    agg = aggregate_generation_score(list(_average_losses(runs).values()), weights)
    assert agg["mean_score"] == pytest.approx(0.25)
    assert agg["scalar_components"]["pass"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Issue #109 — fast mode must not silently drop `replicates`
# ---------------------------------------------------------------------------


def test_run_fast_mode_accepts_replicates() -> None:
    """``run_fast_mode`` must expose the knob its contract advertises.

    A signature pin; the behavioural half lives in
    ``tests/test_tournament_runner.py::test_run_fast_mode_honours_replicates``
    (both slots run, and the aggregate is their fold).
    """
    import inspect

    from zicato.tournament.runner import run_fast_mode

    assert "replicates" in inspect.signature(run_fast_mode).parameters


def test_evolve_once_threads_replicates_into_the_fast_branch() -> None:
    """The fast branch must forward the resolved replicate count.

    Source-level pin on the ONE call site (``zicato/orchestrator.py``); a
    live-run assertion is out of scope, and the defect was the missing
    argument, not its downstream behaviour.
    """
    import inspect

    from zicato import orchestrator

    src = inspect.getsource(orchestrator.evolve_once)
    fast_call = src.split("run_fast_mode(", 1)[1].split(")", 1)[0]
    assert "replicates" in fast_call


def test_fast_branch_is_loud_about_the_one_sided_noise_reduction() -> None:
    """Honouring the knob is not the same as making the contrast symmetric.

    Fast mode compares a replicated challenger against ONE frozen cached
    champion aggregate, so the variance reduction is one-sided no matter
    how high ``replicates`` goes — the operator must be told, not left to
    infer a symmetric improvement from the contract.
    """
    import inspect

    from zicato import orchestrator

    src = inspect.getsource(orchestrator.evolve_once)
    fast_branch = src.split("run_fast_mode(", 1)[0]
    assert "log.warning" in fast_branch
    assert "--mode full" in fast_branch


# ---------------------------------------------------------------------------
# Issue #111 — the compared scalars must be recorded on BOTH decisions
# ---------------------------------------------------------------------------


def test_gate_evaluated_carries_the_compared_scalars() -> None:
    """The round-log event must carry the numbers structurally, not in prose."""
    from zicato.epoch.round_log import GateEvaluated

    fields = set(GateEvaluated.__dataclass_fields__)
    assert {"champion_scalar", "challenger_scalar", "margin_required"} <= fields


def test_gate_evaluated_scalars_default_to_none_not_zero() -> None:
    """A log written before the fields existed must not read as "both zero".

    ``0.0`` is a legal scalar, so a numeric default would make an unrecorded
    measurement indistinguishable from a measured zero — the exact confusion
    #111 is about, one layer down. Also the pre-existing-log tolerance pin:
    a payload carrying only the two original keys still decodes.
    """
    from zicato.epoch.round_log import GateEvaluated, _decode_event

    legacy = _decode_event("gate_evaluated", {"rule_fired": "", "decision": "promoted"})
    assert isinstance(legacy, GateEvaluated)
    assert legacy.decision == "promoted"
    assert legacy.champion_scalar is None
    assert legacy.challenger_scalar is None
    assert legacy.margin_required is None


def test_promoted_duel_is_reconstructable_from_the_round_log() -> None:
    """The #111 pin, in its typed-fields form.

    The original triage pin asserted that ``GateOutcome.reason`` become
    non-empty on the accept path. That was reshaped to this: the pin's INTENT
    is that a promoted duel's decision be reconstructable from the log, and
    the empty-reason-on-promote invariant turned out to be load-bearing —
    five persisted fields (``experiment.json``'s outcome, two index columns,
    the round-log provenance, ``field_tournament.json``) treat a non-empty
    reason as "this was rejected", and two of the write sites are unguarded,
    so the text would also reach the analyzer's LLM report payload
    (``analyzer/report_prompts.py``) and ``epoch/analysis.py``'s ladder line.
    So the numbers move somewhere structural instead, and ``rule_fired`` keeps
    its "empty on a clean promote" contract.

    What has to hold: from the event alone, on a PROMOTION, a consumer can
    recover the effect size and check the decision.
    """
    from zicato.epoch.round_log import GateEvaluated
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision.value == "promoted"

    event = _emitted_gate_event(outcome, parent, child, weights)
    assert isinstance(event, GateEvaluated)
    # rule_fired stays empty on a clean promote — unchanged contract.
    assert event.rule_fired == ""
    # ...and yet the duel is fully reconstructable: effect size and verdict.
    assert event.challenger_scalar is not None and event.champion_scalar is not None
    assert event.challenger_scalar - event.champion_scalar == pytest.approx(-0.3)
    assert event.margin_required == pytest.approx(0.1)
    assert event.challenger_scalar <= event.champion_scalar - event.margin_required


def test_rejected_duel_records_the_same_scalars_as_a_promoted_one() -> None:
    """Both decisions, identically — the point of #111.

    The bias #111 describes comes from the sample being truncated in a way
    correlated with the outcome, so the two paths must record the SAME field
    set. A rejection additionally keeps its rule text.
    """
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.619670, "pass_rate": 1.0, "per_entry": {}}
    promoted_child = {"scalar": 0.30, "pass_rate": 1.0, "per_entry": {}}
    rejected_child = {"scalar": 0.581291, "pass_rate": 1.0, "per_entry": {}}

    events = [
        _emitted_gate_event(evaluate_gate(parent, c, weights), parent, c, weights)
        for c in (promoted_child, rejected_child)
    ]
    assert [e.decision for e in events] == ["promoted", "rejected"]
    for event, child in zip(events, (promoted_child, rejected_child), strict=True):
        assert event.champion_scalar == pytest.approx(0.619670)
        assert event.challenger_scalar == pytest.approx(child["scalar"])
        assert event.margin_required == pytest.approx(0.1)
    # The reject still names its rule; the promote still names none.
    assert events[0].rule_fired == ""
    assert "insufficient improvement" in events[1].rule_fired


def test_gate_event_scalars_survive_the_round_record_fold() -> None:
    """``fold_round_record`` must carry the new fields, not just the old two."""
    from dataclasses import asdict

    from zicato.epoch.round_log import RoundLogEnvelope, fold_round_record
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    event = _emitted_gate_event(evaluate_gate(parent, child, weights), parent, child, weights)

    folded = fold_round_record(
        [
            RoundLogEnvelope(
                seq=1,
                ts="2026-07-29T00:00:00Z",
                type="gate_evaluated",
                payload=asdict(event),
                event=event,
            )
        ]
    )
    assert folded.gates == (event,)
    assert folded.gates[0].champion_scalar == pytest.approx(0.6)


def test_emitter_omits_scalars_it_was_not_given() -> None:
    """Absent inputs record ABSENT fields, never fabricated zeros.

    Emission is best-effort everywhere in the round log; a caller that has no
    aggregates, or a hand-built one carrying no ``scalar``, must still produce
    a well-formed event rather than a plausible-looking ``0.0``.
    """
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)

    bare = _emitted_gate_event(outcome)
    assert bare.decision == "promoted"
    assert bare.champion_scalar is None
    assert bare.challenger_scalar is None
    assert bare.margin_required is None

    scalarless = _emitted_gate_event(outcome, {"pass_rate": 1.0}, {"scalar": "nope"}, weights)
    assert scalarless.champion_scalar is None
    assert scalarless.challenger_scalar is None
    # ...while the margin, which WAS resolvable, is still recorded.
    assert scalarless.margin_required == pytest.approx(0.1)


def test_emitter_treats_a_boolean_scalar_as_non_numeric() -> None:
    """``bool`` is an ``int`` subclass — a boolean scalar must not fabricate
    a numeric field.

    ``isinstance(True, int | float)`` is ``True``, so a naive numeric guard
    would record ``champion_scalar=1.0`` for a malformed aggregate carrying
    ``{"scalar": True}`` — indistinguishable from a genuinely measured
    ``1.0``. A boolean must be treated exactly like the ``"nope"`` string
    case above: non-numeric, field left absent. Covers both extraction
    sites in ``_emit_gate_evaluated`` — the per-side scalar AND
    ``margin_required``.
    """
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)

    class _BoolMargin:
        promote_margin = True

    boolean = _emitted_gate_event(outcome, {"scalar": True}, {"scalar": False}, _BoolMargin())
    assert boolean.champion_scalar is None
    assert boolean.challenger_scalar is None
    assert boolean.margin_required is None


def _emitted_gate_event(
    outcome: object,
    parent_agg: object = None,
    child_agg: object = None,
    weights: object = None,
) -> Any:
    """Run ``_emit_gate_evaluated`` against a capturing emitter; return the event.

    Goes through the real emitter rather than constructing the event by hand,
    so these pins cover the field WIRING (which aggregate feeds which field)
    and not just the dataclass shape.
    """
    from zicato.epoch.round_log import EVENT_TYPES
    from zicato.orchestrator import _emit_gate_evaluated

    captured: list[Any] = []

    class _Emitter:
        def emit(self, type_token: str, fields: dict[str, Any] | None = None) -> None:
            captured.append(EVENT_TYPES[type_token](**(fields or {})))

    _emit_gate_evaluated(
        _Emitter(),  # type: ignore[arg-type]
        outcome,
        parent_agg=parent_agg,
        child_agg=child_agg,
        weights=weights,
    )
    assert len(captured) == 1
    return captured[0]


def test_rejected_gate_outcome_still_names_its_rule() -> None:
    """The reject text the #111 fix must not weaken."""
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.58, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision.value == "rejected"
    assert "insufficient improvement" in outcome.reason


def test_promoted_gate_outcome_records_no_reason() -> None:
    """The load-bearing invariant #111 was NOT allowed to break.

    ``GateOutcome.reason`` is empty on the accept path, and five persisted
    fields depend on that: a non-empty value in ``experiment.json``'s
    ``rejection_reason``, the two index columns, the round-log decision
    provenance, or ``field_tournament.json`` means "this was rejected". Two of
    those write sites derive the value from the settled decision WITHOUT
    checking that it promoted (``evolve_once``'s ``override_reason`` and
    ``_evolve_multi_challenger``'s per-challenger reason), so flipping this
    would corrupt the meaning of every rejection marker and leak promote text
    into the analyzer's LLM report payload.

    Pinned here rather than merely relied upon, because populating the accept
    path is a natural-looking change (the issue itself suggests it) whose blast
    radius is entirely non-local.
    """
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    h_parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    h_child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}

    assert evaluate_gate(parent, child, weights).reason == ""
    # ...including the holdout-confirmed promotion.
    confirmed = evaluate_gate(
        parent, child, weights, holdout_parent_agg=h_parent, holdout_child_agg=h_child
    )
    assert confirmed.decision.value == "promoted"
    assert confirmed.reason == ""
