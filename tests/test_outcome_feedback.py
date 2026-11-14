"""``experimental.outcome_feedback``: banded per-candidate results in the memory.

With the knob on, settlement records each candidate's per-metric movements,
training-task pass changes and the standard error of its paired scalar
difference, and the experiment memory renders them as directions, magnitude
buckets and counts. Everything comes from the training slice and describes
the whole candidate: no board-entry id, no metric value and no holdout result
reaches the prompt. With the knob off nothing is recorded or rendered.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_index_prior_experiments import _insert_experiment, _new_index
from zicato.core.experiment import OUTCOME_OPTIONAL_FIELDS, outcome_to_dict
from zicato.core.scoring_config import ExperimentalConfig, ScoringWeights
from zicato.core.types import ExpectedMetricMovement, PriorExperiment
from zicato.epoch.contract import scoring_to_canon
from zicato.epoch.journal import outcome_from_dict
from zicato.evolve.settlement import _train_feedback
from zicato.index.query import prior_experiments_for_epoch
from zicato.proposer.prompts import (
    OUTCOME_FEEDBACK_MAX_CHARS,
    _bucket_scalar_delta,
    render_prior_experiments_block,
)
from zicato.testing.fixtures import (
    make_hypothesis_spec,
    make_loss_profile,
    make_metric_count,
    make_outcome_record,
)
from zicato.tournament.scoring import (
    aggregate_generation_score,
    metric_means,
    paired_scalar_se,
    pass_flips,
)

_ON = ScoringWeights(experimental=ExperimentalConfig(outcome_feedback=True))


# -- the knob -----------------------------------------------------------------


def test_knob_defaults_off_and_enabling_it_changes_contract_identity() -> None:
    default = scoring_to_canon(ScoringWeights())
    assert default["experimental"]["outcome_feedback"] is False
    assert scoring_to_canon(_ON)["experimental"]["outcome_feedback"] is True
    assert json.dumps(scoring_to_canon(_ON), sort_keys=True) != json.dumps(default, sort_keys=True)


# -- Part C: the noise-aware band ---------------------------------------------


@pytest.mark.parametrize(
    "delta, se, band",
    [
        (-0.05, 0.01, "improved (clear)"),
        (-0.05, 0.04, "improved (within noise)"),
        (0.05, 0.01, "regressed (clear)"),
        (0.05, 0.04, "regressed (within noise)"),
        (-0.004, 0.001, "improved (clear)"),  # a small effect clear of noise
        (-0.004, 0.01, "flat"),
        (0.0, 0.0, "flat"),
        (-0.02, 0.01, "improved (clear)"),  # exactly two standard errors
    ],
)
def test_band_is_qualified_by_the_standard_error(delta: float, se: float, band: str) -> None:
    assert _bucket_scalar_delta(delta, se) == band


@pytest.mark.parametrize("delta", [-0.5, -0.011, -0.01, 0.0, 0.01, 0.011, 0.5])
def test_band_without_a_standard_error_is_the_three_way_band(delta: float) -> None:
    expected = "improved" if delta < -0.01 else "regressed" if delta > 0.01 else "flat"
    assert _bucket_scalar_delta(delta) == expected


def _pairs(parent_pass: list[bool], child_pass: list[bool], child_drift: list[float]):
    return [
        (
            make_loss_profile(entry_id=f"t{i}", generation_id="v1", pass_fail=before),
            make_loss_profile(entry_id=f"t{i}", generation_id="v2", pass_fail=after, drift_loss=d),
        )
        for i, (before, after, d) in enumerate(
            zip(parent_pass, child_pass, child_drift, strict=True)
        )
    ]


def _delta(pairs, weights: ScoringWeights = _ON) -> float:
    child = aggregate_generation_score([c for _p, c in pairs], weights)["scalar"]
    parent = aggregate_generation_score([p for p, _c in pairs], weights)["scalar"]
    return child - parent


def test_standard_error_matches_an_independent_calculation() -> None:
    pairs = _pairs([False, True, True, False], [True, True, False, True], [0.0, 0.2, 0.1, 0.4])
    weights = ScoringWeights()
    differences = [
        aggregate_generation_score([c], weights)["scalar"]
        - aggregate_generation_score([p], weights)["scalar"]
        for p, c in pairs
    ]
    assert len(set(differences)) > 1
    expected = statistics.stdev(differences) / math.sqrt(len(differences))
    assert paired_scalar_se(pairs, weights, _delta(pairs)) == pytest.approx(expected)


def test_standard_error_is_withheld_when_the_scalar_does_not_decompose() -> None:
    pairs = _pairs([False, True, True], [True, True, False], [0.0, 0.2, 0.1])
    weights = ScoringWeights()
    assert paired_scalar_se(pairs, weights, _delta(pairs) + 0.05) is None
    assert paired_scalar_se(pairs[:1], weights, _delta(pairs[:1])) is None
    # A pass transform makes the scalar a non-linear function of the mean.
    squared = ScoringWeights(pass_transform={"op": "pow", "exponent": 2.0})
    skewed = _pairs([False, False, False], [True, True, False], [0.0, 0.2, 0.1])
    assert paired_scalar_se(skewed, squared, _delta(skewed, squared)) is None
    assert paired_scalar_se(skewed, weights, _delta(skewed)) is not None


# -- Part B: fixed and broken counts -------------------------------------------


def test_pass_flips_counts_only_the_entries_it_is_given() -> None:
    parent = {"a": False, "b": True, "c": True, "d": None, "h": False}
    child = {"a": True, "b": False, "c": True, "d": True, "h": True}
    fixed, broke = pass_flips(parent, child, {"a", "b", "c", "d"})
    assert (fixed, broke) == (frozenset({"a"}), frozenset({"b"}))


def test_metric_means_counts_an_absent_metric_as_zero() -> None:
    losses = [
        make_loss_profile(
            entry_id="a", metric_counts=(make_metric_count("drift:off_topic", count=2),)
        ),
        make_loss_profile(entry_id="b"),
    ]
    assert metric_means(losses)["drift:off_topic"] == 1.0


# -- settlement ---------------------------------------------------------------


def _field(weights: ScoringWeights, per_entry_losses, train_ids=("t0", "t1", "t2", "t3")):
    field_round = SimpleNamespace(
        parent_id="v1",
        prepared=SimpleNamespace(
            weights=weights, train_board=[SimpleNamespace(id=i) for i in train_ids]
        ),
    )
    result = SimpleNamespace(
        parent_generation_id="v1", child_generation_id="v2", per_entry_losses=per_entry_losses
    )
    return field_round, SimpleNamespace(raw_results={"m1": result})


def _duel() -> dict:
    pairs = _pairs([False, True, True, False], [True, True, False, True], [0.0, 0.2, 0.1, 0.4])
    pairs[0] = (
        replace(pairs[0][0], metric_counts=(make_metric_count("drift:off_topic", count=4),)),
        pairs[0][1],
    )
    return {parent.entry_id: (parent, child) for parent, child in pairs}


_HYPOTHESIS = make_hypothesis_spec(
    expected_metric_movements=(ExpectedMetricMovement("drift:off_topic", "decrease", "medium"),)
)


def test_settlement_records_nothing_with_the_knob_off() -> None:
    duel = _duel()
    field_round, execution = _field(ScoringWeights(), duel)
    assert _train_feedback(field_round, execution, "v2", _HYPOTHESIS, 0.0) == {}


def test_settlement_records_train_feedback_with_the_knob_on() -> None:
    duel = _duel()
    field_round, execution = _field(_ON, duel)
    delta = _delta(list(duel.values()), _ON)

    feedback = _train_feedback(field_round, execution, "v2", _HYPOTHESIS, delta)

    assert (feedback["train_fixed"], feedback["train_broke"]) == (2, 1)
    assert feedback["scalar_score_delta_se"] == pytest.approx(
        paired_scalar_se(list(duel.values()), _ON, delta)
    )
    movement = {m.metric_name: m for m in feedback["metric_movements"]}["drift:off_topic"]
    assert (movement.from_value, movement.to_value, movement.hypothesis_match) == (1.0, 0.0, True)


def test_settlement_never_counts_an_entry_outside_the_training_slice() -> None:
    duel = _duel()
    all_train, execution = _field(_ON, duel)
    # ``t0`` is one of the two fixed entries; treat it as a holdout entry.
    without_t0, _ = _field(_ON, duel, train_ids=("t1", "t2", "t3"))

    full = _train_feedback(all_train, execution, "v2", _HYPOTHESIS, 0.0)
    train_only = _train_feedback(without_t0, execution, "v2", _HYPOTHESIS, 0.0)

    assert (full["train_fixed"], train_only["train_fixed"]) == (2, 1)
    names = {m.metric_name for m in train_only["metric_movements"]}
    assert "drift:off_topic" not in names  # only ``t0`` carried that metric


def test_settlement_orients_a_duel_that_lists_the_champion_second() -> None:
    duel = {entry: (child, parent) for entry, (parent, child) in _duel().items()}
    field_round, execution = _field(_ON, duel)
    execution.raw_results["m1"].parent_generation_id = "v2"
    execution.raw_results["m1"].child_generation_id = "v1"
    feedback = _train_feedback(field_round, execution, "v2", _HYPOTHESIS, 0.0)
    assert (feedback["train_fixed"], feedback["train_broke"]) == (2, 1)


def test_a_challenger_that_never_met_the_champion_records_nothing() -> None:
    field_round, execution = _field(_ON, _duel())
    assert _train_feedback(field_round, execution, "v9", _HYPOTHESIS, 0.0) == {}


# -- the stored record --------------------------------------------------------


def test_an_outcome_without_feedback_omits_the_optional_keys() -> None:
    body = outcome_to_dict(make_outcome_record())
    assert not set(OUTCOME_OPTIONAL_FIELDS) & set(body)
    assert outcome_from_dict(json.loads(json.dumps(body))) == make_outcome_record()


def test_an_outcome_with_feedback_round_trips() -> None:
    outcome = make_outcome_record(scalar_score_delta_se=0.02, train_fixed=9, train_broke=7)
    body = outcome_to_dict(outcome)
    assert (body["scalar_score_delta_se"], body["train_fixed"], body["train_broke"]) == (0.02, 9, 7)
    assert outcome_from_dict(json.loads(json.dumps(body))) == outcome


# -- the reader ---------------------------------------------------------------

_ENTRY_IDS = ("refund_task_17", "holdout_escalation_4")


def _settled_row(db: Path) -> None:
    hypothesis = {
        "core_idea": "Forbid uncited claims.",
        "modulating": ["researcher.instruction"],
        "expected_metric_movements": [
            {"metric_name": "drift:off_topic", "direction": "decrease", "magnitude": "medium"},
            {"metric_name": "judge:tone", "direction": "increase_or_neutral", "magnitude": "small"},
            {"metric_name": "cost:tokens", "direction": "decrease", "magnitude": "small"},
        ],
    }
    outcome = {
        "metric_movements": [
            {"metric_name": "drift:off_topic", "from_value": 0.7391, "to_value": 0.4391},
            {"metric_name": "judge:tone", "from_value": 0.2137, "to_value": 0.2137},
            {"metric_name": "failure:tasks", "from_value": 0.1123, "to_value": 0.9123},
            {"metric_name": "judge:brevity", "from_value": 0.5123, "to_value": 0.2623},
            {"metric_name": "runtime:seconds", "from_value": 1.0, "to_value": 1.0437},
        ],
        "scalar_score_delta_se": 0.0123,
        "train_fixed": 9,
        "train_broke": 7,
        "holdout": {"regressed_entries": list(_ENTRY_IDS), "champion_scalar": 0.4127},
    }
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v2",
        decision="rejected",
        scalar_delta=-0.034,
        rejection_reason=f"holdout_not_confirmed: holdout pass-rate regression on entries: "
        f"{', '.join(_ENTRY_IDS)}",
        hypothesis_json=json.dumps(hypothesis),
        core_idea="Forbid uncited claims.",
        outcome_json=json.dumps(outcome),
    )


def test_reader_carries_no_feedback_with_the_knob_off(tmp_path: Path) -> None:
    db = _new_index(tmp_path)
    _settled_row(db)
    (entry,) = prior_experiments_for_epoch(db, "e1")
    assert entry.scalar_delta_se is None and entry.train_fixed is None
    assert entry.predicted_movements == () and entry.realised_movements == ()


def test_reader_bands_the_feedback_with_the_knob_on(tmp_path: Path) -> None:
    db = _new_index(tmp_path)
    _settled_row(db)
    (entry,) = prior_experiments_for_epoch(db, "e1", outcome_feedback=True)

    assert (entry.scalar_delta_se, entry.train_fixed, entry.train_broke) == (0.0123, 9, 7)
    assert [m.metric_name for m in entry.predicted_movements] == [
        "drift:off_topic",
        "judge:tone",
        "cost:tokens",
    ]
    assert entry.realised_movements == (
        ExpectedMetricMovement("drift:off_topic", "decrease", "medium"),
        ExpectedMetricMovement("judge:tone", "neutral", "small"),
        # Unpredicted, largest first; the small runtime movement is left out.
        ExpectedMetricMovement("failure:tasks", "increase", "large"),
        ExpectedMetricMovement("judge:brevity", "decrease", "medium"),
    )


def test_reader_lists_at_most_three_unpredicted_metrics(tmp_path: Path) -> None:
    db = _new_index(tmp_path)
    outcome = {
        "metric_movements": [
            {"metric_name": f"judge:j{i}", "from_value": 0.0, "to_value": 0.3} for i in range(6)
        ]
    }
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v2",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=json.dumps({"modulating": [], "expected_metric_movements": []}),
        outcome_json=json.dumps(outcome),
    )
    (entry,) = prior_experiments_for_epoch(db, "e1", outcome_feedback=True)
    assert len(entry.realised_movements) == 3


# -- the rendering ------------------------------------------------------------


def _rendered(tmp_path: Path, *, restrict: bool) -> str:
    db = _new_index(tmp_path)
    _settled_row(db)
    return render_prior_experiments_block(
        prior_experiments_for_epoch(db, "e1", outcome_feedback=True), restrict=restrict
    )


def test_restricted_entry_renders_bands_and_counts_only(tmp_path: Path) -> None:
    assert _rendered(tmp_path, restrict=True) == (
        "Already rejected (do NOT re-propose these unless something changed):\n"
        "- v2 REJECTED Δscalar=improved (clear)  [researcher.instruction]  "
        "(holdout_not_confirmed)  prediction:high\n"
        "    Forbid uncited claims.\n"
        "    predictions: drift:off_topic predicted decrease medium, actual decrease medium "
        "(direction ok, size ok); judge:tone predicted increase_or_neutral small, actual "
        "neutral small (direction ok, size ok); cost:tokens predicted decrease small, "
        "unmeasured\n"
        "    unpredicted: failure:tasks increase large; judge:brevity decrease medium\n"
        "    train tasks: fixed 9, broke 7"
    )


def test_unrestricted_entry_renders_the_number_with_its_standard_error(tmp_path: Path) -> None:
    head = _rendered(tmp_path, restrict=False).splitlines()[1]
    assert head.startswith("- v2 REJECTED Δscalar=-0.034 ±0.012  [researcher.instruction]")


def test_restricted_entry_leaks_no_entry_id_and_no_metric_value(tmp_path: Path) -> None:
    block = _rendered(tmp_path, restrict=True)
    assert not any(entry in block for entry in _ENTRY_IDS)
    # The only digits are the generation id and the two whole-candidate counts.
    assert re.findall(r"\d+(?:\.\d+)?", block) == ["2", "9", "7"]


def test_feedback_text_is_capped_per_entry() -> None:
    entry = PriorExperiment(
        generation_id="v2",
        epoch_id="e1",
        core_idea="idea",
        modulating=(),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=-0.1,
        predicted_movements=tuple(
            ExpectedMetricMovement(f"judge:a_long_judge_name_{i}", "decrease", "medium")
            for i in range(40)
        ),
    )
    block = render_prior_experiments_block([entry])
    feedback = block.split("    idea", 1)[1]
    assert len(feedback) == OUTCOME_FEEDBACK_MAX_CHARS and feedback.endswith("…")


def test_an_entry_without_feedback_renders_as_before() -> None:
    entry = PriorExperiment(
        generation_id="v6",
        epoch_id="e1",
        core_idea="idea",
        modulating=("m",),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=-0.09,
    )
    assert render_prior_experiments_block([entry], restrict=True).endswith(
        "- v6 PROMOTED Δscalar=improved  [m]\n    idea"
    )
    assert render_prior_experiments_block([entry]).endswith(
        "- v6 PROMOTED Δscalar=-0.090  [m]\n    idea"
    )


# -- the real loop on the deterministic example ---------------------------------

_LOOP = """
import asyncio, sys
from pathlib import Path
from zicato.orchestrator import evolve_n_rounds
from zicato.evolve import lifecycle_services
lifecycle_services._resolve_or_launch_harmonograf = (
    lambda *args, **kwargs: ("", lifecycle_services._NoopShutdownHandle())
)
asyncio.run(evolve_n_rounds(
    rounds=3, workspace_root=Path(sys.argv[1]), max_consecutive_rejections=3
))
"""


@pytest.mark.integration
def test_the_example_loop_records_and_renders_feedback_with_the_knob_on(tmp_path: Path) -> None:
    """Three rounds of the model-free example, each fixing one of four tasks."""
    import subprocess
    import sys

    from zicato.cli.init_cmd import initialize_workspace
    from zicato.epoch.journal import read_experiment

    workspace = tmp_path / ".zicato"
    initialize_workspace(workspace, instance_id="example-test", example=True)
    scoring = tmp_path / "scoring.json"
    scoring.write_text(
        scoring.read_text().replace('"outcome_feedback": false', '"outcome_feedback": true')
    )
    result = subprocess.run(
        [sys.executable, "-c", _LOOP, str(workspace)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    (epoch,) = (path.name for path in (workspace / "epochs").iterdir())
    for generation in ("v1", "v2", "v3"):
        outcome = read_experiment(workspace, epoch, generation).outcome
        assert outcome is not None and outcome.tournament_decision == "promoted"
        assert (outcome.train_fixed, outcome.train_broke) == (1, 0)
        # Four paired differences of -1, 0, 0, 0: sd 0.5, over sqrt(4).
        assert outcome.scalar_score_delta_se == pytest.approx(0.25)
        assert outcome.metric_movements
    prior = prior_experiments_for_epoch(workspace / "index.db", epoch, outcome_feedback=True)
    block = render_prior_experiments_block(prior, restrict=True)
    assert block.count("Δscalar=improved (within noise)") == 3
    assert block.count("train tasks: fixed 1, broke 0") == 3
