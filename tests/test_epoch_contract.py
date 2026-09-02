"""Tests for :mod:`zicato.epoch.contract` — contract-hash canonicalization.

The contract hash must be *stable* across spurious edits (whitespace,
row reordering, equivalent number spellings) and *sensitive* to semantic
changes (a board entry's input, a scoring weight, the entrypoint, the
mutable-tree set). These tests pin both halves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import ScoringWeights
from zicato.epoch.contract import (
    ContractInputs,
    compute_contract_hash,
    evaluation_implementation_identity,
    resolve_contract_inputs,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal contract files
# ---------------------------------------------------------------------------


_BOARD_LINE_A = json.dumps(
    {
        "id": "entry_a",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "hello world",
    }
)
_BOARD_LINE_B = json.dumps(
    {
        "id": "entry_b",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "goodbye world",
    }
)

_BRIEF = "# Proposer brief\n\n## Focus\n- Be careful.\n"

_SCORING = json.dumps({"pass_weight": 1.0, "promote_margin": 0.01})


class _ContractAdapter:
    def __init__(self, marker: str, integrations: list[str] | None = None) -> None:
        self.marker = marker
        self.integrations = integrations or []

    def worker_spec(self) -> dict[str, object]:
        return {
            "kind": "import",
            "factory": "tests.test_epoch_contract:_make_contract_adapter",
            "args": [self.marker, self.integrations],
            "integrations": list(self.integrations),
        }


def _make_contract_adapter(
    marker: str,
    integrations: list[str] | None = None,
) -> _ContractAdapter:
    return _ContractAdapter(marker, integrations)


def _write_contract(
    tmp_path: Path,
    *,
    board: str = _BOARD_LINE_A + "\n" + _BOARD_LINE_B + "\n",
    brief: str = _BRIEF,
    scoring: str = _SCORING,
) -> ContractInputs:
    board_path = tmp_path / "board.jsonl"
    brief_path = tmp_path / "brief.md"
    scoring_path = tmp_path / "scoring.json"
    board_path.write_text(board)
    brief_path.write_text(brief)
    scoring_path.write_text(scoring)
    return ContractInputs(
        board_path=board_path,
        brief_path=brief_path,
        scoring_path=scoring_path,
        entrypoint="pkg.mod:agent",
        mutable_trees=(str(tmp_path / "agent"),),
    )


# ---------------------------------------------------------------------------
# Stability — spurious edits must NOT change the hash
# ---------------------------------------------------------------------------


def test_hash_stable_across_whitespace_only_brief_edits(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Re-write the proposer brief with CRLF line endings, trailing
    # whitespace, and extra leading/trailing blank lines.
    base.brief_path.write_text(
        "\n\n# Proposer brief   \r\n\r\n## Focus\r\n- Be careful.   \r\n\n\n"
    )
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_hash_stable_across_board_entry_reordering(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Reorder the two board rows — canonicalization sorts by id.
    base.board_path.write_text(_BOARD_LINE_B + "\n" + _BOARD_LINE_A + "\n")
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_hash_stable_across_equivalent_scoring_number_spellings(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # These JSON spellings parse to the same runtime values as _SCORING.
    base.scoring_path.write_text(
        json.dumps(
            {
                "pass_weight": 1.000000,
                "promote_margin": 0.010000,
            }
        )
    )
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_hash_changes_for_distinct_goldfive_threshold_values(tmp_path: Path) -> None:
    """Executed detector precision is preserved in the evaluation contract."""
    base = _write_contract(
        tmp_path,
        scoring=json.dumps(
            {"goldfive": {"reasoning_drift": {"off_topic_distance_threshold": 0.7000001}}}
        ),
    )
    first_hash = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"goldfive": {"reasoning_drift": {"off_topic_distance_threshold": 0.7000002}}})
    )
    assert compute_contract_hash(base) != first_hash


def test_hash_changes_when_the_zicato_evaluator_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Cached measurements cannot cross a Zicato implementation change."""
    import zicato.epoch.contract as contract

    base = _write_contract(tmp_path)
    monkeypatch.setattr(contract, "_canon_evaluator_revision", lambda: "revision-a")
    first_hash = compute_contract_hash(base)
    monkeypatch.setattr(contract, "_canon_evaluator_revision", lambda: "revision-b")
    assert compute_contract_hash(base) != first_hash


def test_goldfive_implementation_changes_only_a_goldfive_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import zicato.integrations.goldfive as goldfive

    generic = _write_contract(tmp_path, scoring=json.dumps({"telemetry_dialect": "transcript"}))
    generic_hash = compute_contract_hash(generic)
    monkeypatch.setattr(goldfive, "GOLDFIVE_IMPLEMENTATION_VERSION", "git:revision-a")
    assert compute_contract_hash(generic) == generic_hash

    generic.scoring_path.write_text(json.dumps({"goldfive": {}}))
    goldfive_hash = compute_contract_hash(generic)
    monkeypatch.setattr(goldfive, "GOLDFIVE_IMPLEMENTATION_VERSION", "version:1.2.3")
    assert compute_contract_hash(generic) != goldfive_hash


