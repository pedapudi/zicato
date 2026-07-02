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


def test_set_gate_monotonicity_scope() -> None:
    """The gate operation can author the issue-#17 monotonicity scope, and
    it survives the draft's scoring serialization."""
    import json

    import pytest

    draft = TournamentDraft()
    # Default is per_entry; flipping to aggregate records the change.
    assert draft.scoring.pass_rate_monotonicity_scope == "per_entry"
    patch = ops.set_gate(draft, monotonicity_scope="aggregate")
    assert draft.scoring.pass_rate_monotonicity_scope == "aggregate"
    assert patch.changed["pass_rate_monotonicity_scope"]["from"] == "per_entry"
    assert patch.changed["pass_rate_monotonicity_scope"]["to"] == "aggregate"

    # It is carried in the draft's serialized form (the shape the builder
    # persists / the REST surface returns).
    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["scoring"]["pass_rate_monotonicity_scope"] == "aggregate"

    # An invalid token is rejected, not silently coerced.
    with pytest.raises(ValueError, match="per_entry"):
        ops.set_gate(TournamentDraft(), monotonicity_scope="bogus")


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
    # A 6-entry board now clears the (lowered, 8 -> 6) split floor; this
    # test's subject is the swiss run arithmetic over the whole board, so
    # pin the holdout split off.
    _no_holdout(draft)
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
# Per-structure default replicates — the under-reporting bug + its anti-drift
# pin. The cost meter must default ``replicates`` to the STRUCTURE's own
# default (swiss / elim default to 2), not a flat 1, or it under-reports.
# ---------------------------------------------------------------------------


def _no_holdout(draft: TournamentDraft) -> None:
    """Disable the hash-derived holdout so the whole board is the train slice.

    Keeps the cost arithmetic a clean ``... × board`` with no holdout-confirm
    term, so the default-replicates assertions read off one number.
    """
    import dataclasses

    draft.scoring = dataclasses.replace(
        draft.scoring,
        overfitting=dataclasses.replace(draft.scoring.overfitting, enabled=False),
    )


def test_cost_swiss_unset_replicates_uses_strategy_default_two() -> None:
    # The under-reporting bug: with ``replicates`` UNSET the meter must use
    # swiss's strategy default of 2, not a flat 1. The old flat-1 default
    # would have reported HALF this number.
    draft = TournamentDraft()
    draft.entries = _board(8)
    _no_holdout(draft)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 4)
    # ``replicates`` is deliberately NOT set.
    est = ops.estimate_cost(draft)
    # rounds_n 4 (default) × pairings (4//2=2) × replicates 2 (swiss default)
    # × board 8 = 128. The old flat-1 default reported 64 — half the real cost.
    assert est.board_runs_per_round == 128
    detail = next(line.detail for line in est.breakdown if line.label == "swiss-pairing runs")
    assert "replicates 2" in detail


def test_cost_explicit_replicates_overrides_structure_default() -> None:
    # An EXPLICIT ``replicates`` is honored verbatim even when it differs from
    # the structure default (swiss default is 2; an explicit 1 still wins).
    draft = TournamentDraft()
    draft.entries = _board(8)
    _no_holdout(draft)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "replicates", 1)
    est1 = ops.estimate_cost(draft)
    assert est1.board_runs_per_round == 64  # 4 × 2 × 1 × 8

    ops.set_param(draft, "replicates", 3)
    est3 = ops.estimate_cost(draft)
    assert est3.board_runs_per_round == 192  # 4 × 2 × 3 × 8


def test_cost_unset_replicates_per_structure_defaults() -> None:
    # The per-structure default the estimator applies when ``replicates`` is
    # unset, for EVERY structure: the base default is now 2 (the noise-aware
    # posture) — gauntlet/elim/swiss inherit or pin it — while racing pins 1
    # (its replication is intrinsic to the escalating board slices). (Each
    # computed over an 8-board, field 4, holdout off.)
    expected = {
        "gauntlet": 2,
        "single_elim": 2,
        "double_elim": 2,
        "swiss": 2,
        "racing": 1,
    }
    for structure, default in expected.items():
        draft = TournamentDraft()
        draft.entries = _board(8)
        _no_holdout(draft)
        ops.set_structure(draft, structure)
        ops.set_param(draft, "field_size", 4)
        # ``replicates`` UNSET → the estimator resolves the structure default.
        est_default = ops.estimate_cost(draft)
        ops.set_param(draft, "replicates", default)
        est_explicit = ops.estimate_cost(draft)
        # The unset-default estimate equals the explicit-default estimate.
        assert est_default.board_runs_per_round == est_explicit.board_runs_per_round, structure


