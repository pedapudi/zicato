"""What the channel composition must NOT move, and what it must refuse.

The scalar has exactly two kinds of term: one bounded correctness term over
board expectations, and signed coefficients over measured metric channels
(``zicato.scoring.builtins.builtin_scalar``). Moving drift, judges, task
failures and the not-completed charge onto that one channel map was meant to
leave the numbers where they were at shipped defaults, so the four scenarios
below pin the arithmetic rather than the plumbing:

* a run with no drift, no judges and no abort scores exactly ``pass``;
* a custom-judge-only run scores exactly what the judges' weighted losses sum
  to — the quantity the drift term used to carry for them;
* an aborted run still costs 60.0, and now it costs that with the drift
  channel turned off, which is the behaviour change the whole change exists
  for;
* the sum is order-independent.

The refusals are the other half: a contract cannot make crashing free, and it
cannot pretend to retune judges through a knob that no longer reaches them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zicato.core import DriftCount, JudgeLoss, LossProfile, ScoringWeights  # noqa: E402
from zicato.tournament.scoring import aggregate_generation_score  # noqa: E402
from zicato.tournament.unit_cache import _average_losses  # noqa: E402


def _profile(entry_id: str, **overrides: object) -> LossProfile:
    kwargs: dict[str, object] = {
        "run_id": f"run-{entry_id}",
        "entry_id": entry_id,
        "generation_id": "v0",
        "epoch_id": "e0",
        "drift_counts": (),
        "plan_revisions": 0,
        "task_failure_ratio": 0.0,
        "runtime_ms": 0,
        "wall_clock_budget_exceeded": False,
        "expectation_result": None,
        "drift_loss": 0.0,
        "pass_fail": None,
    }
    kwargs.update(overrides)
    return LossProfile(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identity pins at shipped defaults
# ---------------------------------------------------------------------------


def test_a_run_with_no_signal_scores_exactly_the_pass_term() -> None:
    """No drift, no judges, no failures: every channel contributes zero.

    The channels are emitted at zero rather than omitted, so this also pins
    that an always-present channel adds nothing when it has nothing to say.
    """
    weights = ScoringWeights()
    agg = aggregate_generation_score(
        [_profile("a", pass_fail=True), _profile("b", pass_fail=False)], weights
    )
    assert agg["scalar"] == pytest.approx(weights.pass_weight * 0.5)
    assert agg["scalar_components"]["drift"] == 0.0
    assert agg["scalar_components"]["judge"] == 0.0
    assert agg["scalar_components"]["failure"] == 0.0
    assert agg["scalar_components"]["runtime"] == 0.0


def test_a_custom_judge_only_run_scores_what_the_judges_weigh() -> None:
    """The judge channel carries exactly the per-judge split, no more.

    Custom judges used to reach the scalar through the ``custom`` drift kind,
    and the per-judge split was documented to sum to precisely that
    custom-attributed portion. The judge channel must therefore total the same
    number the drift term used to contribute for them — while the drift
    channel, which now excludes judge-attributed counts, contributes nothing.
    """
    weights = ScoringWeights(per_judge_weights={"precision": 3.0}, default_judge_weight=1.5)
    # warning=3.0 severity: precision 3.0*2*3.0 = 18.0; unlisted 1.0*1*1.5 = 1.5.
    profile = _profile(
        "a",
        pass_fail=True,
        drift_counts=(
            DriftCount(kind="custom:precision", severity="warning", count=2),
            DriftCount(kind="custom:unlisted", severity="info", count=1),
        ),
        per_judge_loss=(
            JudgeLoss(judge_name="precision", raw_loss=6.0, weight=3.0, weighted_loss=18.0),
            JudgeLoss(judge_name="unlisted", raw_loss=1.0, weight=1.5, weighted_loss=1.5),
        ),
    )
    agg = aggregate_generation_score([profile], weights)
    assert agg["scalar_components"]["judge"] == pytest.approx(19.5)
    assert agg["scalar_components"]["drift"] == 0.0
    assert agg["scalar"] == pytest.approx(19.5)


def test_an_aborted_run_still_costs_sixty() -> None:
    """The abort charge is unchanged: 10.0 for the tasks, 50.0 for the crash."""
    weights = ScoringWeights()
    aborted = _profile("a", pass_fail=False, task_failure_ratio=1.0, not_completed=True)
    agg = aggregate_generation_score([aborted], weights)
    assert agg["scalar_components"]["failure"] == pytest.approx(60.0)
    assert agg["per_entry"]["a"]["failure"] == pytest.approx(60.0)
    # 60.0 for the abort + 1.0 for the failed expectation.
    assert agg["scalar"] == pytest.approx(61.0)


def test_an_aborted_run_still_costs_sixty_with_the_drift_channel_off() -> None:
    """THE behaviour change: a drift-disabled contract no longer pays nothing.

    A contract that zeroes ``drift:`` — the shipping configuration for any
    adapter that emits no drift stream — used to silence the crash charge
    along with the drift term, so a challenger could win by failing fast. The
    charge now lives in its own channel and survives.
    """
    weights = ScoringWeights(namespace_weights={"drift:": 0.0, "failure:": 1.0})
    aborted = _profile("a", pass_fail=False, task_failure_ratio=1.0, not_completed=True)
    agg = aggregate_generation_score([aborted], weights)
    assert agg["scalar_components"]["drift"] == 0.0
    assert agg["scalar_components"]["failure"] == pytest.approx(60.0)
    assert agg["scalar"] == pytest.approx(61.0)


def test_the_channel_sum_does_not_depend_on_mapping_order() -> None:
    """Sorted iteration: the same profiles score identically however they arrive.

    ``set``/``dict`` iteration order is hash-seed dependent for the namespace
    key set, so an unsorted sum would let the scalar's last bit vary between
    processes — undetectable in one run and fatal to a golden.
    """
    weights = ScoringWeights(
        namespace_weights={
            "schema:": 5.0,
            "drift:": 1.0,
            "rubric:": -1.0,
            "failure:": 1.0,
            "judge:": 1.0,
            "cost:": 0.001,
        }
    )
    reordered = ScoringWeights(
        namespace_weights={
            "cost:": 0.001,
            "judge:": 1.0,
            "failure:": 1.0,
            "rubric:": -1.0,
            "drift:": 1.0,
            "schema:": 5.0,
        }
    )
    profile = _profile(
        "a",
        pass_fail=True,
        drift_loss=1.7,
        task_failure_ratio=0.3,
        tokens_spent=1234,
        schema_failures=2,
        per_judge_loss=(JudgeLoss(judge_name="j", raw_loss=1.0, weight=1.0, weighted_loss=0.9),),
    )
    a = aggregate_generation_score([profile], weights)["scalar"]
    b = aggregate_generation_score([profile], reordered)["scalar"]
    assert float(a).hex() == float(b).hex()


# ---------------------------------------------------------------------------
# Load-time refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_weight", [0.0, -1.0])
def test_a_contract_may_not_make_crashing_free(failure_weight: float) -> None:
    """A non-positive ``failure:`` coefficient is rejected at contract load."""
    with pytest.raises(ValueError, match="failure:"):
        ScoringWeights(namespace_weights={"drift:": 1.0, "failure:": failure_weight})


def test_omitting_the_failure_channel_is_rejected_like_zeroing_it() -> None:
    """An explicit namespace map REPLACES the defaults, so omission is 0.0.

    Silently scoring aborts at zero because a key was left out is the same
    defect as setting it to zero, and is rejected the same way.
    """
    with pytest.raises(ValueError, match="failure:"):
        ScoringWeights(namespace_weights={"drift:": 1.0})


def test_per_kind_weights_may_not_claim_the_custom_kind() -> None:
    """``per_kind_weights["custom"]`` is inert, so it is refused, not ignored.

    Custom-judge drift resolves through ``per_judge_weights`` in the judge
    channel; an operator who set it here would believe they had retuned their
    judges and would be wrong.
    """
    with pytest.raises(ValueError, match="per_kind_weights"):
        ScoringWeights(per_kind_weights={"custom": 2.0})


# ---------------------------------------------------------------------------
# not_completed: persistence and the replicate fold
# ---------------------------------------------------------------------------


def test_not_completed_round_trips_through_loss_json(tmp_path: Path) -> None:
    """The flag survives the write/read cycle, and defaults False when absent.

    ``not_completed_reason`` is legitimately ``None`` for an abort whose
    adapter supplied no reason, so the flag has to be its own field: reading
    the absence of a reason as "completed" would hand a crashed run the best
    possible score.
    """
    from zicato.telemetry.reducer import read_loss_profile, write_loss_profile

    aborted = _profile("a", task_failure_ratio=1.0, not_completed=True, not_completed_reason=None)
    path = tmp_path / "loss.json"
    write_loss_profile(aborted, path)
    assert read_loss_profile(path).not_completed is True

    import json

    payload = json.loads(path.read_text())
    del payload["not_completed"]
    path.write_text(json.dumps(payload))
    assert read_loss_profile(path).not_completed is False


def test_one_failed_replicate_makes_the_folded_unit_not_completed() -> None:
    """The fold is an OR: replication may not dilute a crash away.

    ``task_failure_ratio`` means across replicates, because "how much of this
    run failed" is a quantity. ``not_completed`` does not: a unit that could
    not be completed even once did not complete. A mean is not representable
    on a bool and a majority would make a crash in half the replicates free,
    which is exactly the property the failure channel exists to deny.
    """
    clean = _profile("a")
    crashed = _profile("a", task_failure_ratio=1.0, not_completed=True, runtime_ms=400)
    folded = _average_losses([{"a": clean}, {"a": crashed}, {"a": clean}, {"a": clean}])["a"]
    assert folded.not_completed is True
    assert folded.task_failure_ratio == pytest.approx(0.25)
    assert folded.runtime_ms == 100

    all_clean = _average_losses([{"a": clean}, {"a": clean}])["a"]
    assert all_clean.not_completed is False