def test_evaluation_implementation_identity_is_explicit_and_capability_scoped() -> None:
    from zicato.integrations.goldfive import (
        GOLDFIVE_IMPLEMENTATION_VERSION,
        ZICATO_GOLDFIVE_INTEGRATION_REVISION,
    )

    assert evaluation_implementation_identity(ScoringWeights()) == {
        "zicato_evaluator_revision": 1,
    }
    assert evaluation_implementation_identity(ScoringWeights(goldfive={})) == {
        "zicato_evaluator_revision": 1,
        "goldfive_version": GOLDFIVE_IMPLEMENTATION_VERSION,
        "zicato_goldfive_integration_revision": ZICATO_GOLDFIVE_INTEGRATION_REVISION,
    }


# ---------------------------------------------------------------------------
# Sensitivity — semantic changes MUST change the hash
# ---------------------------------------------------------------------------


def test_hash_changes_on_board_entry_input_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    edited = json.dumps(
        {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello CHANGED world",
        }
    )
    base.board_path.write_text(edited + "\n" + _BOARD_LINE_B + "\n")
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_scoring_weight_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    base.scoring_path.write_text(json.dumps({"pass_weight": 2.0, "promote_margin": 0.01}))
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_entrypoint_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    from dataclasses import replace

    moved = replace(base, entrypoint="pkg.mod:OTHER_agent")
    h2 = compute_contract_hash(moved)
    assert h1 != h2


def test_adapter_integration_order_does_not_change_hash(tmp_path: Path) -> None:
    from dataclasses import replace

    base = _write_contract(tmp_path)
    one = replace(
        base,
        adapter_spec={"kind": "import", "factory": "pkg.mod:factory", "integrations": ["b", "a"]},
        adapter_source_specs=("pkg.mod:factory",),
    )
    two = replace(
        one,
        adapter_spec={"kind": "import", "factory": "pkg.mod:factory", "integrations": ["a", "b"]},
    )
    assert compute_contract_hash(one) == compute_contract_hash(two)


def test_entrypoint_source_inside_a_mutable_tree_is_generation_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys
    from dataclasses import replace

    package = tmp_path / "agentpkg"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text('AGENT = "one"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    base = _write_contract(tmp_path)
    contract = replace(
        base,
        entrypoint="agentpkg:AGENT",
        mutable_trees=(str(package),),
        adapter_spec={
            "kind": "adk",
            "entrypoint": "agentpkg:AGENT",
            "mutable_trees": [str(package)],
            "integrations": ["goldfive"],
        },
        adapter_source_specs=("agentpkg:AGENT",),
    )
    first = compute_contract_hash(contract)
    source.write_text('AGENT = "two"\n')
    sys.modules.pop("agentpkg", None)
    importlib.invalidate_caches()
    assert compute_contract_hash(contract) == first


def test_external_adapter_factory_source_is_contract_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys
    from dataclasses import replace

    driver = tmp_path / "driver.py"
    driver.write_text("def make():\n    return 'one'\n")
    mutable = tmp_path / "mutable_agent"
    mutable.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    base = _write_contract(tmp_path)
    contract = replace(
        base,
        mutable_trees=(str(mutable),),
        adapter_spec={"kind": "import", "factory": "driver:make"},
        adapter_source_specs=("driver:make",),
    )
    first = compute_contract_hash(contract)
    driver.write_text("def make():\n    return 'two'\n")
    sys.modules.pop("driver", None)
    importlib.invalidate_caches()
    assert compute_contract_hash(contract) != first


def test_hash_changes_on_adding_a_mutable_tree(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    from dataclasses import replace

    expanded = replace(
        base,
        mutable_trees=base.mutable_trees + (str(tmp_path / "extra_agent"),),
    )
    h2 = compute_contract_hash(expanded)
    assert h1 != h2


def test_hash_stable_across_mutable_tree_reordering(tmp_path: Path) -> None:
    from dataclasses import replace

    base = _write_contract(tmp_path)
    two = replace(
        base,
        mutable_trees=(str(tmp_path / "a"), str(tmp_path / "b")),
    )
    h1 = compute_contract_hash(two)
    swapped = replace(
        base,
        mutable_trees=(str(tmp_path / "b"), str(tmp_path / "a")),
    )
    h2 = compute_contract_hash(swapped)
    assert h1 == h2


# ---------------------------------------------------------------------------
# Anti-overfitting config folds into the scoring contract (OVERFITTING.md
# §12 #1/#3): changing any knob in the ``overfitting`` block — or the
# one-time default-on rollout — must roll the epoch, exactly as retuning a
# scoring weight does. An absent block is the default-on config, so a
# scoring.json that never mentions it hashes identically to one that spells
# the defaults out.
# ---------------------------------------------------------------------------


def test_hash_changes_on_overfitting_knob_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Flip the master switch off — a different evaluation contract.
    base.scoring_path.write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "promote_margin": 0.01,
                "overfitting": {"enabled": False},
            }
        )
    )
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_holdout_fraction_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"overfitting": {"holdout_fraction": 0.3}}))
    h1 = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"overfitting": {"holdout_fraction": 0.4}}))
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_restrict_visibility_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(
        json.dumps({"overfitting": {"restrict_proposer_visibility": True}})
    )
    h1 = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"overfitting": {"restrict_proposer_visibility": False}})
    )
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_outcome_summarizer_spec_edit(tmp_path: Path) -> None:
    # The optional operator outcome-summarizer hook (issue #18 cap 2, item 8)
    # is a plain ScoringWeights field, so configuring or changing the dotted
    # spec folds into the scoring canon and rolls the epoch — exactly like
    # retuning any other contract field. The empty-string default (no
    # summarizer) is byte-identical to today.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({}))
    h_default = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"outcome_summarizer_spec": "pkg.mod:summarize_a"}))
    h1 = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"outcome_summarizer_spec": "pkg.mod:summarize_b"}))
    h2 = compute_contract_hash(base)
    # Configuring a spec rolls off the default, and changing it rolls again.
    assert h_default != h1
    assert h1 != h2


