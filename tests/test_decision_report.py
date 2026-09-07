"""Seed completeness and report reuse for statistical assertions."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from tests._decision_report import DecisionReport, DecisionTrial
from tests.test_decision_procedure_power import DELTA_CASES, _power_report


@pytest.mark.parametrize(
    "seeds, trials", [((), ()), ((0, 0), (0, 0)), ((0, 1), (0,)), ((0, 1), (1, 0))]
)
def test_report_rejects_incomplete_or_ambiguous_seed_sets(seeds, trials):
    rows = tuple(DecisionTrial(seed, (), "promoted", "", "{}", ()) for seed in trials)
    with pytest.raises(ValueError, match="seed"):
        DecisionReport("{}", "revision", seeds, rows)


def test_report_retains_every_trial_in_the_promotion_denominator():
    rows = tuple(
        DecisionTrial(seed, (), outcome, outcome, "{}", ())
        for seed, outcome in enumerate(("promoted", "rejected", "deferred", "inconclusive"))
    )
    report = DecisionReport("{}", "revision", (0, 1, 2, 3), rows)
    assert report.promotion_rate == 0.25


def test_report_retains_actual_confirmation_admission_and_spend():
    report = _power_report(DELTA_CASES["large"][0], effective=True, seeds=(0,))
    trial = report.trials[0]
    evidence = json.loads(trial.evidence_json)
    attempts = evidence["verdict"]["attempts"]
    assert attempts[0]["eligibility"] == "selection_only"
    assert trial.rating_eligibility == tuple(
        attempt["eligibility"] == "eligible" for attempt in attempts
    )
    assert not trial.rating_eligibility[0]
    assert (
        sum(attempt["budget_spent"] for attempt in attempts)
        == evidence["verdict"]["replicates_spent"]
    )
    assert evidence["verdict"]["n_duels"] == sum(trial.rating_eligibility)


@pytest.mark.parametrize("scenario", ["improvement", "tie", "missing"])
def test_direct_decisions_match_scheduler_at_measurement_boundaries(
    monkeypatch, tmp_path, scenario
):
    from dataclasses import asdict

    from tests import test_decision_procedure_power as power

    world = power._NoisyWorld(
        {
            "champion": power.BASE_TOKENS,
            "challenger": power.BASE_TOKENS if scenario == "tie" else (),
        },
        0.0 if scenario == "tie" else power.NOISE_SIGMA,
    )
    if scenario == "missing":
        profile = world.profile

        def omit(observation, **kwargs):
            loss = profile(observation, **kwargs)
            return (
                replace(loss, execution_started=False)
                if observation.generation_id == "challenger" and observation.entry_id == "conv_body"
                else loss
            )

        monkeypatch.setattr(world, "profile", omit)
    world.install(monkeypatch)
    scheduled = power._effective_evaluation(tmp_path, 0)
    computed = power._effective_evaluation(tmp_path, 0, world=world)
    assert asdict(computed) == asdict(scheduled)
    if scenario == "improvement":
        assert computed.decision.decision == "promoted"
        assert computed.evidence.verdict.confirmation_status == "satisfied"
    else:
        assert computed.decision.promoted_generation_id is None
        assert computed.evidence is None
        if scenario == "missing":
            assert "incomplete" in computed.decision.reason


def test_report_is_immutable_and_repeated_assertions_do_not_execute_trials(monkeypatch):
    from tests import test_decision_procedure_power as power

    original = power._computed_matchup
    calls = []

    def record(world, matchup, seed, weights, **kwargs):
        calls.append(seed)
        return original(world, matchup, seed, weights, **kwargs)

    monkeypatch.setattr(power, "_computed_matchup", record)
    report = _power_report(DELTA_CASES["small"][0], effective=False, seeds=(0, 1))
    first_rate = report.promotion_rate
    assert len(report.trials) == 2
    assert all(len(trial.observations) == 10 for trial in report.trials)
    assert all(trial.comparisons_spent == 1 for trial in report.trials)
    assert report.promotion_rate == first_rate
    assert calls == [0, 1]

    with pytest.raises(FrozenInstanceError):
        report.trials[0].observations[0].drift_loss = -1
    with pytest.raises(FrozenInstanceError):
        report.trials[0].decision = "promoted"
    decoded = json.loads(report.trials[0].audit_json)
    decoded["decision"] = "corrupted"
    assert json.loads(report.trials[0].audit_json)["decision"] != "corrupted"

    _power_report(DELTA_CASES["small"][0], effective=False, seeds=(1, 2))
    assert calls == [0, 1, 1, 2]
    monkeypatch.setattr(power, "NAIVE_WEIGHTS", replace(power.NAIVE_WEIGHTS, promote_margin=100.0))
    changed = _power_report(DELTA_CASES["small"][0], effective=False, seeds=(0, 1))
    assert calls == [0, 1, 1, 2, 0, 1]
    assert changed.inputs_json != report.inputs_json
    assert changed.promotion_rate == 0.0