def test_estimator_default_matches_strategy_default_for_every_structure() -> None:
    # ANTI-DRIFT PIN: the default the cost estimator applies per structure
    # MUST equal the default the live SelectionStrategy uses when
    # ``params["replicates"]`` is unset. They read the SAME source
    # (``_default_replicates``); this test fails if a future change moves one
    # without the other.
    from zicato.core.types import VALID_TOURNAMENT_STRUCTURES
    from zicato.selection import default_replicates_for
    from zicato.selection.registry import STRATEGY_REGISTRY

    for structure in VALID_TOURNAMENT_STRUCTURES:
        cls = STRATEGY_REGISTRY[structure]
        # The strategy's actual default, as resolved in its ``__init__`` with
        # no ``replicates`` param.
        strategy = cls(params={})
        strategy_default = strategy._replicates  # type: ignore[attr-defined]
        # The estimator's per-structure default, from the shared map.
        estimator_default = default_replicates_for(structure)
        assert estimator_default == strategy_default, (
            f"{structure}: estimator default {estimator_default} != "
            f"strategy default {strategy_default}"
        )
        # And both equal the class ClassVar that is the source of truth.
        assert estimator_default == cls._default_replicates, structure


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


# ---------------------------------------------------------------------------
# set_screening — the candidate-screen (tryouts) contract knobs
# ---------------------------------------------------------------------------


def test_set_screening() -> None:
    import json

    import pytest

    draft = TournamentDraft()
    assert draft.scoring.proposer_quality.screen_entries == 0
    patch = ops.set_screening(draft, entries=2, veto_only=True)
    assert draft.scoring.proposer_quality.screen_entries == 2
    assert draft.scoring.proposer_quality.screen_veto_only is True
    assert patch.changed["screen_entries"] == {"from": 0, "to": 2}
    assert patch.changed["screen_veto_only"] == {"from": False, "to": True}

    # No-op edit records nothing.
    patch2 = ops.set_screening(draft, entries=2, veto_only=True)
    assert patch2.changed == {}

    # It survives the draft's serialized form (what the REST surface returns).
    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["scoring"]["proposer_quality"]["screen_entries"] == 2
    assert serialized["scoring"]["proposer_quality"]["screen_veto_only"] is True

    # A negative panel size is rejected, not silently coerced.
    with pytest.raises(ValueError, match=">= 0"):
        ops.set_screening(TournamentDraft(), entries=-1)


def test_cost_includes_candidate_screen_runs_when_opted_in() -> None:
    draft = TournamentDraft()
    draft.entries = _board(10)
    _no_holdout(draft)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 1)

    # Default (screen off): no candidate-screen line.
    est_off = ops.estimate_cost(draft)
    assert not any(line.label == "candidate-screen runs" for line in est_off.breakdown)

    # Opted in: proposes 1 (gauntlet) × best_of_n 3 (default) × panel 2.
    ops.set_screening(draft, entries=2)
    est = ops.estimate_cost(draft)
    screen_lines = [line for line in est.breakdown if line.label == "candidate-screen runs"]
    assert len(screen_lines) == 1
    assert screen_lines[0].runs == 1 * 3 * 2
    assert est.board_runs_per_round == est_off.board_runs_per_round + 6


def test_cost_screen_runs_scale_with_field_and_cap_at_board() -> None:
    draft = TournamentDraft()
    draft.entries = _board(3)
    _no_holdout(draft)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    # Panel request larger than the board caps at the train-board size.
    ops.set_screening(draft, entries=8)
    est = ops.estimate_cost(draft)
    screen_lines = [line for line in est.breakdown if line.label == "candidate-screen runs"]
    assert len(screen_lines) == 1
    # proposes 4 (racing field) × best_of_n 3 × panel min(8, 3) = 36.
    assert screen_lines[0].runs == 4 * 3 * 3