def test_hash_stable_when_outcome_summarizer_spec_omitted(tmp_path: Path) -> None:
    # A contract that does not mention the new field hashes identically to one
    # that spells out its empty-string default — no spurious roll for existing
    # contracts that predate the field.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0, "outcome_summarizer_spec": ""}))
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_stable_when_screening_fields_at_default(tmp_path: Path) -> None:
    # Candidate screening (screen_entries / screen_veto_only on the nested
    # proposer_quality block) is omit-at-default like random_baseline_every_n:
    # a contract that predates the fields hashes byte-identically to one that
    # spells out the OFF defaults — no retroactive roll for existing epochs.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "proposer_quality": {"screen_entries": 0, "screen_veto_only": False},
            }
        )
    )
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_changes_when_screening_opted_in(tmp_path: Path) -> None:
    # Opting into screening selects champions under a different rule (a
    # vetoed candidate never reaches the tournament), so a non-zero
    # screen_entries — or flipping screen_veto_only — rolls the epoch,
    # exactly like retuning any other contract weight.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_default = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"screen_entries": 2}})
    )
    h_on = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "proposer_quality": {"screen_entries": 2, "screen_veto_only": True},
            }
        )
    )
    h_veto_only = compute_contract_hash(base)
    assert h_default != h_on
    assert h_on != h_veto_only


def test_hash_stable_when_recombine_at_default(tmp_path: Path) -> None:
    # The recombination slot (WS-REC) is omit-at-default like the screen
    # knobs: a contract that predates the field hashes byte-identically to
    # one that spells out the False default — no retroactive roll.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"recombine": False}})
    )
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_changes_when_recombine_opted_in(tmp_path: Path) -> None:
    # Opting into recombination selects champions under a different rule (a
    # minted union can be chosen without a critic pass), so recombine: true
    # rolls the epoch exactly like retuning any other contract weight.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_default = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"recombine": True}})
    )
    h_on = compute_contract_hash(base)
    assert h_default != h_on


def test_hash_stable_when_mutation_surface_is_empty(tmp_path: Path) -> None:
    # The declared mutation-site syntax table (issue #168) is omit-at-default:
    # a contract that predates the field hashes byte-identically to one that
    # spells out the empty table, so widening the ENVELOPE mechanism rolls no
    # existing epoch.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0, "mutation_surface": {}}))
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_changes_when_a_file_type_is_declared(tmp_path: Path) -> None:
    # Declaring a file type widens what the proposer may rewrite — the surface,
    # hence comparability — so it rolls the epoch like any contract edit. The
    # LEADERS are part of it too: they decide what the applier can strip out of
    # a region body, so retuning them alone must move the hash.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_none = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "mutation_surface": {".ts": {"leaders": ["//"]}}})
    )
    h_ts = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "mutation_surface": {".ts": {"leaders": ["//", "/*"]}}})
    )
    h_ts_two_leaders = compute_contract_hash(base)
    assert h_none != h_ts
    assert h_ts != h_ts_two_leaders


