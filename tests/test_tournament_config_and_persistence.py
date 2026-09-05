"""Config parsing, contract-hash, and back-compat persistence tests.

Covers the data-model half of the configurable-tournament-structures
feature: the ``tournament`` block in ``scoring.json``, its fold into the
contract hash, and the back-compat loading of gauntlet-era persisted
records (ActiveTournament, OutcomeRecord/journal).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import OutcomeRecord, ScoringWeights, TournamentStructure
from zicato.epoch.contract import ContractInputs, compute_contract_hash
from zicato.epoch.journal import _outcome_from_dict
from zicato.runtime.state import ActiveTournament, ActiveTournamentEntry
from zicato.workspace_loader import (
    overfitting_config_from_dict,
    overfitting_config_to_dict,
    scoring_weights_from_dict,
    tournament_structure_from_dict,
    tournament_structure_to_dict,
)

# ---------------------------------------------------------------------------
# Config parsing + validation
# ---------------------------------------------------------------------------


def test_absent_tournament_block_defaults_to_gauntlet() -> None:
    spec = tournament_structure_from_dict(None)
    assert spec.structure == "gauntlet"
    assert spec.params == {}


def test_scoring_without_tournament_key_is_gauntlet() -> None:
    w = scoring_weights_from_dict({"pass_weight": 2.0})
    assert w.tournament_structure.structure == "gauntlet"


def test_scoring_parses_swiss_block_with_params() -> None:
    w = scoring_weights_from_dict(
        {
            "tournament": {"structure": "swiss", "params": {"rounds_n": 6}},
            "experimental": {"tournament_structures": True},
        }
    )
    assert w.tournament_structure.structure == "swiss"
    assert w.tournament_structure.params["rounds_n"] == 6


def test_invalid_structure_token_rejected_listing_valid() -> None:
    with pytest.raises(ValueError) as exc:
        tournament_structure_from_dict({"structure": "bogus"})
    msg = str(exc.value)
    assert "single_elim" in msg and "racing" in msg


def test_non_mapping_params_rejected() -> None:
    with pytest.raises(ValueError):
        TournamentStructure(structure="gauntlet", params=[1, 2, 3])  # type: ignore[arg-type]


def test_tournament_block_round_trips() -> None:
    spec = TournamentStructure(structure="racing", params={"eta": 3, "board_fraction": 0.5})
    again = tournament_structure_from_dict(tournament_structure_to_dict(spec))
    assert again.structure == "racing"
    assert again.params == {"eta": 3, "board_fraction": 0.5}


# ---------------------------------------------------------------------------
# Anti-overfitting config parsing + round-trip (OVERFITTING.md §12 #1/#3)
# ---------------------------------------------------------------------------


def test_absent_overfitting_block_is_default_on() -> None:
    cfg = overfitting_config_from_dict(None)
    assert cfg.enabled is True
    assert cfg.holdout_fraction == 0.3
    # The noise-aware defaults flip lowered the split floor from 8 to 6.
    assert cfg.min_board_size_for_split == 6
    assert cfg.restrict_proposer_visibility is True


def test_scoring_without_overfitting_key_is_default_on() -> None:
    w = scoring_weights_from_dict({"pass_weight": 2.0})
    assert w.overfitting.enabled is True
    assert w.overfitting.restrict_proposer_visibility is True


def test_scoring_parses_overfitting_block() -> None:
    w = scoring_weights_from_dict(
        {
            "overfitting": {
                "enabled": False,
                "holdout_fraction": 0.25,
                "min_board_size_for_split": 12,
                "restrict_proposer_visibility": False,
            }
        }
    )
    o = w.overfitting
    assert o.enabled is False
    assert o.holdout_fraction == 0.25
    assert o.min_board_size_for_split == 12
    assert o.restrict_proposer_visibility is False


def test_overfitting_block_round_trips() -> None:
    from zicato.core.types import OverfittingConfig

    cfg = OverfittingConfig(
        enabled=False,
        holdout_fraction=0.4,
        min_board_size_for_split=10,
        restrict_proposer_visibility=False,
        rotate_holdout=False,
        max_generations_per_contract=25,
    )
    again = overfitting_config_from_dict(overfitting_config_to_dict(cfg))
    assert again == cfg


def test_scoring_parses_rotation_and_cadence_knobs() -> None:
    # The §12 #6 knobs (rotate_holdout / max_generations_per_contract) parse
    # and default safely (rotation on, no ceiling) when absent.
    o = scoring_weights_from_dict(
        {"overfitting": {"rotate_holdout": False, "max_generations_per_contract": 30}}
    ).overfitting
    assert o.rotate_holdout is False
    assert o.max_generations_per_contract == 30

    default = scoring_weights_from_dict({"overfitting": {"holdout_fraction": 0.3}}).overfitting
    assert default.rotate_holdout is True
    assert default.max_generations_per_contract is None


def test_absent_ladder_block_is_default_on() -> None:
    cfg = overfitting_config_from_dict({"holdout_fraction": 0.3})
    assert cfg.ladder.enabled is True
    assert cfg.ladder.threshold is None
    assert cfg.ladder.budget == 16
    assert cfg.ladder.noise_scale == 0.0


def test_scoring_parses_ladder_block() -> None:
    w = scoring_weights_from_dict(
        {
            "overfitting": {
                "ladder": {
                    "enabled": False,
                    "threshold": 0.05,
                    "budget": 4,
                    "noise_scale": 0.02,
                }
            }
        }
    )
    lad = w.overfitting.ladder
    assert lad.enabled is False
    assert lad.threshold == 0.05
    assert lad.budget == 4
    assert lad.noise_scale == 0.02


def test_ladder_threshold_null_round_trips_as_none() -> None:
    from zicato.core.types import LadderConfig, OverfittingConfig

    cfg = OverfittingConfig(ladder=LadderConfig(threshold=None, budget=8))
    again = overfitting_config_from_dict(overfitting_config_to_dict(cfg))
    assert again.ladder.threshold is None
    assert again == cfg


def test_overfitting_block_round_trips_with_ladder() -> None:
    from zicato.core.types import LadderConfig, OverfittingConfig

    cfg = OverfittingConfig(
        enabled=False,
        holdout_fraction=0.4,
        ladder=LadderConfig(enabled=True, threshold=0.07, budget=32, noise_scale=0.01),
    )
    again = overfitting_config_from_dict(overfitting_config_to_dict(cfg))
    assert again == cfg


def test_scoring_lifecycle_serde_preserves_overfitting() -> None:
    # The lifecycle serializer (scoring_to_dict / _scoring_from_dict) must
    # carry the overfitting block through a full round-trip so a frozen
    # epoch's config.json reloads with the same anti-overfitting contract.
    from zicato.core.types import OverfittingConfig
    from zicato.epoch.lifecycle import _scoring_from_dict, scoring_to_dict

    weights = ScoringWeights(overfitting=OverfittingConfig(enabled=False, holdout_fraction=0.45))
    again = _scoring_from_dict(scoring_to_dict(weights))
    assert again.overfitting == weights.overfitting


# ---------------------------------------------------------------------------
# Contract hash
# ---------------------------------------------------------------------------


_scoring_counter = [0]


def _write_scoring(tmp_path: Path, payload: dict) -> ContractInputs:
    # Distinct file per call so two ContractInputs in one test do not
    # alias the same scoring.json (each write would otherwise clobber the
    # previous one's content under a shared path).
    _scoring_counter[0] += 1
    scoring = tmp_path / f"scoring_{_scoring_counter[0]}.json"
    scoring.write_text(json.dumps(payload), encoding="utf-8")
    return ContractInputs(
        board_path=tmp_path / "missing_board.jsonl",
        brief_path=tmp_path / "missing_brief.md",
        scoring_path=scoring,
        entrypoint="pkg:entry",
        mutable_trees=("src",),
    )


#: The block that admits an experimental structure into a contract.
_ADMIT: dict[str, Any] = {"experimental": {"tournament_structures": True}}


def test_structure_change_moves_contract_hash(tmp_path: Path) -> None:
    gauntlet = _write_scoring(tmp_path, {"pass_weight": 1.0})
    h_gauntlet = compute_contract_hash(gauntlet)
    swiss = _write_scoring(tmp_path, {"tournament": {"structure": "swiss"}, **_ADMIT})
    h_swiss = compute_contract_hash(swiss)
    assert h_gauntlet != h_swiss


def test_param_change_moves_contract_hash(tmp_path: Path) -> None:
    four = _write_scoring(
        tmp_path, {"tournament": {"structure": "swiss", "params": {"rounds_n": 4}}, **_ADMIT}
    )
    six = _write_scoring(
        tmp_path, {"tournament": {"structure": "swiss", "params": {"rounds_n": 6}}, **_ADMIT}
    )
    assert compute_contract_hash(four) != compute_contract_hash(six)


def test_absent_block_hashes_same_as_explicit_gauntlet(tmp_path: Path) -> None:
    # An operator's partial doc (no tournament key) and an explicit
    # fully-defaulted gauntlet block must canonicalize identically — this
    # is what keeps a stored epoch hash matching a re-derived live hash.
    absent = _write_scoring(tmp_path, {"pass_weight": 1.0})
    explicit = _write_scoring(
        tmp_path,
        {"tournament": {"structure": "gauntlet", "params": {}}},
    )
    assert compute_contract_hash(absent) == compute_contract_hash(explicit)


def test_param_key_order_does_not_move_hash(tmp_path: Path) -> None:
    a = _write_scoring(
        tmp_path,
        {"tournament": {"structure": "racing", "params": {"eta": 2, "board_fraction": 0.5}}},
    )
    b = _write_scoring(
        tmp_path,
        {"tournament": {"structure": "racing", "params": {"board_fraction": 0.5, "eta": 2}}},
    )
    assert compute_contract_hash(a) == compute_contract_hash(b)


# ---------------------------------------------------------------------------
# Back-compat: ActiveTournament
# ---------------------------------------------------------------------------


def test_old_active_tournament_loads_with_gauntlet_defaults() -> None:
    # A gauntlet-era active_tournament.json (no structure envelope).
    legacy = {
        "tournament_id": "t1",
        "parent_generation_id": "v3",
        "child_generation_id": "v4",
        "epoch_id": "e3",
        "started_at": "2026-01-01T00:00:00Z",
        "phase": "running",
        "entries": [
            {"entry_id": "a", "side": "parent", "status": "completed"},
            {"entry_id": "a", "side": "child", "status": "completed"},
        ],
    }
    t = ActiveTournament.from_dict(legacy)
    assert t.structure == "gauntlet"
    assert t.competitors == []
    assert t.rounds == []
    assert t.standings == []
    # The legacy parent/child ids stay authoritative.
    assert t.parent_generation_id == "v3"
    assert t.child_generation_id == "v4"


def test_old_active_tournament_entry_defaults_match_id_empty() -> None:
    e = ActiveTournamentEntry.from_dict({"entry_id": "a", "side": "parent", "status": "completed"})
    assert e.match_id == ""
    assert e.side == "parent"


def test_active_tournament_structure_envelope_round_trips() -> None:
    t = ActiveTournament(
        tournament_id="t1",
        parent_generation_id="",
        child_generation_id="",
        epoch_id="e3",
        started_at="2026-01-01T00:00:00Z",
        structure="swiss",
        structure_params={"rounds_n": 4},
        competitors=[{"generation_id": "v3", "seed": 1, "role": "champion"}],
        rounds=[{"round_index": 0, "label": "Round 1", "matches": []}],
        standings=[{"generation_id": "v3", "rank": 1, "scalar": 0.4}],
    )
    again = ActiveTournament.from_dict(t.to_dict())
    assert again.structure == "swiss"
    assert again.structure_params == {"rounds_n": 4}
    assert again.competitors[0]["generation_id"] == "v3"
    assert again.rounds[0]["label"] == "Round 1"
    assert again.standings[0]["rank"] == 1


def test_active_tournament_entry_with_match_id_round_trips() -> None:
    e = ActiveTournamentEntry(entry_id="a", side="v5", status="completed", match_id="r2_m1")
    again = ActiveTournamentEntry.from_dict(e.to_dict())
    assert again.side == "v5"  # widened domain: a generation id
    assert again.match_id == "r2_m1"


# ---------------------------------------------------------------------------
# Back-compat: OutcomeRecord / journal
# ---------------------------------------------------------------------------


def test_old_outcome_record_loads_with_gauntlet_defaults() -> None:
    legacy = {
        "ran_at": "2026-01-01T00:00:00Z",
        "drift_movements": [],
        "pass_rate_delta": 0.0,
        "drift_loss_delta": 0.0,
        "scalar_score_delta": -0.1,
        "tournament_decision": "promoted",
        "rejection_reason": "",
    }
    rec = _outcome_from_dict(legacy)
    assert rec is not None
    assert rec.structure == "gauntlet"
    assert rec.final_rank is None
    assert rec.eliminated_in_round is None
    assert rec.match_record == ()


def test_outcome_record_with_structure_fields_round_trips() -> None:
    from dataclasses import asdict

    from zicato.core.types import MatchOutcome

    rec = OutcomeRecord(
        ran_at="2026-01-01T00:00:00Z",
        drift_movements=(),
        pass_rate_delta=0.0,
        drift_loss_delta=0.0,
        scalar_score_delta=-0.2,
        tournament_decision="promoted",
        structure="single_elim",
        final_rank=1,
        eliminated_in_round=None,
        match_record=(
            MatchOutcome(match_id="WB-R0-0", opponent="v5", won=True, delta_scalar=-0.1),
        ),
    )
    again = _outcome_from_dict(asdict(rec))
    assert again is not None
    assert again.structure == "single_elim"
    assert again.final_rank == 1
    assert again.match_record[0].opponent == "v5"
    assert again.match_record[0].won is True


# ---------------------------------------------------------------------------
# ScoringWeights default
# ---------------------------------------------------------------------------


def test_scoring_weights_default_is_gauntlet() -> None:
    assert ScoringWeights().tournament_structure.structure == "gauntlet"
