"""Scoring defaults and accepted value ranges."""

from __future__ import annotations

from dataclasses import MISSING, fields

import pytest

from zicato.contract_draft import operations
from zicato.contract_draft.draft import TournamentDraft
from zicato.core.constraints import KnobConstraint, knob_constraint
from zicato.core.scoring_config import (
    ExperimentalConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
)
from zicato.core.tournament import TournamentStructure

_INADMISSIBLE_VALUES: dict[tuple[type, str], object] = {
    (ScoringWeights, "pass_weight"): float("nan"),
    (ScoringWeights, "default_judge_weight"): float("inf"),
    (ScoringWeights, "plan_revision_weight"): float("nan"),
    (ScoringWeights, "task_failure_weight"): float("nan"),
    (ScoringWeights, "not_completed_weight"): float("nan"),
    (ExperimentalConfig, "diff_complexity_weight"): -1.0,
    (ExperimentalConfig, "diff_complexity_ceiling"): -1.0,
    (ScoringWeights, "promote_margin"): -0.05,
    (ScoringWeights, "holdout_margin"): float("nan"),
    (ScoringWeights, "holdout_entry_regression_budget"): -1,
    (ScoringWeights, "pass_rate_monotonicity_scope"): "per_namespace",
    (ScoringWeights, "regression_timeout_s"): 0,
    (ScoringWeights, "telemetry_dialect"): "syslog",
    (OverfittingConfig, "min_board_size_for_split"): -1,
    (OverfittingConfig, "holdout_fraction"): 0.0,
    # Not ``0``: ``set_holdout`` reserves that as the token that CLEARS the
    # ceiling, since ``None`` there already means "leave unchanged".
    (ExperimentalConfig, "max_generations_per_contract"): -1,
    (ExperimentalConfig, "random_baseline_every_n"): -1,
    (LadderConfig, "threshold"): -0.5,
    (LadderConfig, "budget"): -1,
    (ProposerQualityConfig, "best_of_n"): 0,
    (ProposerQualityConfig, "screen_entries"): -1,
    (ExperimentalConfig, "process_exemplars"): -1,
    (ExperimentalConfig, "genealogy"): -1,
    (ExperimentalConfig, "calibration_feedback"): -1,
    (ExperimentalConfig, "recombine_merge"): "union",
}


def _bounded_knobs() -> list[tuple[type, str]]:
    """Enumerate the constrained fields on the supported scoring dataclasses."""
    return [
        (owner, declared.name)
        for owner in (
            ScoringWeights,
            OverfittingConfig,
            LadderConfig,
            ProposerQualityConfig,
            ExperimentalConfig,
        )
        for declared in fields(owner)
        if isinstance(declared.metadata.get("constraint"), KnobConstraint)
    ]


@pytest.mark.parametrize("owner,name", _bounded_knobs())
def test_loader_refuses_values_outside_declared_bounds(owner: type, name: str) -> None:
    """Every declared bound refuses an independently chosen invalid value."""
    value = _INADMISSIBLE_VALUES[(owner, name)]
    with pytest.raises(ValueError) as from_loader:
        owner(**{name: value})
    expected_name = knob_constraint(owner, name).label or name
    assert str(from_loader.value).startswith(expected_name)


def test_every_bounded_knob_has_an_inadmissible_value() -> None:
    """No knob may declare a bound with no case pinning both surfaces to it."""
    declared = set(_bounded_knobs())
    missing = sorted(
        f"{owner.__name__}.{name}" for owner, name in declared - set(_INADMISSIBLE_VALUES)
    )
    assert not missing, (
        f"knob(s) {missing} declare a bound with no entry in _INADMISSIBLE_VALUES — "
        "add a value the bound forbids."
    )
    stale = sorted(
        f"{owner.__name__}.{name}" for owner, name in set(_INADMISSIBLE_VALUES) - declared
    )
    assert not stale, f"_INADMISSIBLE_VALUES names knob(s) {stale} that declare no bound."


def test_recommended_scaffold_enables_no_experimental_knob() -> None:
    """The recommended configuration retains every experimental field's default."""
    scaffold = ScoringWeights().experimental
    for declared in fields(ExperimentalConfig):
        default = (
            declared.default if declared.default is not MISSING else declared.default_factory()
        )
        assert getattr(scaffold, declared.name) == default, declared.name


def test_authored_schema_groups_experimental_fields_without_historical_aliases() -> None:
    from dataclasses import fields

    from zicato.core.configuration import dataclass_schema

    properties = dataclass_schema(ScoringWeights)["properties"]
    experimental = properties["experimental"]["properties"]
    assert set(experimental) == {item.name for item in fields(ExperimentalConfig)}
    assert "experiment_memory" not in properties
    assert "diff_complexity_weight" not in properties
    assert "random_baseline_every_n" not in properties["overfitting"]["properties"]
    ordinary = properties["proposer_quality"]["properties"]
    assert "process_exemplars" not in ordinary
    assert {
        "best_of_n",
        "critique_enabled",
        "screen_entries",
        "screen_veto_only",
    } <= ordinary.keys()
    assert experimental["standing_rating"]["enum"] == ["none", "bradley_terry"]
    assert experimental["resolver"]["enum"] == ["none", "copeland", "ranked_pairs"]


def test_promote_margin_may_not_invert_the_gate() -> None:
    """A negative promote margin is refused rather than promoting a regression.

    The gate's scalar rule is ``delta_scalar <= -promote_margin``, so a margin
    of ``-0.05`` would promote a challenger that scored 0.05 WORSE than the
    champion. Nothing rejected it before: the field was checked for
    finiteness alone.
    """
    with pytest.raises(ValueError, match="promote_margin must be >= 0"):
        ScoringWeights(promote_margin=-0.05)
    # A zero margin is a bar of zero, not an inversion.
    ScoringWeights(promote_margin=0.0)


@pytest.mark.parametrize("replicates", [0, -2])
def test_zero_or_negative_replicates_is_refused(replicates: int) -> None:
    """A duel count below one is refused at load instead of clamped at run time.

    ``replicates`` lives in the untyped structure-params mapping, where every
    strategy that reads it clamps with ``max(1, ...)`` — so an operator who
    wrote ``0`` got single-run duels and no indication their setting was
    ignored.
    """
    with pytest.raises(ValueError, match=r'tournament params\["replicates"\] must be >= 1'):
        TournamentStructure(structure="swiss", params={"replicates": replicates})
    with pytest.raises(ValueError, match=r'tournament params\["replicates"\] must be >= 1'):
        operations.set_param(TournamentDraft(), "replicates", replicates)