def test_hash_stable_when_genealogy_at_default(tmp_path: Path) -> None:
    # The genealogy channel (WS-GENE) is omit-at-default like the screen /
    # exemplar knobs: a contract that predates the field hashes byte-identically
    # to one that spells out the 0 default — no retroactive roll.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"genealogy": 0}})
    )
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_changes_when_genealogy_opted_in(tmp_path: Path) -> None:
    # A proposer shown candidate genealogy proposes under a different rule, so
    # a non-zero genealogy count rolls the epoch like any other contract weight.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_default = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"genealogy": 4}})
    )
    h_on = compute_contract_hash(base)
    assert h_default != h_on


def test_hash_stable_when_calibration_feedback_at_default(tmp_path: Path) -> None:
    # The critic-calibration channel (WS-CAL) is omit-at-default like the
    # genealogy / screen / exemplar knobs: a contract that predates the field
    # hashes byte-identically to one that spells out the 0 default.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_omitted = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"calibration_feedback": 0}})
    )
    h_explicit_default = compute_contract_hash(base)
    assert h_omitted == h_explicit_default


def test_hash_changes_when_calibration_feedback_opted_in(tmp_path: Path) -> None:
    # A proposer shown its own prediction calibration proposes under a different
    # rule, so a non-zero calibration_feedback rolls the epoch like any weight.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0}))
    h_default = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps({"pass_weight": 1.0, "proposer_quality": {"calibration_feedback": 5}})
    )
    h_on = compute_contract_hash(base)
    assert h_default != h_on


def test_hash_changes_on_ladder_knob_edit(tmp_path: Path) -> None:
    # The Ladder sub-config (OVERFITTING.md §12 #2) folds into the scoring
    # contract through OverfittingConfig — bumping a Ladder knob rolls the
    # epoch, exactly as retuning promote_margin does.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"overfitting": {"ladder": {"budget": 16}}}))
    h1 = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"overfitting": {"ladder": {"budget": 32}}}))
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_ladder_disable(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h_on = compute_contract_hash(base)  # default-on ladder
    base.scoring_path.write_text(json.dumps({"overfitting": {"ladder": {"enabled": False}}}))
    h_off = compute_contract_hash(base)
    assert h_on != h_off


def test_hash_changes_on_rotate_holdout_edit(tmp_path: Path) -> None:
    # The ``rotate_holdout`` flag (OVERFITTING.md §12 #6) folds into the
    # contract: flipping it selects champions under a different holdout-
    # derivation discipline, so it rolls the epoch — even though the
    # rotation itself never touches the board's contract hash.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"overfitting": {"rotate_holdout": True}}))
    h_on = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"overfitting": {"rotate_holdout": False}}))
    h_off = compute_contract_hash(base)
    assert h_on != h_off


def test_hash_changes_on_max_generations_per_contract_edit(tmp_path: Path) -> None:
    # The cadence ceiling (OVERFITTING.md §12 #6) folds into the contract.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(
        json.dumps({"overfitting": {"max_generations_per_contract": None}})
    )
    h_none = compute_contract_hash(base)
    base.scoring_path.write_text(json.dumps({"overfitting": {"max_generations_per_contract": 20}}))
    h_set = compute_contract_hash(base)
    assert h_none != h_set


def test_absent_ladder_block_hashes_as_the_defaults(tmp_path: Path) -> None:
    # A scoring.json that never mentions ``overfitting.ladder`` must hash like
    # one that spells the default-on Ladder config out in full.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"overfitting": {"holdout_fraction": 0.3}}))
    h_absent = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps(
            {
                "overfitting": {
                    "holdout_fraction": 0.3,
                    "ladder": {
                        "enabled": True,
                        "threshold": None,
                        "budget": 16,
                        "noise_scale": 0.0,
                    },
                }
            }
        )
    )
    h_spelled = compute_contract_hash(base)
    assert h_absent == h_spelled


def test_absent_overfitting_block_hashes_as_the_defaults(tmp_path: Path) -> None:
    # A scoring.json that never mentions ``overfitting`` must hash exactly
    # like one that spells the default-on config out in full — the
    # canonicalizer routes both through the same fully-defaulted
    # OverfittingConfig.
    base = _write_contract(tmp_path)
    base.scoring_path.write_text(json.dumps({"pass_weight": 1.0, "promote_margin": 0.01}))
    h_absent = compute_contract_hash(base)
    base.scoring_path.write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "promote_margin": 0.01,
                "overfitting": {
                    "enabled": True,
                    "holdout_fraction": 0.3,
                    "min_board_size_for_split": 6,
                    "restrict_proposer_visibility": True,
                },
            }
        )
    )
    h_spelled = compute_contract_hash(base)
    assert h_absent == h_spelled


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


