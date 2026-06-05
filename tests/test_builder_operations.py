"""Tests for the builder operations: ops + patches, cost, validate."""

from __future__ import annotations

from goldfive import DriftSeverity

from zicato.board.split import HOLDOUT_TAG
from zicato.builder import operations as ops
from zicato.builder.draft import TournamentDraft
from zicato.core.types import (
    BoardEntry,
    JudgeMode,
    JudgeSpec,
)


def _entry(entry_id: str, *, tags: tuple[str, ...] = ()) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
        tags=tags,
    )


def _board(n: int) -> list[BoardEntry]:
    return [_entry(f"e{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Write ops + their DraftPatch
# ---------------------------------------------------------------------------


def test_set_structure_changes_structure_and_keeps_params() -> None:
    draft = TournamentDraft()
    ops.set_param(draft, "field_size", 4)
    patch = ops.set_structure(draft, "swiss")
    assert draft.scoring.tournament_structure.structure == "swiss"
    assert draft.scoring.tournament_structure.params["field_size"] == 4
    assert patch.to_dict()["changed"]["structure"] == {"from": "gauntlet", "to": "swiss"}


def test_set_param_and_remove() -> None:
    draft = TournamentDraft()
    ops.set_param(draft, "replicates", 3)
    assert draft.scoring.tournament_structure.params["replicates"] == 3
    patch = ops.set_param(draft, "replicates", None)
    assert "replicates" not in draft.scoring.tournament_structure.params
    assert patch.changed["replicates"]["to"] is None


def test_set_holdout_enabled_fraction_tags() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    patch = ops.set_holdout(draft, enabled=False, fraction=0.4, tags=["e0", "e2"])
    assert draft.scoring.overfitting.enabled is False
    assert draft.scoring.overfitting.holdout_fraction == 0.4
    held = {e.id for e in draft.entries if HOLDOUT_TAG in e.tags}
    assert held == {"e0", "e2"}
    assert patch.changed["holdout_tags"]["to"] == ["e0", "e2"]


def test_set_holdout_tags_replace_not_accumulate() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    ops.set_holdout(draft, tags=["e0"])
    ops.set_holdout(draft, tags=["e1"])
    held = {e.id for e in draft.entries if HOLDOUT_TAG in e.tags}
    assert held == {"e1"}


def test_set_proposer_and_clear() -> None:
    draft = TournamentDraft()
    patch = ops.set_proposer(draft, "proposers/p1")
    assert draft.proposer_path is not None
    assert draft.proposer_path.name == "p1"
    assert patch.changed["proposer_path"]["to"].endswith("p1")
    ops.set_proposer(draft, None)
    assert draft.proposer_path is None


def test_set_weights() -> None:
    draft = TournamentDraft()
    patch = ops.set_weights(
        draft,
        drift_weight=2.0,
        per_judge_weights={"j1": 3.0},
    )
    assert draft.scoring.drift_weight == 2.0
    assert dict(draft.scoring.per_judge_weights) == {"j1": 3.0}
    assert patch.changed["drift_weight"]["to"] == 2.0


def test_set_gate() -> None:
    draft = TournamentDraft()
    patch = ops.set_gate(draft, promote_margin=0.05, monotonicity=False)
    assert draft.scoring.promote_margin == 0.05
    assert draft.scoring.pass_rate_monotonicity is False
    assert patch.changed["promote_margin"]["to"] == 0.05


def test_edit_board_entry_add_then_replace() -> None:
    draft = TournamentDraft()
    p1 = ops.edit_board_entry(draft, _entry("x1"))
    assert p1.changed["action"] == "added"
    assert len(draft.entries) == 1
    p2 = ops.edit_board_entry(
        draft,
        BoardEntry(id="x1", kind="single_turn", wall_clock_budget_seconds=99, input="bye"),
    )
    assert p2.changed["action"] == "replaced"
    assert len(draft.entries) == 1
    assert draft.entries[0].wall_clock_budget_seconds == 99


def test_add_and_remove_judge() -> None:
    draft = TournamentDraft()
    ops.edit_board_entry(draft, _entry("x1"))
    judge = JudgeSpec(
        name="tone", mode=JudgeMode.INLINE, body="is it polite?", severity=DriftSeverity.WARNING
    )
    ops.add_judge(draft, "x1", judge)
    assert draft.entry_by_id("x1").judges[0].name == "tone"
    patch = ops.remove_judge(draft, "x1", "tone")
    assert draft.entry_by_id("x1").judges == ()
    assert patch.op == "remove_judge"


def test_remove_missing_judge_is_noop_with_note() -> None:
    draft = TournamentDraft()
    ops.edit_board_entry(draft, _entry("x1"))
    patch = ops.remove_judge(draft, "x1", "nope")
    assert "no judge named" in patch.note


def test_add_duplicate_judge_raises() -> None:
    draft = TournamentDraft()
    ops.edit_board_entry(draft, _entry("x1"))
    j = JudgeSpec(name="tone", mode=JudgeMode.INLINE, body="b", severity=DriftSeverity.INFO)
    ops.add_judge(draft, "x1", j)
    import pytest

    with pytest.raises(ValueError, match="already has a judge"):
        ops.add_judge(draft, "x1", j)


def test_set_brief() -> None:
    draft = TournamentDraft()
    patch = ops.set_brief(draft, "new brief text")
    assert draft.brief == "new brief text"
    assert patch.changed["brief_chars"]["to"] == len("new brief text")


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_cost_gauntlet() -> None:
    draft = TournamentDraft()
    draft.entries = _board(5)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 2)
    est = ops.estimate_cost(draft)
    # 1 field × 2 replicates × 5 board = 10 (no holdout on a 5-board: below
    # the default min_board_size_for_split, so holdout is empty).
    assert est.structure == "gauntlet"
    assert est.holdout_size == 0
    assert est.board_runs_per_round == 10


def test_cost_swiss() -> None:
    draft = TournamentDraft()
    draft.entries = _board(6)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "replicates", 2)
    ops.set_param(draft, "rounds_n", 3)
    est = ops.estimate_cost(draft)
    # rounds_n 3 × pairings (4//2=2) × replicates 2 × board 6 = 72.
    assert est.board_runs_per_round == 72


