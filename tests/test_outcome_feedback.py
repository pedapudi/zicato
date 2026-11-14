"""``experimental.outcome_feedback``: banded per-candidate results in the memory.

With the knob on, settlement records each candidate's per-metric movements,
training-task pass changes and the standard error of its paired scalar
difference, and the experiment memory renders them as directions, magnitude
buckets and counts. Everything comes from the training slice and describes
the whole candidate: no board-entry id, no task text, no metric value and no
holdout result reaches the prompt. With the knob off nothing is recorded or
rendered.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._proposal_evidence import render_proposal_evidence
from tests.test_index_prior_experiments import _insert_experiment, _new_index
from zicato.core.experiment import OUTCOME_OPTIONAL_FIELDS, SINGLE_ENTRY_NOTE, outcome_to_dict
from zicato.core.scoring_config import ExperimentalConfig, ScoringWeights
from zicato.core.types import ExpectedMetricMovement, PriorExperiment
from zicato.epoch.contract import scoring_to_canon
from zicato.epoch.journal import outcome_from_dict
from zicato.evolve.settlement import _train_feedback
from zicato.index.query import _metric_ranges_for_epoch, prior_experiments_for_epoch
from zicato.proposer.prompts import (
    OUTCOME_FEEDBACK_MAX_CHARS,
    _band_count,
    _bucket_scalar_delta,
    render_prior_experiments_block,
)
from zicato.testing.fixtures import (
    make_hypothesis_spec,
    make_loss_profile,
    make_metric_count,
    make_mutation_point,
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


# -- the noise-aware band -----------------------------------------------------

# Two-sided 0.05 critical values of Student's t, from a printed table.
_T_5, _T_9 = 2.571, 2.262


@pytest.mark.parametrize(
    "delta, se, n, band",
    [
        (-0.05, 0.01, 10, "improved (clear)"),
        (-0.05, 0.04, 10, "improved (within noise)"),
        (0.05, 0.01, 10, "regressed (clear)"),
        (0.05, 0.04, 10, "regressed (within noise)"),
        (-0.004, 0.001, 10, "improved (clear)"),  # a small effect clear of noise
        (-0.004, 0.01, 10, "flat"),
        # The threshold is the t critical value, which grows as entries shrink.
        (-0.05, 0.05 / (_T_9 + 0.01), 10, "improved (clear)"),
        (-0.05, 0.05 / (_T_9 - 0.01), 10, "improved (within noise)"),
        (-0.05, 0.05 / (_T_5 + 0.01), 6, "improved (clear)"),
        (-0.05, 0.05 / (_T_5 - 0.01), 6, "improved (within noise)"),
        # Too few entries, or no spread: the plain three-way band.
        (-0.05, 0.001, 5, "improved"),
        (-0.05, 0.001, 2, "improved"),
        (-0.05, 0.0, 10, "improved"),
        (-0.004, 0.0, 10, "flat"),
        (-0.05, 0.001, None, "improved"),
    ],
)
def test_band_is_qualified_by_a_paired_t_test(
    delta: float, se: float, n: int | None, band: str
) -> None:
    assert _bucket_scalar_delta(delta, se, n) == band


@pytest.mark.parametrize("delta", [-0.5, -0.011, -0.01, 0.0, 0.01, 0.011, 0.5])
def test_band_without_a_standard_error_is_the_three_way_band(delta: float) -> None:
    expected = "improved" if delta < -0.01 else "regressed" if delta > 0.01 else "flat"
    assert _bucket_scalar_delta(delta) == expected


@pytest.mark.parametrize(
    "count, band", [(0, "0"), (1, "1"), (2, "2-3"), (3, "2-3"), (4, "4+"), (9, "4+")]
)
def test_counts_are_banded(count: int, band: str) -> None:
    assert _band_count(count) == band


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


# -- fixed and broken counts ----------------------------------------------------


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


def _field(weights, per_entry_losses, tmp_path, train_ids=("t0", "t1", "t2", "t3")):
    field_round = SimpleNamespace(
        parent_id="v1",
        prepared=SimpleNamespace(
            weights=weights,
            train_board=[SimpleNamespace(id=i) for i in train_ids],
            workspace_root=tmp_path,
            epoch_id="e1",
        ),
    )
    result = SimpleNamespace(
        parent_generation_id="v1", child_generation_id="v2", per_entry_losses=per_entry_losses
    )
    return field_round, SimpleNamespace(raw_results={"m1": result})


def _duel() -> dict:
    """Four entries; two carry ``drift:off_topic`` and one carries ``judge:tone``."""
    pairs = _pairs([False, True, True, False], [True, True, False, True], [0.0, 0.2, 0.1, 0.4])
    drift = (make_metric_count("drift:off_topic", count=2),)
    pairs[0] = (replace(pairs[0][0], metric_counts=drift), pairs[0][1])
    pairs[1] = (replace(pairs[1][0], metric_counts=drift), pairs[1][1])
    pairs[2] = (
        pairs[2][0],
        replace(pairs[2][1], metric_counts=(make_metric_count("judge:tone", "", count=1),)),
    )
    return {parent.entry_id: (parent, child) for parent, child in pairs}


def _hypothesis(magnitude: str):
    return make_hypothesis_spec(
        expected_metric_movements=(
            ExpectedMetricMovement("drift:off_topic", "decrease", magnitude),  # type: ignore[arg-type]
        )
    )


def test_settlement_records_nothing_with_the_knob_off(tmp_path: Path) -> None:
    field_round, execution = _field(ScoringWeights(), _duel(), tmp_path)
    assert _train_feedback(field_round, execution, "v2", _hypothesis("large"), 0.0) == {}


def test_settlement_records_train_feedback_with_the_knob_on(tmp_path: Path) -> None:
    duel = _duel()
    field_round, execution = _field(_ON, duel, tmp_path)
    delta = _delta(list(duel.values()), _ON)

    feedback = _train_feedback(field_round, execution, "v2", _hypothesis("large"), delta)

    assert (feedback["train_fixed"], feedback["train_broke"]) == (2, 1)
    assert feedback["train_entries"] == 4
    assert feedback["scalar_score_delta_se"] == pytest.approx(
        paired_scalar_se(list(duel.values()), _ON, delta)
    )
    movement = {m.metric_name: m for m in feedback["metric_movements"]}["drift:off_topic"]
    assert (movement.from_value, movement.to_value, movement.note) == (1.0, 0.0, "")


def test_the_stored_verdict_grades_direction_and_magnitude(tmp_path: Path) -> None:
    """A mean of 1.0 falling to 0.0 with no indexed range is a large decrease."""
    field_round, execution = _field(_ON, _duel(), tmp_path)

    def verdict(magnitude: str) -> bool:
        feedback = _train_feedback(field_round, execution, "v2", _hypothesis(magnitude), 0.0)
        return {m.metric_name: m for m in feedback["metric_movements"]}[
            "drift:off_topic"
        ].hypothesis_match

    assert verdict("large") is True
    assert verdict("medium") is False  # right direction, wrong size


def test_a_metric_on_one_training_entry_is_marked(tmp_path: Path) -> None:
    field_round, execution = _field(_ON, _duel(), tmp_path)
    feedback = _train_feedback(field_round, execution, "v2", _hypothesis("large"), 0.0)
    notes = {m.metric_name: m.note for m in feedback["metric_movements"]}
    assert notes["judge:tone"] == SINGLE_ENTRY_NOTE
    assert notes["drift:off_topic"] == "" and notes["failure:tasks"] == ""


def test_settlement_never_counts_an_entry_outside_the_training_slice(tmp_path: Path) -> None:
    duel = _duel()
    all_train, execution = _field(_ON, duel, tmp_path)
    # ``t0`` is one of the two fixed entries; treat it as a holdout entry.
    without_t0, _ = _field(_ON, duel, tmp_path, train_ids=("t1", "t2", "t3"))

    full = _train_feedback(all_train, execution, "v2", _hypothesis("large"), 0.0)
    train_only = _train_feedback(without_t0, execution, "v2", _hypothesis("large"), 0.0)

    assert (full["train_fixed"], train_only["train_fixed"]) == (2, 1)
    assert (full["train_entries"], train_only["train_entries"]) == (4, 3)
    drift = {m.metric_name: m for m in train_only["metric_movements"]}["drift:off_topic"]
    assert drift.from_value == pytest.approx(2 / 3)  # ``t0``'s two counts are gone


def test_settlement_orients_a_duel_that_lists_the_champion_second(tmp_path: Path) -> None:
    duel = {entry: (child, parent) for entry, (parent, child) in _duel().items()}
    field_round, execution = _field(_ON, duel, tmp_path)
    execution.raw_results["m1"].parent_generation_id = "v2"
    execution.raw_results["m1"].child_generation_id = "v1"
    feedback = _train_feedback(field_round, execution, "v2", _hypothesis("large"), 0.0)
    assert (feedback["train_fixed"], feedback["train_broke"]) == (2, 1)


def test_a_challenger_that_never_met_the_champion_records_nothing(tmp_path: Path) -> None:
    field_round, execution = _field(_ON, _duel(), tmp_path)
    assert _train_feedback(field_round, execution, "v9", _hypothesis("large"), 0.0) == {}


# -- the stored record --------------------------------------------------------


def test_an_outcome_without_feedback_omits_the_optional_keys() -> None:
    body = outcome_to_dict(make_outcome_record())
    assert not set(OUTCOME_OPTIONAL_FIELDS) & set(body)
    assert outcome_from_dict(json.loads(json.dumps(body))) == make_outcome_record()


def test_an_outcome_with_feedback_round_trips() -> None:
    outcome = make_outcome_record(
        scalar_score_delta_se=0.02, train_fixed=9, train_broke=7, train_entries=40
    )
    body = outcome_to_dict(outcome)
    assert [body[name] for name in OUTCOME_OPTIONAL_FIELDS] == [0.02, 9, 7, 40]
    assert outcome_from_dict(json.loads(json.dumps(body))) == outcome


# -- the reader ---------------------------------------------------------------

_ENTRY_IDS = ("refund_task_17", "holdout_escalation_4")
#: A metric name an emitter invented, carrying an entry id and task text.
_PROBE = "drift:custom:refund_task_17 user asks for a refund of order 4411"
_JUDGES = ("tone", "brevity")


def _settled_row(db: Path) -> None:
    hypothesis = {
        "core_idea": "Forbid uncited claims.",
        "modulating": ["researcher.instruction"],
        "expected_metric_movements": [
            {"metric_name": "drift:off_topic", "direction": "decrease", "magnitude": "medium"},
            {"metric_name": "judge:tone", "direction": "increase_or_neutral", "magnitude": "small"},
            {"metric_name": "cost:tokens_spent", "direction": "decrease", "magnitude": "small"},
            {"metric_name": "rubric:refund_task_17", "direction": "increase", "magnitude": "small"},
        ],
    }
    outcome = {
        "metric_movements": [
            {"metric_name": "drift:off_topic", "from_value": 0.7391, "to_value": 0.4391},
            {"metric_name": "judge:tone", "from_value": 0.2137, "to_value": 0.2137},
            {"metric_name": "rubric:refund_task_17", "from_value": 0.1, "to_value": 0.15},
            {"metric_name": "failure:tasks", "from_value": 0.1123, "to_value": 0.9123},
            {"metric_name": "judge:brevity", "from_value": 0.5123, "to_value": 0.2623},
            {"metric_name": _PROBE, "from_value": 0.0, "to_value": 0.9},
            {
                "metric_name": "judge:one_entry_only",
                "from_value": 0.0,
                "to_value": 0.9,
                "note": SINGLE_ENTRY_NOTE,
            },
            {"metric_name": "runtime:seconds", "from_value": 1.0, "to_value": 1.0437},
        ],
        "scalar_score_delta_se": 0.0123,
        "train_fixed": 9,
        "train_broke": 7,
        "train_entries": 40,
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
    assert entry.train_entries is None
    assert entry.predicted_movements == () and entry.realised_movements == ()


def test_reader_bands_the_feedback_with_the_knob_on(tmp_path: Path) -> None:
    db = _new_index(tmp_path)
    _settled_row(db)
    (entry,) = prior_experiments_for_epoch(db, "e1", outcome_feedback=True)

    assert (entry.scalar_delta_se, entry.train_fixed, entry.train_broke) == (0.0123, 9, 7)
    assert entry.train_entries == 40
    assert len(entry.predicted_movements) == 4
    assert entry.realised_movements == (
        ExpectedMetricMovement("drift:off_topic", "decrease", "medium"),
        ExpectedMetricMovement("judge:tone", "neutral", "small"),
        ExpectedMetricMovement("rubric:refund_task_17", "increase", "small"),
        # Unpredicted, largest first. The small runtime movement and the judge
        # measured on a single training entry are left out.
        ExpectedMetricMovement(_PROBE, "increase", "large"),  # type: ignore[arg-type]
        ExpectedMetricMovement("failure:tasks", "increase", "large"),
        ExpectedMetricMovement("judge:brevity", "decrease", "medium"),
    )


def test_a_prediction_on_a_single_entry_metric_reads_as_unmeasured(tmp_path: Path) -> None:
    db = _new_index(tmp_path)
    prediction = {"metric_name": "judge:solo", "direction": "decrease", "magnitude": "large"}
    movement = {"metric_name": "judge:solo", "from_value": 1.0, "to_value": 0.0}
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v2",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=json.dumps({"modulating": [], "expected_metric_movements": [prediction]}),
        outcome_json=json.dumps({"metric_movements": [{**movement, "note": SINGLE_ENTRY_NOTE}]}),
    )
    (entry,) = prior_experiments_for_epoch(db, "e1", outcome_feedback=True)
    assert entry.realised_movements == ()
    block = render_prior_experiments_block([entry], restrict=True, judge_names=["solo"])
    assert "judge:solo predicted decrease large, unmeasured" in block


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


def _index_run(db: Path, run_id: str, entry_id: str, metric: str, count: float) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO runs (run_id, epoch_id, generation_id, entry_id) "
            "VALUES (?, 'e1', 'v1', ?)",
            (run_id, entry_id),
        )
        conn.execute(
            "INSERT INTO metric_counts (run_id, namespace, name, severity, count) "
            "VALUES (?, 'judge:', ?, '', ?)",
            (run_id, metric, count),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_holdout_run_cannot_move_a_magnitude_bucket(tmp_path: Path) -> None:
    """A movement of 0.3 is large against a range of 0.5 and small against 5."""
    db = _new_index(tmp_path)
    _index_run(db, "r1", "train_a", "judge:tone", 0.5)
    _index_run(db, "r2", "holdout_b", "judge:tone", 5.0)
    assert _metric_ranges_for_epoch(db, "e1")["judge:tone"] == 5.0
    assert _metric_ranges_for_epoch(db, "e1", frozenset({"train_a"}))["judge:tone"] == 0.5
    _insert_experiment(
        db,
        epoch_id="e1",
        generation_id="v2",
        decision="promoted",
        scalar_delta=-0.1,
        hypothesis_json=json.dumps({"modulating": [], "expected_metric_movements": []}),
        outcome_json=json.dumps(
            {
                "metric_movements": [
                    {"metric_name": "judge:tone", "from_value": 0.0, "to_value": 0.3}
                ]
            }
        ),
    )

    def buckets(train: frozenset[str] | None) -> list[str]:
        (entry,) = prior_experiments_for_epoch(
            db, "e1", outcome_feedback=True, train_entry_ids=train
        )
        return [m.magnitude for m in entry.realised_movements]

    assert buckets(None) == []  # small against the holdout-inflated range
    assert buckets(frozenset({"train_a"})) == ["large"]


# -- the rendering ------------------------------------------------------------


def _prior(tmp_path: Path) -> list[PriorExperiment]:
    db = _new_index(tmp_path)
    _settled_row(db)
    return prior_experiments_for_epoch(db, "e1", outcome_feedback=True)


def test_restricted_entry_renders_bands_and_declared_names_only(tmp_path: Path) -> None:
    block = render_prior_experiments_block(_prior(tmp_path), restrict=True, judge_names=_JUDGES)
    assert block == (
        "(clear) and (within noise) compare a change with the spread of per-task "
        "differences across train tasks.\n\n"
        "Already rejected (do NOT re-propose these unless something changed):\n"
        "- v2 REJECTED Δscalar=improved (clear)  [researcher.instruction]  "
        "(holdout_not_confirmed)  prediction:high\n"
        "    Forbid uncited claims.\n"
        "    train tasks: fixed 4+, broke 4+\n"
        "    predictions: drift:off_topic predicted decrease medium, actual decrease medium "
        "(direction ok, size ok); judge:tone predicted increase_or_neutral small, actual "
        "neutral small (direction ok, size ok); cost:tokens_spent predicted decrease small, "
        "unmeasured\n"
        "    unpredicted: failure:tasks increase large; judge:brevity decrease medium"
    )


def test_restricted_rendering_drops_an_undeclared_metric_name(tmp_path: Path) -> None:
    prior = _prior(tmp_path)
    assert _PROBE in render_prior_experiments_block(prior, restrict=False)

    restricted = render_prior_experiments_block(prior, restrict=True, judge_names=_JUDGES)
    assert "refund" not in restricted and "4411" not in restricted
    # A judge the contract does not declare is treated the same way.
    undeclared = render_prior_experiments_block(prior, restrict=True)
    assert "judge:" not in undeclared and "drift:off_topic" in undeclared


def test_the_evidence_shown_to_proposer_and_critic_uses_the_declared_judges(
    tmp_path: Path,
) -> None:
    rendered = render_proposal_evidence(
        mutations=[make_mutation_point()],
        prior_experiments=_prior(tmp_path),
        custom_judge_names=_JUDGES,
        restrict_visibility=True,
    )
    assert "judge:brevity decrease medium" in rendered
    assert "refund" not in rendered and not any(entry in rendered for entry in _ENTRY_IDS)


def test_unrestricted_entry_renders_exact_numbers(tmp_path: Path) -> None:
    lines = render_prior_experiments_block(_prior(tmp_path), restrict=False).splitlines()
    assert lines[1].startswith("- v2 REJECTED Δscalar=-0.034 ±0.012  [researcher.instruction]")
    assert lines[3] == "    train tasks: fixed 9, broke 7"
    assert not lines[0].startswith("(clear)")


def test_restricted_entry_leaks_no_entry_id_and_no_metric_value(tmp_path: Path) -> None:
    block = render_prior_experiments_block(_prior(tmp_path), restrict=True, judge_names=_JUDGES)
    assert not any(entry in block for entry in _ENTRY_IDS)
    # The only digits are the generation id and the two banded counts.
    assert re.findall(r"\d+(?:\.\d+)?", block) == ["2", "4", "4"]


def test_the_cap_keeps_the_training_task_counts() -> None:
    entry = PriorExperiment(
        generation_id="v2",
        epoch_id="e1",
        core_idea="idea",
        modulating=(),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=-0.1,
        train_fixed=3,
        train_broke=0,
        predicted_movements=tuple(
            ExpectedMetricMovement(f"judge:a_long_judge_name_{i}", "decrease", "medium")
            for i in range(40)
        ),
    )
    feedback = render_prior_experiments_block([entry]).split("    idea", 1)[1]
    assert len(feedback) == OUTCOME_FEEDBACK_MAX_CHARS and feedback.endswith("…")
    assert feedback.startswith("\n    train tasks: fixed 3, broke 0\n")


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
    assert render_prior_experiments_block([entry], restrict=True) == (
        "Already promoted (build on these — the direction worked):\n"
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
        assert (outcome.train_fixed, outcome.train_broke, outcome.train_entries) == (1, 0, 4)
        # Four paired differences of -1, 0, 0, 0: sd 0.5, over sqrt(4).
        assert outcome.scalar_score_delta_se == pytest.approx(0.25)
        assert outcome.metric_movements
    prior = prior_experiments_for_epoch(workspace / "index.db", epoch, outcome_feedback=True)
    block = render_prior_experiments_block(prior, restrict=True)
    # Four tasks are too few for the qualifier, so the plain band renders.
    assert block.count("Δscalar=improved  [") == 3 and "(within noise)" not in block
    assert block.count("train tasks: fixed 1, broke 0") == 3
