"""Guard tests for the frozen-contract serializer (issue #13).

The epoch evaluation contract — :class:`ScoringWeights` and its nested
config dataclasses — is serialized by two code paths that MUST agree on
which fields exist:

* the contract-hash canonicalizer
  (:func:`zicato.epoch.contract.scoring_to_canon`), which enumerates
  ``dataclasses.fields()`` and therefore covers every field; and
* the frozen-epoch snapshot writer/parser/loader.

Historically the snapshot path was a hand-maintained, field-by-field
dict. When a new field was added and threaded through the canonicalizer
but NOT the hand-written writer, the frozen ``scoring.json`` silently
dropped it; on the next ``evolve`` the live contract hashed differently
from the frozen one and the orchestrator performed a *spurious* epoch
auto-roll.

These tests pin the GENERAL invariant for the WHOLE contract-dataclass
family, not one field:

    from_dict(to_dict(x)) == x                          # round-trip identity
    canon(x) == canon(from_dict(to_dict(x)))            # no spurious roll
    every dataclass field appears in to_dict(x)         # no dropped field

The structural tests iterate ``dataclasses.fields()`` and synthesise a
non-default value for each field, so a FUTURE field added to any contract
dataclass is covered automatically — if a serializer drops it, these
tests fail without anyone having to remember to extend them.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import Any

import pytest

from zicato.core.scoring_config import SprtConfig
from zicato.core.types import (
    ExperimentMemoryConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.epoch.contract import round_floats, scoring_to_canon
from zicato.epoch.contract_serde import (
    _persisted_key,
    dataclass_to_jsonable,
    jsonable_to_dataclass,
)
from zicato.epoch.lifecycle import _scoring_from_dict, scoring_to_dict
from zicato.workspace_loader import scoring_weights_from_dict

# Every contract dataclass whose frozen-snapshot serialization must be
# field-complete. The structural tests below cover each one.
_CONTRACT_DATACLASSES = [
    ScoringWeights,
    OverfittingConfig,
    LadderConfig,
    ProposerQualityConfig,
    ExperimentMemoryConfig,
    SprtConfig,
]

# A hand-curated, constraint-VALID non-default value for every field of
# every contract dataclass. Hand-curated (rather than blindly mutated)
# because the dataclasses enforce range/validity in ``__post_init__`` — a
# blind mutator produces out-of-range values that never construct. Keyed by
# class name, then field name.
#
# ``_all_fields_nondefault`` asserts that EVERY ``dataclasses.fields()``
# entry is present here and differs from the default. So when a future
# field is added to any contract dataclass, these tests FAIL until the new
# field is added to this table — which is exactly how a future dropped
# field is caught: the same table drives the round-trip + no-roll guards.
_NONDEFAULT_VALUES: dict[str, dict[str, Any]] = {
    "LadderConfig": {
        "enabled": False,
        "threshold": 0.25,
        "budget": 4,
        "noise_scale": 0.1,
    },
    "OverfittingConfig": {
        "enabled": False,
        "holdout_fraction": 0.42,
        "min_board_size_for_split": 15,
        "restrict_proposer_visibility": False,
        "ladder": LadderConfig(enabled=False, threshold=0.25, budget=4, noise_scale=0.1),
        "rotate_holdout": False,
        "max_generations_per_contract": 9,
        "random_baseline_every_n": 5,
    },
    "ProposerQualityConfig": {
        "best_of_n": 4,
        "critique_enabled": False,
        "screen_entries": 3,
        "screen_veto_only": True,
        "process_exemplars": 2,
        "recombine": True,
        "genealogy": 4,
        "calibration_feedback": 5,
        "recombine_merge": "llm",
    },
    "ExperimentMemoryConfig": {
        "cross_epoch": True,
    },
    "SprtConfig": {
        "preset": "balanced",
        "alpha": 0.01,
        "beta": 0.02,
        "min_replicates": 7,
    },
    "ScoringWeights": {
        "drift_weight": 2.5,
        "pass_weight": 3.5,
        "severity_weights": {"info": 2.0, "warning": 4.0, "critical": 11.0},
        "per_kind_weights": {"off_topic": 1.5},
        "per_judge_weights": {"quality": 4.0, "no_pii": 7.0},
        "default_judge_weight": 2.5,
        "plan_revision_weight": 0.9,
        "runtime_weight": 0.3,
        "diff_complexity_weight": 0.2,
        "diff_complexity_ceiling": 10.0,
        "promote_margin": 0.05,
        "holdout_margin": 0.11,
        "holdout_entry_regression_budget": 2,
        "pass_rate_monotonicity": False,
        "pass_rate_monotonicity_scope": "aggregate",
        "regression_gate_enabled": True,
        "regression_test_command": ("python", "-m", "unittest"),
        "regression_timeout_s": 120,
        "namespace_weights": {"drift:": 2.0, "cost:": 0.002},
        "namespace_monotonicity": {"drift:": True, "rubric:": False},
        "tournament_structure": TournamentStructure(
            structure="swiss", params={"rounds_n": 3, "nested": {"a": [1, 2, 3]}}
        ),
        "overfitting": OverfittingConfig(
            enabled=False,
            max_generations_per_contract=9,
            ladder=LadderConfig(threshold=0.27, budget=8),
        ),
        "proposer_quality": ProposerQualityConfig(best_of_n=5, critique_enabled=False),
        "experiment_memory": ExperimentMemoryConfig(cross_epoch=True),
        # SprtConfig: enable a preset (with overrides) so the round-trip
        # covers the non-default. tournament_structure above pins swiss, so
        # the racing cross-field guard does not fire.
        "sprt": SprtConfig(preset="balanced", alpha=0.01, beta=0.02, min_replicates=7),
        "outcome_summarizer_spec": "pkg.mod:summarize_outcomes",
        "pass_transform": {"op": "pow", "exponent": 2.0},
        "drift_kind_aggregation": {
            "looping_reasoning": {"op": "harmonic"},
            "off_topic": {"op": "cap", "max": 5.0},
        },
        # Issue #19 phase-3 dotted-spec scoring plugins. Folded into the contract
        # hash with a source hash; the bare-spec strings here resolve to nothing
        # at hash time (a degraded null source hash), which is fine for the
        # round-trip / drop-a-field guard this test exercises.
        "scalar_fn": "pkg.mod:my_scalar",
        "drift_reducer": "pkg.mod:my_drift_reducer",
        # Opt-in integrity blocking modes (default OFF; omit-at-default in
        # the canonicalizer — opting in rolls the epoch like any weight).
        "block_on_containment_violation": True,
        "block_on_gate_contradiction": True,
        # Telemetry dialect (TELEMETRY-DIALECTS.md): the pluggable LossProfile
        # producer. Default "goldfive" is omit-at-default; a non-default
        # dialect rolls the epoch like any weight.
        "telemetry_dialect": "adk_events",
    },
}


def _canon(weights: ScoringWeights) -> str:
    """The contract-hash canonical string for one ScoringWeights."""
    return json.dumps(round_floats(scoring_to_canon(weights)), sort_keys=True)


def _distinct_value(cls: type, field_name: str) -> Any:
    """The curated non-default value for ``cls.field_name``.

    Raises if the table is missing an entry — that is the signal that a
    new field was added to a contract dataclass and the guard table (and
    therefore the serializer) needs attention.
    """
    table = _NONDEFAULT_VALUES.get(cls.__name__, {})
    if field_name not in table:
        raise AssertionError(
            f"no curated non-default value for {cls.__name__}.{field_name}; add one to "
            f"_NONDEFAULT_VALUES in this test so the new contract field is guarded against "
            f"the issue #13 drop-a-field defect class"
        )
    return table[field_name]


def _all_fields_nondefault(cls: type) -> Any:
    """Construct ``cls`` with every field set to a curated non-default value.

    Iterates ``dataclasses.fields()`` so a field added in the future is
    covered automatically: the lookup raises until the guard table is
    extended, and once extended the new field flows through every
    round-trip / no-roll assertion below.
    """
    base = cls()
    overrides: dict[str, Any] = {}
    for f in fields(cls):
        if not f.init:
            continue
        value = _distinct_value(cls, f.name)
        assert value != getattr(base, f.name), (
            f"curated value for {cls.__name__}.{f.name} equals the default; "
            f"pick a genuinely different value so the guard is not vacuous"
        )
        overrides[f.name] = value
    inst = replace(base, **overrides)
    assert inst != base, f"failed to synthesise a non-default {cls.__name__}"
    return inst


# ---------------------------------------------------------------------------
# Structural: no serializer may drop ANY field of ANY contract dataclass.
# Iterates dataclasses.fields() so a FUTURE field is covered automatically.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _CONTRACT_DATACLASSES, ids=lambda c: c.__name__)
def test_every_field_appears_in_snapshot(cls: type) -> None:
    """The generic writer emits a key for EVERY declared field.

    This is the regression guard for issue #13: a hand-written writer that
    forgot a field would drop it from the snapshot. The writer is
    field-enumerating, so this asserts the property holds for every current
    field — and, because it iterates ``dataclasses.fields()``, for every
    field added in the future.
    """
    inst = _all_fields_nondefault(cls)
    snapshot = dataclass_to_jsonable(inst)
    for f in fields(cls):
        if not f.init:
            continue
        key = _persisted_key(cls.__name__, f.name)
        assert key in snapshot, (
            f"{cls.__name__}.{f.name} (key {key!r}) is missing from the frozen "
            f"snapshot — a serializer dropped a field; this is the issue #13 defect class"
        )


@pytest.mark.parametrize("cls", _CONTRACT_DATACLASSES, ids=lambda c: c.__name__)
def test_generic_round_trip_identity(cls: type) -> None:
    """``from_dict(to_dict(x)) == x`` for non-default values of every field."""
    inst = _all_fields_nondefault(cls)
    again = jsonable_to_dataclass(cls, dataclass_to_jsonable(inst))
    assert again == inst


# ---------------------------------------------------------------------------
# ScoringWeights via the actual lifecycle + loader entry points.
# ---------------------------------------------------------------------------


def test_scoring_lifecycle_round_trip_every_field() -> None:
    """``_scoring_from_dict(scoring_to_dict(w)) == w`` with every field
    set to a non-default value — the lifecycle (epoch-creation) path."""
    w = _all_fields_nondefault(ScoringWeights)
    assert _scoring_from_dict(scoring_to_dict(w)) == w


def test_scoring_loader_round_trip_every_field() -> None:
    """``scoring_weights_from_dict(scoring_to_dict(w)) == w`` — the
    workspace-loader / canonicalizer read path."""
    w = _all_fields_nondefault(ScoringWeights)
    assert scoring_weights_from_dict(scoring_to_dict(w)) == w


def test_lifecycle_parser_and_loader_agree() -> None:
    """The lifecycle parser and the workspace loader must produce the SAME
    ScoringWeights from the same dict — if they diverged on any field the
    frozen contract and the live contract would hash differently."""
    w = _all_fields_nondefault(ScoringWeights)
    d = scoring_to_dict(w)
    assert _scoring_from_dict(d) == scoring_weights_from_dict(d)


# ---------------------------------------------------------------------------
# Behavioral: persist -> load -> re-hash must NOT trigger a spurious roll.
# Simulated at the unit level (no live LLM evolve).
# ---------------------------------------------------------------------------


def test_persist_load_rehash_no_spurious_roll_default() -> None:
    """A default ScoringWeights persists, loads, and re-hashes identically —
    the zero-churn guarantee for every epoch already on disk."""
    w = ScoringWeights()
    reloaded = scoring_weights_from_dict(scoring_to_dict(w))
    assert _canon(w) == _canon(reloaded)


def test_persist_load_rehash_no_spurious_roll_every_field() -> None:
    """The core issue-#13 reproduction, fixed: a ScoringWeights with EVERY
    field at a non-default value must hash the same after persist->load, so
    the orchestrator's auto-roll decision is 'no change'.

    The orchestrator rolls iff ``stored_hash != live_hash`` (see
    :func:`zicato.orchestrator.ensure_epoch_for_contract`). The stored hash
    is computed over the live ScoringWeights at epoch creation; the live
    hash on the next evolve is computed over the frozen ``scoring.json``
    parsed back. If any field is dropped in between, the two canonical
    forms differ and the epoch spuriously rolls. This asserts they match.
    """
    w = _all_fields_nondefault(ScoringWeights)
    live_hash = _canon(w)
    # Persist exactly as new_epoch does, then re-read exactly as the
    # contract canonicalizer does on the next evolve.
    frozen = scoring_to_dict(w)
    reloaded = scoring_weights_from_dict(frozen)
    rolled_hash = _canon(reloaded)
    assert live_hash == rolled_hash, (
        "frozen contract hashes differently from the live contract after a "
        "persist->load round-trip — this is the spurious-auto-roll bug (#13)"
    )


def test_persist_load_rehash_no_roll_for_individual_nondefault_field() -> None:
    """Per-field: flipping ONE scoring field at a time and round-tripping
    must never change the canonical hash. Pinpoints exactly which field a
    future regression dropped."""
    base = ScoringWeights()
    for f in fields(ScoringWeights):
        if not f.init:
            continue
        w = replace(base, **{f.name: _distinct_value(ScoringWeights, f.name)})
        reloaded = scoring_weights_from_dict(scoring_to_dict(w))
        assert _canon(w) == _canon(reloaded), (
            f"round-tripping a non-default {f.name!r} changed the contract hash — "
            f"the frozen serializer dropped or mangled it"
        )


# ---------------------------------------------------------------------------
# Nested-dataclass coverage is recursive: a non-default value buried inside
# the tournament structure / overfitting / ladder block survives too.
# ---------------------------------------------------------------------------


def test_nested_tournament_and_overfitting_survive_round_trip() -> None:
    w = replace(
        ScoringWeights(),
        tournament_structure=TournamentStructure(
            structure="swiss", params={"rounds_n": 3, "nested": {"a": [1, 2, 3]}}
        ),
        overfitting=OverfittingConfig(
            enabled=False,
            max_generations_per_contract=9,
            ladder=LadderConfig(threshold=0.27, budget=4, noise_scale=0.1),
        ),
    )
    reloaded = _scoring_from_dict(scoring_to_dict(w))
    assert reloaded == w
    assert _canon(w) == _canon(reloaded)


def test_tournament_block_uses_legacy_key() -> None:
    """The tournament structure is persisted under ``"tournament"`` (not
    ``"tournament_structure"``) — the shape the dashboard builder and every
    existing on-disk ``scoring.json`` rely on."""
    snapshot = scoring_to_dict(ScoringWeights())
    assert "tournament" in snapshot
    assert "tournament_structure" not in snapshot


def test_legacy_scoring_json_loads_at_defaults() -> None:
    """A minimal legacy ``scoring.json`` (only a couple of keys) loads with
    every absent field at its dataclass default — back-compat for epochs
    frozen before later fields landed."""
    legacy = {"drift_weight": 1.0, "pass_weight": 1.0, "promote_margin": 0.01}
    w = scoring_weights_from_dict(legacy)
    assert w == ScoringWeights()


def test_continuous_score_adds_no_scoring_contract_field() -> None:
    """The per-entry continuous-score feature (#18 cap 1) adds NO scoring config.

    ``score`` / ``metrics`` are reducer OUTPUT (loss.json), not contract
    inputs, so they must never appear in the scoring canon — otherwise they
    would enter the contract hash and roll the epoch. Enabling continuous
    scores is opt-in per board entry (the operator writes a float scorer),
    not via a ScoringWeights flag; back-compat is automatic via score=None.
    """
    canon = scoring_to_canon(ScoringWeights())
    assert "score" not in canon
    assert "metrics" not in canon
    assert "mean_score" not in canon
    # And the default contract hash is unchanged shape-wise: no new top-level
    # scoring key was introduced by this feature.
    field_names = {f.name for f in fields(ScoringWeights)}
    assert "score" not in field_names
    assert "metrics" not in field_names


def test_experiment_memory_omitted_from_canon_at_default() -> None:
    """The opt-in cross-epoch memory knob is additive: an unset (or
    explicitly-default) ``experiment_memory`` block is OMITTED from the
    canonical scoring form, so every existing epoch keeps its hash; opting
    in emits the block and rolls the epoch like any contract change."""
    canon_default = scoring_to_canon(ScoringWeights())
    assert "experiment_memory" not in canon_default

    explicit_default = ScoringWeights(experiment_memory=ExperimentMemoryConfig())
    assert _canon(explicit_default) == _canon(ScoringWeights())

    opted_in = ScoringWeights(experiment_memory=ExperimentMemoryConfig(cross_epoch=True))
    assert scoring_to_canon(opted_in)["experiment_memory"] == {"cross_epoch": True}
    assert _canon(opted_in) != _canon(ScoringWeights())