def test_missing_files_hash_deterministically(tmp_path: Path) -> None:
    """A workspace with no contract files still hashes the same twice."""
    inputs = ContractInputs(
        board_path=tmp_path / "nope_board.jsonl",
        brief_path=tmp_path / "nope_brief.md",
        scoring_path=tmp_path / "nope_scoring.json",
        entrypoint="",
        mutable_trees=(),
    )
    h1 = compute_contract_hash(inputs)
    h2 = compute_contract_hash(inputs)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64


def test_missing_board_differs_from_present_board(tmp_path: Path) -> None:
    """A missing board hashes differently than a populated one."""
    base = _write_contract(tmp_path)
    h_present = compute_contract_hash(base)

    from dataclasses import replace

    h_missing = compute_contract_hash(replace(base, board_path=tmp_path / "absent.jsonl"))
    assert h_present != h_missing


# ---------------------------------------------------------------------------
# Board-level judges + disable_drift in the canonical board form
#
# The board carries two pieces of contract beyond its entry rows. These
# tests pin the canonicalization of those board-level fields directly —
# the entry-row format is owned by zicato.board and reconciled at
# integration time, so the helpers are exercised as units here.
# ---------------------------------------------------------------------------


def test_canon_board_meta_stable_for_board_without_meta(tmp_path: Path) -> None:
    """An entry-only board canonicalizes to a stable empty-meta form."""
    from zicato.epoch.contract import _canon_board_meta

    board = tmp_path / "board.jsonl"
    board.write_text(_BOARD_LINE_A + "\n")
    meta = _canon_board_meta(board)
    # Empty-meta form: no judges, drift not disabled.
    assert json.loads(meta) == {"judges": [], "disable_drift": False}


def test_canon_board_meta_picks_up_judges_and_disable_drift(tmp_path: Path) -> None:
    """A board-level metadata object feeds judges + disable_drift into the form.

    The board-level metadata is a JSON object carrying ``judges`` /
    ``disable_drift`` but no entry ``id``; it is scanned out of the raw
    board file. The exact on-disk placement of that object alongside
    entry rows is owned by ``zicato.board`` and reconciled at
    integration time.
    """
    from zicato.epoch.contract import _canon_board_meta

    board = tmp_path / "board.jsonl"
    board.write_text(
        json.dumps({"judges": [{"name": "j1"}], "disable_drift": ["tool_error"]}) + "\n"
    )
    meta = json.loads(_canon_board_meta(board))
    assert meta["disable_drift"] == ["tool_error"]
    assert meta["judges"] == [{"name": "j1"}]


# ---------------------------------------------------------------------------
# disable_drift is a LIST of drift kinds, and the contract hash must see
# WHICH kinds are disabled — each disabled kind disarms a built-in judge
# and so moves the loss surface.
# ---------------------------------------------------------------------------


def _board_with_disable_drift(kinds: list[str]) -> str:
    """A two-entry board whose board_meta header disables ``kinds``."""
    header = json.dumps({"board_meta": True, "disable_drift": kinds})
    return header + "\n" + _BOARD_LINE_A + "\n" + _BOARD_LINE_B + "\n"


def test_canon_board_meta_keeps_the_sorted_kind_list(tmp_path: Path) -> None:
    """The canonical form carries the sorted kind list, not a bare flag."""
    from zicato.epoch.contract import _canon_board_meta

    board = tmp_path / "board.jsonl"
    board.write_text(_board_with_disable_drift(["tool_error", "agent_refusal"]))
    meta = json.loads(_canon_board_meta(board))
    assert meta["disable_drift"] == ["agent_refusal", "tool_error"]


def test_hash_changes_when_a_different_drift_kind_is_disabled(tmp_path: Path) -> None:
    """Two boards differing ONLY in WHICH kinds are disabled hash differently.

    This is the point of folding board meta into the contract: disabling
    ``tool_error`` and disabling ``goal_drift`` arm different built-in
    judges, so the loss surface differs and the epoch must roll.
    """
    base = _write_contract(tmp_path, board=_board_with_disable_drift(["tool_error"]))
    h1 = compute_contract_hash(base)

    base.board_path.write_text(_board_with_disable_drift(["goal_drift"]))
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_stable_across_disable_drift_ordering(tmp_path: Path) -> None:
    """``disable_drift`` is a SET: declaration order must not move the hash."""
    base = _write_contract(
        tmp_path, board=_board_with_disable_drift(["tool_error", "agent_refusal"])
    )
    h1 = compute_contract_hash(base)

    base.board_path.write_text(_board_with_disable_drift(["agent_refusal", "tool_error"]))
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_empty_disable_drift_hashes_like_a_board_with_no_header(tmp_path: Path) -> None:
    """Header-absent, empty-list, and the historic form all agree.

    The omit-at-default discipline: a board that disables nothing must
    hash byte-for-byte as it did before ``disable_drift`` became a kind
    list, so no existing workspace rolls its epoch for this change.
    """
    from zicato.epoch.contract import _canon_board_meta

    absent = tmp_path / "absent.jsonl"
    absent.write_text(_BOARD_LINE_A + "\n")

    empty = tmp_path / "empty.jsonl"
    empty.write_text(
        json.dumps({"board_meta": True, "disable_drift": []}) + "\n" + _BOARD_LINE_A + "\n"
    )

    assert _canon_board_meta(absent) == _canon_board_meta(empty)
    # The historic canonical bytes, pinned literally.
    assert json.loads(_canon_board_meta(absent)) == {"judges": [], "disable_drift": False}


