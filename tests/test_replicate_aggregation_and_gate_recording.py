"""Triage pins for the replication / gate-recording cluster (#108, #109, #111).

One root theme: the loop runs and reports, but a quantity a consumer
reasonably believes is being measured is not the quantity recorded.

* **#108** — :func:`zicato.tournament.unit_cache._average_losses` folds
  ``drift_loss`` and majority-votes ``pass_fail``, then takes every other
  field from replicate 0. ``score`` is one of those, and ``score`` is what
  :func:`zicato.tournament.scoring.entry_score` reads FIRST — so the
  continuous outcome axis of a K-replicate duel is replicate 0 verbatim.
* **#109** — :func:`zicato.tournament.runner.run_fast_mode` (the gauntlet
  under the default ``--mode fast``) does not take a ``replicates``
  parameter at all, so the contract's knob is silently inert on that path.
* **#111** — the gauntlet gate records the compared scalars only inside the
  human-readable REJECT text; a promoted duel carries no numbers, so any
  downstream effect-size analysis is missing exactly its promotions.

Every failing pin is ``xfail(strict=True)``: it must XPASS once fixed, at
which point the marker is removed.
"""

from __future__ import annotations

import pytest

from zicato.core import DriftCount, ExpectationResult, LossProfile, ScoringWeights


def _loss(
    *,
    entry_id: str = "e",
    drift_loss: float = 1.0,
    score: float | None = None,
    pass_fail: bool | None = None,
    tokens_spent: int = 0,
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
        tokens_spent=tokens_spent,
    )


# ---------------------------------------------------------------------------
# Issue #108 — the outcome axis must actually average
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #108: _average_losses takes `score` from replicate 0, so K "
        "replicates buy zero variance reduction on the axis that decides the duel"
    ),
)
def test_average_losses_averages_score() -> None:
    """A 4-replicate list scoring [1, 0, 0, 0] averages to 0.25, not 1.0."""
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(score=s, pass_fail=bool(s))} for s in (1.0, 0.0, 0.0, 0.0)]
    out = _average_losses(runs)
    assert out["e"].score == pytest.approx(0.25)


@pytest.mark.xfail(
    strict=True,
    reason="issue #108: the averaged profile's entry_score is replicate 0's",
)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #108: metric_counts / tokens_spent are namespace-scalar-bearing "
        "via aggregate_namespaced_metrics and are also replicate-0-only"
    ),
)
def test_average_losses_averages_namespace_bearing_counters() -> None:
    """``tokens_spent`` feeds the ``cost:`` namespace term of the scalar."""
    from zicato.tournament.unit_cache import _average_losses

    runs = [{"e": _loss(tokens_spent=t, score=0.5)} for t in (400, 0, 0, 0)]
    out = _average_losses(runs)
    assert out["e"].tokens_spent == pytest.approx(100)


# ---------------------------------------------------------------------------
# Issue #109 — fast mode must not silently drop `replicates`
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #109: run_fast_mode takes no `replicates` parameter, so the "
        "gauntlet under the default --mode fast runs the challenger board once"
    ),
)
def test_run_fast_mode_accepts_replicates() -> None:
    """``run_fast_mode`` must expose the knob its contract advertises.

    A signature pin rather than a behavioural one: today the parameter does
    not exist, so no caller can even ask for replication on this path.
    """
    import inspect

    from zicato.tournament.runner import run_fast_mode

    assert "replicates" in inspect.signature(run_fast_mode).parameters


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #109: evolve_once's fast branch never passes strategy.replicates() "
        "to run_fast_mode, so the contract's knob cannot reach the gauntlet path"
    ),
)
def test_evolve_once_threads_replicates_into_the_fast_branch() -> None:
    """The fast branch must forward the resolved replicate count.

    Source-level pin on the ONE call site (``zicato/orchestrator.py``); a
    live-run assertion is out of scope for triage, and the defect is the
    missing argument, not its downstream behaviour.
    """
    import inspect

    from zicato import orchestrator

    src = inspect.getsource(orchestrator.evolve_once)
    fast_call = src.split("run_fast_mode(", 1)[1].split(")", 1)[0]
    assert "replicates" in fast_call


# ---------------------------------------------------------------------------
# Issue #111 — the compared scalars must be recorded on BOTH decisions
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #111: GateEvaluated carries only (rule_fired, decision), so a "
        "promoted duel records no scalars and downstream effect-size analysis "
        "is missing exactly its promotions"
    ),
)
def test_gate_evaluated_carries_the_compared_scalars() -> None:
    """The round-log event must carry the numbers structurally, not in prose."""
    from zicato.epoch.round_log import GateEvaluated

    fields = set(GateEvaluated.__dataclass_fields__)
    assert {"champion_scalar", "challenger_scalar", "margin_required"} <= fields


@pytest.mark.xfail(
    strict=True,
    reason="issue #111: GateOutcome.reason is the empty string on the accept path",
)
def test_promoted_gate_outcome_states_its_reason() -> None:
    """A promotion should say why it cleared, exactly as a reject says why not."""
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.3, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision.value == "promoted"
    assert outcome.reason, "the accept path must record why it cleared"


def test_rejected_gate_outcome_still_names_its_rule() -> None:
    """The reject text the #111 fix must not weaken."""
    from zicato.tournament.gate import evaluate_gate

    weights = ScoringWeights(promote_margin=0.1)
    parent = {"scalar": 0.6, "pass_rate": 1.0, "per_entry": {}}
    child = {"scalar": 0.58, "pass_rate": 1.0, "per_entry": {}}
    outcome = evaluate_gate(parent, child, weights)
    assert outcome.decision.value == "rejected"
    assert "insufficient improvement" in outcome.reason
