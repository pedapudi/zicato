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


def test_report_is_immutable_and_repeated_assertions_do_not_execute_trials(monkeypatch, tmp_path):
    from tests import test_decision_procedure_power as power

    original = power._naive_outcome
    calls = []

    def record(workspace, seed, weights):
        calls.append(seed)
        return original(workspace, seed, weights)

    monkeypatch.setattr(power, "_naive_outcome", record)
    report = _power_report(
        monkeypatch, tmp_path, DELTA_CASES["small"][0], effective=False, seeds=(0, 1)
    )
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

    _power_report(monkeypatch, tmp_path, DELTA_CASES["small"][0], effective=False, seeds=(1, 2))
    assert calls == [0, 1, 1, 2]
    monkeypatch.setattr(power, "NAIVE_WEIGHTS", replace(power.NAIVE_WEIGHTS, promote_margin=100.0))
    changed = _power_report(
        monkeypatch, tmp_path, DELTA_CASES["small"][0], effective=False, seeds=(0, 1)
    )
    assert calls == [0, 1, 1, 2, 0, 1]
    assert changed.inputs_json != report.inputs_json
    assert changed.promotion_rate == 0.0