def test_cost_racing_sums_rungs_plus_final() -> None:
    draft = TournamentDraft()
    draft.entries = _board(8)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "eta", 2)
    ops.set_param(draft, "board_fraction", 0.25)
    ops.set_param(draft, "replicates", 1)
    est = ops.estimate_cost(draft)
    # base slice = ceil(8*0.25)=2. rung0: alive4 × slice2 = 8; alive→2,
    # rung1: slice4 → 2×4=8; alive→1 stops. final: full board 8 = 8.
    # total = 8 + 8 + 8 = 24 (no holdout: holdout empty on this board).
    assert est.structure == "racing"
    assert est.board_runs_per_round == 24
    labels = [line.label for line in est.breakdown]
    assert "racing-final runs" in labels


def test_cost_includes_holdout_confirm_runs() -> None:
    draft = TournamentDraft()
    # 12 entries, two explicitly tagged holdout → holdout split active.
    draft.entries = _board(12)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 1)
    ops.set_holdout(draft, tags=["e0", "e1"])
    est = ops.estimate_cost(draft)
    assert est.holdout_size == 2
    # duel: 1 × 1 × 10 train = 10; holdout-confirm: 2 × 1 = 2 → 12.
    assert est.board_runs_per_round == 12
    assert any(line.label == "holdout-confirm runs" for line in est.breakdown)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_field_size_one_degrades_to_gauntlet() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 1)
    codes = {w.code for w in ops.validate(draft)}
    assert "field_size_degrades_to_gauntlet" in codes


def test_validate_small_board_holdout_disabled() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    codes = {w.code for w in ops.validate(draft)}
    assert "holdout_disabled_small_board" in codes


def test_validate_racing_rung0_slice() -> None:
    draft = TournamentDraft()
    draft.entries = _board(8)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "board_fraction", 0.25)
    warns = {w.code: w for w in ops.validate(draft)}
    assert "racing_rung0_slice" in warns
    # ceil(8 * 0.25) = 2.
    assert "= 2 entries" in warns["racing_rung0_slice"].message


def test_validate_replicates_recommended_for_brackets() -> None:
    draft = TournamentDraft()
    draft.entries = _board(8)
    ops.set_structure(draft, "single_elim")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "replicates", 1)
    codes = {w.code for w in ops.validate(draft)}
    assert "replicates_recommended_for_brackets" in codes


def test_validate_whole_board_holdout() -> None:
    draft = TournamentDraft()
    draft.entries = _board(3)
    ops.set_holdout(draft, tags=["e0", "e1", "e2"])
    codes = {w.code for w in ops.validate(draft)}
    assert "holdout_tags_cover_whole_board" in codes