def test_canon_judges_is_order_independent() -> None:
    """Judge declaration order does not move the canonical judges form."""
    from zicato.epoch.contract import _canon_judges

    a = _canon_judges([{"name": "alpha"}, {"name": "beta"}])
    b = _canon_judges([{"name": "beta"}, {"name": "alpha"}])
    assert a == b


def test_canon_judges_sensitive_to_judge_change() -> None:
    """Editing a judge changes the canonical judges form."""
    from zicato.epoch.contract import _canon_judges

    a = _canon_judges([{"name": "alpha", "model": "m1"}])
    b = _canon_judges([{"name": "alpha", "model": "m2"}])
    assert a != b


def test_canon_board_folds_meta_into_canonical_form(tmp_path: Path) -> None:
    """The board-level metadata participates in the full canonical board.

    A normal entry-only board's canonical form carries the board-meta
    line, so a future change to ``judges`` / ``disable_drift`` flows into
    the contract hash and rolls the epoch.
    """
    from zicato.epoch.contract import _canon_board

    board = tmp_path / "board.jsonl"
    board.write_text(_BOARD_LINE_A + "\n")
    canon = _canon_board(board)
    # The board-meta line is prepended with a NUL-marked prefix; it must
    # be present so board-level contract changes participate in the hash.
    assert "\x00board-meta\x00" in canon
    # The entry row is still part of the canonical form.
    assert "entry_a" in canon


# ---------------------------------------------------------------------------
# resolve_contract_inputs
# ---------------------------------------------------------------------------


def test_resolve_contract_inputs_reads_config(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": ["/abs/agent"],
                "contract": {
                    "board_path": "/abs/board.jsonl",
                    "brief_path": "/abs/brief.md",
                    "scoring_path": "/abs/scoring.json",
                },
            }
        )
    )
    inputs = resolve_contract_inputs(workspace)
    assert inputs.entrypoint == "pkg.mod:agent"
    assert inputs.mutable_trees == ("/abs/agent",)
    assert inputs.board_path == Path("/abs/board.jsonl")
    assert inputs.brief_path == Path("/abs/brief.md")


def test_resolve_contract_inputs_reads_nested_adk_adapter(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adapter": {
                    "kind": "adk",
                    "entrypoint": "pkg.mod:agent",
                    "mutable_trees": ["/abs/agent"],
                }
            }
        )
    )
    inputs = resolve_contract_inputs(workspace)
    assert inputs.entrypoint == "pkg.mod:agent"
    assert inputs.mutable_trees == ("/abs/agent",)
    assert inputs.adapter_spec == {
        "kind": "adk",
        "entrypoint": "pkg.mod:agent",
        "mutable_trees": ["/abs/agent"],
        "integrations": ["goldfive"],
    }


def test_generic_adapter_factory_args_and_integrations_are_contract_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    files = _write_contract(tmp_path)

    def write_config(marker: str, integrations: list[str]) -> None:
        (workspace / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {
                        "kind": "import",
                        "factory": "tests.test_epoch_contract:_make_contract_adapter",
                        "args": [marker, integrations],
                    },
                    "contract": {
                        "board_path": str(files.board_path),
                        "brief_path": str(files.brief_path),
                        "scoring_path": str(files.scoring_path),
                    },
                }
            )
        )

    write_config("one", [])
    initial = resolve_contract_inputs(workspace)
    first_hash = compute_contract_hash(initial)
    assert initial.adapter_source_specs == ("tests.test_epoch_contract:_make_contract_adapter",)

    write_config("two", [])
    assert compute_contract_hash(resolve_contract_inputs(workspace)) != first_hash
    write_config("one", ["goldfive"])
    assert compute_contract_hash(resolve_contract_inputs(workspace)) != first_hash


