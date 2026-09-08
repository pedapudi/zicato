"""Every scoring field survives serialization and retains its contract identity.

Independent non-default values cover the declared fields, nested configurations,
worker payloads, and saved scoring readers. Sparse input resolves to the same
configuration as explicitly supplied defaults.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import Any

import pytest

from zicato.core.configuration import (
    authored_dataclass_from_json,
    dataclass_to_jsonable,
    persisted_key,
)
from zicato.core.types import (
    ExperimentalConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.epoch.contract import scoring_to_canon
from zicato.epoch.lifecycle import _scoring_from_dict, scoring_to_dict
from zicato.workspace_loader import scoring_weights_from_dict

# Every contract dataclass whose frozen-snapshot serialization must be
# field-complete. The structural tests below cover each one.
_CONTRACT_DATACLASSES = [
    ScoringWeights,
    OverfittingConfig,
    LadderConfig,
    ProposerQualityConfig,
    ExperimentalConfig,
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
    },
    "OverfittingConfig": {
        "enabled": False,
        "holdout_fraction": 0.42,
        "min_board_size_for_split": 15,
        "restrict_proposer_visibility": False,
        "ladder": LadderConfig(enabled=False, threshold=0.25, budget=4),
        "rotate_holdout": False,
    },
    "ProposerQualityConfig": {
        "best_of_n": 4,
        "critique_enabled": False,
        "screen_entries": 3,
        "screen_veto_only": True,
    },
    "ExperimentalConfig": {
        "tournament_structures": True,
        "max_generations_per_contract": 9,
        "random_baseline_every_n": 5,
        "process_exemplars": 2,
        "recombine": True,
        "genealogy": 4,
        "calibration_feedback": 5,
        "recombine_merge": "llm",
        "diff_complexity_weight": 0.2,
        "diff_complexity_ceiling": 10.0,
        "cross_epoch_memory": True,
        "standing_rating": "bradley_terry",
        "resolver": "ranked_pairs",
    },
    "ScoringWeights": {
        "goldfive": {"fail_fast_on_revision_rejection": True},
        "pass_weight": 3.5,
        "severity_weights": {"info": 2.0, "warning": 4.0, "critical": 11.0},
        "per_kind_weights": {"off_topic": 1.5},
        "per_judge_weights": {"quality": 4.0, "no_pii": 7.0},
        "default_judge_weight": 2.5,
        "plan_revision_weight": 0.9,
        "task_failure_weight": 12.0,
        "not_completed_weight": 75.0,
        "promote_margin": 0.05,
        "holdout_margin": 0.11,
        "holdout_entry_regression_budget": 2,
        "pass_rate_monotonicity": False,
        "pass_rate_monotonicity_scope": "aggregate",
        "regression_gate_enabled": True,
        "regression_test_command": ("python", "-m", "unittest"),
        "regression_timeout_s": 120,
        "namespace_weights": {"drift:": 2.0, "failure:": 2.0, "cost:": 0.002},
        "namespace_monotonicity": {"drift:": True, "rubric:": False},
        "tournament_structure": TournamentStructure(
            structure="racing", params={"rounds_n": 3, "nested": {"a": [1, 2, 3]}}
        ),
        "overfitting": OverfittingConfig(
            enabled=False,
            ladder=LadderConfig(threshold=0.27, budget=8),
        ),
        "proposer_quality": ProposerQualityConfig(best_of_n=5, critique_enabled=False),
        "experimental": ExperimentalConfig(tournament_structures=True),
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
        # Enabled integrity checks change the evaluation contract.
        "block_on_containment_violation": True,
        "block_on_gate_contradiction": True,
        # The telemetry dialect identifies the loss-profile producer.
        "telemetry_dialect": "adk_events",
        # A declared file type expands the mutation surface.
        "mutation_surface": {".ts": {"leaders": ["//", "/*"], "trailers": ["*/"]}},
    },
}


def _canon(weights: ScoringWeights) -> str:
    """The contract-hash canonical string for one ScoringWeights."""
    return json.dumps(scoring_to_canon(weights), sort_keys=True)


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
        key = persisted_key(f)
        assert key in snapshot, (
            f"{cls.__name__}.{f.name} (key {key!r}) is missing from the frozen "
            f"snapshot — a serializer dropped a field; this is the issue #13 defect class"
        )


@pytest.mark.parametrize("cls", _CONTRACT_DATACLASSES, ids=lambda c: c.__name__)
def test_generic_round_trip_identity(cls: type) -> None:
    """``from_dict(to_dict(x)) == x`` for non-default values of every field."""
    inst = _all_fields_nondefault(cls)
    again = authored_dataclass_from_json(cls, dataclass_to_jsonable(inst), path="scoring")
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
    """Serializing resolved defaults preserves their supported contract identity."""
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
            structure="racing", params={"rounds_n": 3, "nested": {"a": [1, 2, 3]}}
        ),
        overfitting=OverfittingConfig(
            enabled=False,
            ladder=LadderConfig(threshold=0.27, budget=4),
        ),
    )
    reloaded = _scoring_from_dict(scoring_to_dict(w))
    assert reloaded == w
    assert _canon(w) == _canon(reloaded)


def test_tournament_block_uses_its_declared_json_key() -> None:
    """The scoring document uses the declared tournament key."""
    snapshot = scoring_to_dict(ScoringWeights())
    assert "tournament" in snapshot
    assert "tournament_structure" not in snapshot


def test_sparse_scoring_uses_declared_defaults() -> None:
    supplied = {"pass_weight": 1.0, "promote_margin": 0.01}
    assert scoring_weights_from_dict(supplied) == ScoringWeights()


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


def test_cross_epoch_memory_default_is_explicit_and_enabling_it_changes_identity() -> None:
    canon_default = scoring_to_canon(ScoringWeights())
    assert canon_default["experimental"]["cross_epoch_memory"] is False
    explicit_default = ScoringWeights(experimental=ExperimentalConfig())
    assert _canon(explicit_default) == _canon(ScoringWeights())
    opted_in = ScoringWeights(experimental=ExperimentalConfig(cross_epoch_memory=True))
    assert scoring_to_canon(opted_in)["experimental"]["cross_epoch_memory"] is True
    assert _canon(opted_in) != _canon(ScoringWeights())
