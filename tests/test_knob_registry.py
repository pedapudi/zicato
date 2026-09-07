"""Scoring defaults, identity omissions, and accepted value ranges."""

from __future__ import annotations

import pytest

from zicato.contract_draft import operations
from zicato.contract_draft.draft import TournamentDraft
from zicato.core.constraints import knob_constraint
from zicato.core.scoring_config import (
    ContractKnob,
    ExperimentalConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
    contract_knobs,
    omit_at_default_fields,
)
from zicato.core.tournament import TournamentStructure

_SCORING_OMIT_AT_DEFAULT_FIELDS = omit_at_default_fields()

# ---------------------------------------------------------------------------
# Guard 1 — the derived omit set must equal the pinned frozen literal.
# ---------------------------------------------------------------------------

#: The FROZEN omit-at-default field-name set as captured against the parity
#: goldens. This literal used to live in ``epoch/contract.py`` where a typo
#: could silently move the contract hash; it now lives HERE as the guard the
#: metadata-derived set is pinned against. Adding a genuinely-new additive
#: knob means updating BOTH the field metadata AND this literal — a deliberate
#: two-hands ritual, because either alone would be a contract-hash bug.
_FROZEN_OMIT_AT_DEFAULT_FIELDS = frozenset(
    {
        # The declared mutation-site syntax table (issue #168). Additive and
        # empty by default — every workspace that never declares a file type
        # keeps the hash it has, while a declared suffix widens the surface
        # and rolls the epoch.
        "mutation_surface",
        "diff_complexity_weight",
        "diff_complexity_ceiling",
        # The holdout confirmation's own bounds (issue #118). Additive and
        # default-inert: ``holdout_margin=None`` reuses ``promote_margin`` and
        # a budget of 0 is the historical zero-tolerance rule, so omitting
        # both at their default keeps every existing epoch's hash where it is.
        "holdout_margin",
        "holdout_entry_regression_budget",
        # The opt-ins for features without a measured case (issue #394).
        # Omitted while every flag is off, so a contract naming none of
        # them keeps its hash; a flag turned on rolls the epoch.
        "experimental",
        "max_generations_per_contract",
        "cross_epoch_memory",
        "standing_rating",
        "resolver",
        "random_baseline_every_n",
        "block_on_containment_violation",
        "block_on_gate_contradiction",
        "screen_entries",
        "screen_veto_only",
        "process_exemplars",
        "recombine",
        "recombine_merge",
        "genealogy",
        "calibration_feedback",
        "telemetry_dialect",
        # Optional integration settings are absent from generic contracts.
        # Activating the block makes every nested value contract-bearing.
        "goldfive",
    }
)


def test_derived_omit_set_equals_frozen_literal() -> None:
    """The metadata-derived omit set is byte-identical to the frozen literal.

    ``_SCORING_OMIT_AT_DEFAULT_FIELDS`` is now derived from the per-field
    ``omit_at_default`` metadata. A metadata typo (flag added to the wrong
    field, or dropped from an omit field) would change which keys the
    canonicalizer emits at their default and therefore the contract hash for
    every existing epoch. This pins the derived set to the frozen current set
    so any such drift reds HERE (loudly, per-field) instead of silently in the
    hash.
    """
    added = _SCORING_OMIT_AT_DEFAULT_FIELDS - _FROZEN_OMIT_AT_DEFAULT_FIELDS
    dropped = _FROZEN_OMIT_AT_DEFAULT_FIELDS - _SCORING_OMIT_AT_DEFAULT_FIELDS
    assert not added and not dropped, (
        "the metadata-derived omit-at-default set drifted from the frozen "
        f"literal — added {sorted(added)}, dropped {sorted(dropped)}. An "
        "omit_at_default metadata flag was added to / removed from a field "
        "without the deliberate matching update to _FROZEN_OMIT_AT_DEFAULT_FIELDS "
        "in this test. Because the omit set decides which default-valued keys "
        "the contract canonicalizer emits, this drift would move the CONTRACT "
        "hash for existing epochs. Reconcile the field metadata and this literal."
    )


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


def _bounded_knobs() -> list[ContractKnob]:
    """Every knob that declares a bound, in declaration order."""
    return [knob for knob in contract_knobs() if _declares_a_bound(knob)]


def _declares_a_bound(knob: ContractKnob) -> bool:
    try:
        knob_constraint(knob.owner, knob.name)
    except KeyError:
        return False
    return True


@pytest.mark.parametrize("knob", _bounded_knobs(), ids=lambda knob: knob.key)
def test_loader_refuses_values_outside_declared_bounds(knob: ContractKnob) -> None:
    """Authored values must satisfy every declared bound."""
    value = _INADMISSIBLE_VALUES[(knob.owner, knob.name)]
    with pytest.raises(ValueError) as from_loader:
        knob.owner(**{knob.name: value})
    expected_name = knob_constraint(knob.owner, knob.name).label or knob.name
    assert str(from_loader.value).startswith(expected_name)


def test_every_bounded_knob_has_an_inadmissible_value() -> None:
    """No knob may declare a bound with no case pinning both surfaces to it."""
    declared = {(knob.owner, knob.name) for knob in _bounded_knobs()}
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
    """Every flag in the ``experimental`` block stays off in the scaffold.

    The block holds features without a measured case (issue #394). A
    feature graduates by moving out of it; the scaffold turns no flag on.
    Walking the dataclass fields keeps the pin true for a flag added later.
    """
    scaffold = ScoringWeights().experimental
    enabled = [
        knob.name
        for knob in contract_knobs()
        if knob.owner is ExperimentalConfig and getattr(scaffold, knob.name) != knob.default
    ]
    assert not enabled, f"the recommended scaffold enables experimental knob(s) {enabled}"


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