def test_resolve_contract_inputs_accepts_legacy_rubric_path_key(
    tmp_path: Path,
) -> None:
    """A workspace registered before the rename stored ``rubric_path``.

    ``resolve_contract_inputs`` still resolves the proposer brief from
    the legacy ``contract.rubric_path`` key.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": [],
                "contract": {
                    "board_path": "/abs/board.jsonl",
                    "rubric_path": "/abs/legacy.md",
                    "scoring_path": "/abs/scoring.json",
                },
            }
        )
    )
    inputs = resolve_contract_inputs(workspace)
    assert inputs.brief_path == Path("/abs/legacy.md")


def test_resolve_contract_inputs_raises_without_config(tmp_path: Path) -> None:
    import pytest

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    with pytest.raises(FileNotFoundError, match="zicato epoch register"):
        resolve_contract_inputs(workspace)


def test_resolve_contract_inputs_defaults_when_no_contract_key(
    tmp_path: Path,
) -> None:
    """A workspace registered before auto-epoching uses the default paths."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"adk_entrypoint": "pkg.mod:agent", "mutable_trees": []})
    )
    inputs = resolve_contract_inputs(workspace)
    # Defaults sit next to the workspace dir (the operator's project root).
    assert inputs.board_path == (tmp_path / "board.jsonl").resolve()
    assert inputs.brief_path == (tmp_path / "brief.md").resolve()


def test_resolve_contract_inputs_default_brief_falls_back_to_legacy_file(
    tmp_path: Path,
) -> None:
    """When no ``brief.md`` exists but a legacy ``rubric.md`` does, use it.

    Workspaces created before the rename keep an operator-side
    ``rubric.md``; the default resolver prefers it over a non-existent
    ``brief.md`` so those workspaces resolve without a file rename.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"adk_entrypoint": "pkg.mod:agent", "mutable_trees": []})
    )
    (tmp_path / "rubric.md").write_text("# legacy brief\n")
    inputs = resolve_contract_inputs(workspace)
    assert inputs.brief_path == (tmp_path / "rubric.md").resolve()


# ---------------------------------------------------------------------------
# Proposer component
#
# The proposer — its agent identity, tools, and skill modules — is the
# sixth contract component. A semantic skill edit (or adding / removing /
# renaming a skill, or editing a custom agent.py) rolls the epoch; a
# whitespace-only skill edit or a filesystem-reorder does not.
# ---------------------------------------------------------------------------


_SKILL_A = "---\nname: tighten\ndescription: keep it terse\n---\n\nPrefer terse patches.\n"
_SKILL_B = "---\nname: bold\ndescription: be bold\n---\n\nFavor bold rewrites.\n"


def _make_proposer(tmp_path: Path, *, skills: dict[str, str], agent: str | None = None) -> Path:
    """Create a ``proposers/<name>/`` dir with the given skills + agent."""
    proposer = tmp_path / "proposers" / "p1"
    skills_dir = proposer / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in skills.items():
        (skills_dir / filename).write_text(text)
    if agent is not None:
        (proposer / "agent.py").write_text(agent)
    return proposer


def test_proposer_skill_body_edit_changes_hash(tmp_path: Path) -> None:
    """A semantic edit to a skill body rolls the contract hash."""
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    (proposer / "skills" / "a.md").write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\nPrefer VERY terse patches.\n"
    )
    h2 = compute_contract_hash(with_proposer)
    assert h1 != h2


def test_proposer_skill_edit_names_proposer_component(tmp_path: Path) -> None:
    """Component drift on a skill edit is attributed to ``proposer``."""
    from dataclasses import replace

    from zicato.epoch.contract import compute_component_hashes

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A})
    with_proposer = replace(base, proposer_path=proposer)
    before = compute_component_hashes(with_proposer)

    (proposer / "skills" / "a.md").write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\nA materially different skill.\n"
    )
    after = compute_component_hashes(with_proposer)

    changed = [k for k in before if before[k] != after[k]]
    assert changed == ["proposer"]


def test_proposer_adding_a_skill_changes_hash(tmp_path: Path) -> None:
    """Adding a skill file rolls the hash."""
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    (proposer / "skills" / "b.md").write_text(_SKILL_B)
    h2 = compute_contract_hash(with_proposer)
    assert h1 != h2


def test_proposer_removing_a_skill_changes_hash(tmp_path: Path) -> None:
    """Removing a skill file rolls the hash."""
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A, "b.md": _SKILL_B})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    (proposer / "skills" / "b.md").unlink()
    h2 = compute_contract_hash(with_proposer)
    assert h1 != h2


def test_proposer_renaming_a_skill_changes_hash(tmp_path: Path) -> None:
    """Renaming a skill file (its ``name`` frontmatter) rolls the hash."""
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    (proposer / "skills" / "a.md").write_text(
        "---\nname: RENAMED\ndescription: keep it terse\n---\n\nPrefer terse patches.\n"
    )
    h2 = compute_contract_hash(with_proposer)
    assert h1 != h2


def test_proposer_whitespace_only_skill_edit_is_stable(tmp_path: Path) -> None:
    """Whitespace / line-ending-only skill edits do not roll the hash."""
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    # Same skill body re-spelled with CRLF endings, trailing spaces, and
    # extra leading / trailing blank lines.
    (proposer / "skills" / "a.md").write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\n\nPrefer terse patches.   \r\n\n\n"
    )
    h2 = compute_contract_hash(with_proposer)
    assert h1 == h2


def test_proposer_hash_stable_across_filesystem_reorder(tmp_path: Path) -> None:
    """Re-touching files (mtime reorder) leaves the hash unchanged.

    Skills are discovered sorted by filename, so the order the filesystem
    happens to enumerate / the files' mtimes do not move the hash.
    """
    import os
    from dataclasses import replace

    base = _write_contract(tmp_path)
    proposer = _make_proposer(tmp_path, skills={"a.md": _SKILL_A, "b.md": _SKILL_B})
    with_proposer = replace(base, proposer_path=proposer)
    h1 = compute_contract_hash(with_proposer)

    # Bump mtimes in the reverse of name order; the loader still sorts by
    # name so the canonical form is identical.
    os.utime(proposer / "skills" / "b.md", (1, 1))
    os.utime(proposer / "skills" / "a.md", (2, 2))
    h2 = compute_contract_hash(with_proposer)
    assert h1 == h2


def test_proposer_builtin_default_is_stable(tmp_path: Path) -> None:
    """The built-in default (``proposer_path=None``) hashes deterministically."""
    base = _write_contract(tmp_path)  # proposer_path defaults to None
    h1 = compute_contract_hash(base)
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_proposer_builtin_differs_from_empty_dir(tmp_path: Path) -> None:
    """An empty proposer dir is NOT the builtin — agent_id differs.

    The builtin's ``agent_id`` is ``"builtin:default"`` while a configured
    dir is ``"dir:<name>"`` even with no skills and no agent.py, so the two
    canonicalize differently.
    """
    from dataclasses import replace

    base = _write_contract(tmp_path)  # builtin (None)
    h_builtin = compute_contract_hash(base)

    empty = tmp_path / "proposers" / "p1"
    (empty / "skills").mkdir(parents=True)
    h_empty_dir = compute_contract_hash(replace(base, proposer_path=empty))
    assert h_builtin != h_empty_dir


def test_resolve_contract_inputs_reads_proposer_path(tmp_path: Path) -> None:
    """``contract.proposer_path`` is resolved; relative spellings absolutise."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": [],
                "contract": {
                    "board_path": "/abs/board.jsonl",
                    "brief_path": "/abs/brief.md",
                    "scoring_path": "/abs/scoring.json",
                    "proposer_path": "proposers/p1",
                },
            }
        )
    )
    inputs = resolve_contract_inputs(workspace)
    # Relative to the workspace's parent (the operator's project root).
    assert inputs.proposer_path == (tmp_path / "proposers" / "p1").resolve()


def test_resolve_contract_inputs_proposer_path_absent_is_none(tmp_path: Path) -> None:
    """No ``contract.proposer_path`` ⇒ the built-in default proposer (None)."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"adk_entrypoint": "pkg.mod:agent", "mutable_trees": []})
    )
    inputs = resolve_contract_inputs(workspace)
    assert inputs.proposer_path is None


def test_contract_hash_is_cwd_and_checkout_invariant(tmp_path, monkeypatch):
    """The hash must identify the CONTRACT, not the checkout.

    Registration-relative mutable trees previously resolved against the
    process cwd, folding the absolute checkout path into the hash — the
    same workspace hashed differently run from a different directory (or
    after being moved) and spuriously rolled its epoch.
    """
    from zicato.epoch.contract import ContractInputs, compute_contract_hash

    board = tmp_path / "board.jsonl"
    board.write_text("", encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("goal", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text("{}", encoding="utf-8")

    def compute_from(cwd):
        monkeypatch.chdir(cwd)
        return compute_contract_hash(
            ContractInputs(
                board_path=board,
                brief_path=brief,
                scoring_path=scoring,
                entrypoint="pkg.mod:agent",
                mutable_trees=("agent", "./skills/../skills"),
            )
        )

    other = tmp_path / "elsewhere"
    other.mkdir()
    assert compute_from(tmp_path) == compute_from(other)

    # Normalization unifies ./ and ../ spellings; ordering is irrelevant.
    monkeypatch.chdir(tmp_path)
    base = ContractInputs(
        board_path=board,
        brief_path=brief,
        scoring_path=scoring,
        entrypoint="pkg.mod:agent",
        mutable_trees=("agent", "skills"),
    )
    spelled = ContractInputs(
        board_path=board,
        brief_path=brief,
        scoring_path=scoring,
        entrypoint="pkg.mod:agent",
        mutable_trees=("./skills", "agent/"),
    )
    assert compute_contract_hash(base) == compute_contract_hash(spelled)
