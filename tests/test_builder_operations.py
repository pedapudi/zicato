"""Tests for the builder operations: ops + patches, cost, validate."""

from __future__ import annotations

import pytest
from goldfive import DriftSeverity

from zicato.board.split import HOLDOUT_TAG
from zicato.contract_draft import operations as ops
from zicato.contract_draft.draft import TournamentDraft
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
    patch = ops.set_structure(draft, "racing")
    assert draft.scoring.tournament_structure.structure == "racing"
    assert draft.scoring.tournament_structure.params["field_size"] == 4
    assert patch.to_dict()["changed"]["structure"] == {"from": "gauntlet", "to": "racing"}


def test_set_param_and_remove() -> None:
    draft = TournamentDraft()
    ops.set_param(draft, "replicates", 3)
    assert draft.scoring.tournament_structure.params["replicates"] == 3
    patch = ops.set_param(draft, "replicates", None)
    assert "replicates" not in draft.scoring.tournament_structure.params
    assert patch.changed["replicates"]["to"] is None


def test_set_param_validates_closed_vocabulary_keys() -> None:
    # A typo'd schedule would otherwise be stored verbatim, roll the epoch on
    # save, and only raise from make_strategy at round start. Catch it here so
    # the dispatch turns it into a field-precise 400.
    draft = TournamentDraft()
    with pytest.raises(ValueError, match="slice_schedule must be one of"):
        ops.set_param(draft, "slice_schedule", "shuffled")
    assert "slice_schedule" not in draft.scoring.tournament_structure.params

    for schedule in ("prefix", "shuffled_v1"):
        ops.set_param(draft, "slice_schedule", schedule)
        assert draft.scoring.tournament_structure.params["slice_schedule"] == schedule
    # Removal stays available, and is how the builder drops back to the default.
    ops.set_param(draft, "slice_schedule", None)
    assert "slice_schedule" not in draft.scoring.tournament_structure.params


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
        not_completed_weight=2.0,
        per_judge_weights={"j1": 3.0},
    )
    assert draft.scoring.not_completed_weight == 2.0
    assert dict(draft.scoring.per_judge_weights) == {"j1": 3.0}
    assert patch.changed["not_completed_weight"]["to"] == 2.0


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


def test_add_board_entry_appends_and_refuses_duplicate() -> None:
    draft = TournamentDraft()
    patch = ops.add_board_entry(draft, _entry("n1"))
    assert patch.op == "add_board_entry"
    assert patch.changed == {"entry_id": "n1", "action": "added"}
    assert [e.id for e in draft.entries] == ["n1"]
    # A strict ADD refuses a duplicate id (unlike edit_board_entry's replace).
    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="already exists"):
        ops.add_board_entry(draft, _entry("n1"))
    assert len(draft.entries) == 1


def test_add_board_entry_validates_and_preserves_provenance_context() -> None:
    import pytest  # noqa: PLC0415

    draft = TournamentDraft()
    # A malformed entry (single_turn with no input) is rejected before it lands.
    bad = BoardEntry(id="bad", kind="single_turn", wall_clock_budget_seconds=30)
    with pytest.raises(ValueError, match="requires 'input'"):
        ops.add_board_entry(draft, bad)
    assert draft.entries == []
    # Provenance riding the entry context is appended untouched (§4).
    entry = BoardEntry(
        id="prov",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="hi",
        context={"provenance": '{"miner_version": "eval-synth/1"}'},
    )
    ops.add_board_entry(draft, entry)
    assert draft.entry_by_id("prov").context["provenance"] == '{"miner_version": "eval-synth/1"}'


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


def test_set_board_meta_changed_map() -> None:
    draft = TournamentDraft()
    patch = ops.set_board_meta(draft, disable_drift=["off_topic", "user_steer"], judge_only=True)
    assert [str(k) for k in draft.disable_drift] == ["off_topic", "user_steer"]
    assert draft.judge_only is True
    assert patch.op == "set_board_meta"
    assert patch.changed["disable_drift"] == {"from": [], "to": ["off_topic", "user_steer"]}
    assert patch.changed["judge_only"] == {"from": False, "to": True}


def test_set_board_meta_noop_and_none_leaves_unchanged() -> None:
    draft = TournamentDraft()
    ops.set_board_meta(draft, disable_drift=["off_topic"], judge_only=True)
    # A re-issued identical edit is a no-op — an empty changed map.
    patch = ops.set_board_meta(draft, disable_drift=["off_topic"], judge_only=True)
    assert patch.changed == {}
    # None means "leave unchanged"; an empty list is a REAL clear.
    patch = ops.set_board_meta(draft, judge_only=False)
    assert [str(k) for k in draft.disable_drift] == ["off_topic"]
    assert draft.judge_only is False
    assert list(patch.changed) == ["judge_only"]
    patch = ops.set_board_meta(draft, disable_drift=[])
    assert draft.disable_drift == ()
    assert patch.changed["disable_drift"]["to"] == []


def test_set_board_meta_bad_token_raises() -> None:
    import pytest

    draft = TournamentDraft()
    with pytest.raises(ValueError, match="unknown drift kind"):
        ops.set_board_meta(draft, disable_drift=["not_a_kind"])
    # The failed edit did not corrupt the draft.
    assert draft.disable_drift == ()


def test_compare_drafts_board_meta_detail_key() -> None:
    a = TournamentDraft()
    b = TournamentDraft()
    diff = ops.compare_drafts(a, b)
    assert diff["board_meta"]["changed"] is False
    assert "board_meta" not in diff["changed_components"]

    ops.set_board_meta(b, disable_drift=["off_topic"], judge_only=True)
    diff = ops.compare_drafts(a, b)
    assert "board_meta" in diff["changed_components"]
    assert diff["board_meta"] == {
        "changed": True,
        "a": {"disable_drift": [], "judge_only": False},
        "b": {"disable_drift": ["off_topic"], "judge_only": True},
    }


def test_set_brief() -> None:
    draft = TournamentDraft()
    patch = ops.set_brief(draft, "new brief text")
    assert draft.brief == "new brief text"
    assert patch.changed["brief_chars"]["to"] == len("new brief text")


def test_remove_board_entry() -> None:
    import pytest

    draft = TournamentDraft()
    draft.entries = _board(3)
    patch = ops.remove_board_entry(draft, "e1")
    assert [e.id for e in draft.entries] == ["e0", "e2"]
    assert patch.op == "remove_board_entry"
    assert patch.changed == {"entry_id": "e1", "action": "removed"}
    # Unknown id raises rather than silently no-oping (a typo'd delete).
    with pytest.raises(ValueError, match="no board entry with id"):
        ops.remove_board_entry(draft, "ghost")
    assert [e.id for e in draft.entries] == ["e0", "e2"]


def test_restore_draft_in_place_reports_components() -> None:
    source = TournamentDraft()
    source.entries = _board(2)
    source.brief = "the source brief"

    draft = TournamentDraft()
    draft.entries = _board(3)
    ops.set_structure(draft, "racing")
    ops.set_board_meta(draft, judge_only=True)

    before_identity = draft
    patch = ops.restore_draft(draft, source)
    assert patch.op == "revert_to_live"
    # IN PLACE: the object identity is unchanged — slot bindings stay live.
    assert draft is before_identity
    assert draft.scoring == source.scoring
    assert [e.id for e in draft.entries] == ["e0", "e1"]
    assert draft.brief == "the source brief"
    assert draft.judge_only is False
    # The changed map names the restored components.
    assert set(patch.changed) == {"scoring", "board", "board_meta", "brief_chars"}
    assert patch.changed["board"]["removed"] == ["e2"]

    # Restoring again is an honest no-op with the note.
    patch = ops.restore_draft(draft, source)
    assert patch.changed == {}
    assert "already matches" in patch.note

    # The restored entries list is a COPY — mutating the draft afterwards
    # never leaks back into the source (undo snapshots stay pristine).
    ops.remove_board_entry(draft, "e0")
    assert [e.id for e in source.entries] == ["e0", "e1"]


def test_restore_draft_op_name_for_undo() -> None:
    draft = TournamentDraft()
    patch = ops.restore_draft(draft, TournamentDraft(), op="undo")
    assert patch.op == "undo"


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
    ops.set_experimental(draft, tournament_structures=True)
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


def test_cost_racing_rung0_override_moves_the_estimate() -> None:
    # rung0_board_size overrides ceil(board_fraction x board) for the first
    # rung, so pinning it must change the estimate rather than only the form.
    def _racing(**params: int | float) -> ops.CostEstimate:
        draft = TournamentDraft()
        draft.entries = _board(12)
        ops.set_structure(draft, "racing")
        ops.set_param(draft, "field_size", 4)
        ops.set_param(draft, "eta", 2)
        for key, value in params.items():
            ops.set_param(draft, key, value)
        return ops.estimate_cost(draft)

    by_fraction = _racing(board_fraction=0.25)
    by_count = _racing(rung0_board_size=6)
    assert by_count.board_runs_per_round != by_fraction.board_runs_per_round


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
    ops.set_experimental(draft, tournament_structures=True)
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
    ops.set_experimental(draft, tournament_structures=True)
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
        ops.set_experimental(draft, tournament_structures=True)
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
    from zicato.selection.registry import EXPERIMENTAL_STRATEGY_REGISTRY, STRATEGY_REGISTRY

    for structure in VALID_TOURNAMENT_STRUCTURES:
        cls = {**STRATEGY_REGISTRY, **EXPERIMENTAL_STRATEGY_REGISTRY}[structure]
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
    ops.set_experimental(draft, tournament_structures=True)
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
    ops.set_experimental(draft, tournament_structures=True)
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


# ---------------------------------------------------------------------------
# validate — the statistical margin-vs-noise-floor rule (REFUSE severity)
# ---------------------------------------------------------------------------


def test_validate_margin_below_noise_floor_refuses_when_gate_off() -> None:
    # Default contract: promote_margin 0.01, evidence gate OFF. A measured
    # floor at/above the margin makes margin-only duels noise-decided.
    draft = TournamentDraft()
    draft.entries = _board(4)
    warns = {w.code: w for w in ops.validate(draft, noise_floor_max_abs_delta=0.05)}
    assert "margin_below_noise_floor" in warns
    assert warns["margin_below_noise_floor"].severity == "refuse"
    assert "0.05" in warns["margin_below_noise_floor"].message

    # Exactly-at-floor also refuses (margin <= floor).
    ops.set_gate(draft, promote_margin=0.05)
    codes = {w.code for w in ops.validate(draft, noise_floor_max_abs_delta=0.05)}
    assert "margin_below_noise_floor" in codes


def test_validate_margin_rule_silent_when_gate_on_or_margin_clears() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    # Evidence gate ON (threshold in (0,1)) silences the rule — the
    # defer→replicate loop supplies the statistical resolution instead.
    ops.set_param(draft, "promote_confidence_threshold", 0.8)
    codes = {w.code for w in ops.validate(draft, noise_floor_max_abs_delta=0.05)}
    assert "margin_below_noise_floor" not in codes

    # Gate off but the margin clears the floor — silent.
    ops.set_param(draft, "promote_confidence_threshold", None)
    ops.set_gate(draft, promote_margin=0.06)
    codes = {w.code for w in ops.validate(draft, noise_floor_max_abs_delta=0.05)}
    assert "margin_below_noise_floor" not in codes


def test_validate_margin_rule_silent_without_any_floor() -> None:
    # No floor passed, no workspace: the rule cannot fire (no guessing).
    draft = TournamentDraft()
    draft.entries = _board(4)
    codes = {w.code for w in ops.validate(draft)}
    assert "margin_below_noise_floor" not in codes


def test_validate_reads_measured_floor_off_the_epoch_record(tmp_path) -> None:
    """With a workspace, validate() reads the CURRENT epoch's measured
    ``noise_floor`` record (the `zicato board audit` shape) on its own."""
    import json

    from zicato.epoch.lifecycle import new_epoch, set_epoch_noise_floor

    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# b\n", encoding="utf-8")
    (ws / "config.json").write_text(
        json.dumps({"instance_id": "default", "generation_source_backend": "git"}),
        encoding="utf-8",
    )
    from zicato.core.types import ScoringWeights

    cfg = new_epoch(ws, name="a", board_source=board, brief_source=brief, weights=ScoringWeights())

    draft = TournamentDraft()
    draft.entries = _board(4)
    # No measurement yet: silent.
    codes = {w.code for w in ops.validate(draft, ws)}
    assert "margin_below_noise_floor" not in codes

    set_epoch_noise_floor(ws, cfg.id, {"max_abs_delta": 0.2, "runs": 5})
    warns = {w.code: w for w in ops.validate(draft, ws)}
    assert "margin_below_noise_floor" in warns
    assert warns["margin_below_noise_floor"].severity == "refuse"

    # An explicit floor argument overrides the record.
    codes = {w.code for w in ops.validate(draft, ws, noise_floor_max_abs_delta=0.001)}
    assert "margin_below_noise_floor" not in codes


# ---------------------------------------------------------------------------
# preflight — the build-time statistical measurement (honest degrades)
# ---------------------------------------------------------------------------


def test_preflight_degrades_on_empty_board_and_missing_epoch(tmp_path) -> None:
    import asyncio

    ws = tmp_path / ".zicato"
    ws.mkdir()

    # Empty draft board: nothing to measure.
    empty = TournamentDraft()
    res = asyncio.run(ops.preflight(empty, ws))
    assert res.available is False
    assert "non-empty draft board" in res.reason
    assert res.to_dict()["verdict"] is None

    # A board but no current epoch: preflight needs a registered target.
    draft = TournamentDraft()
    draft.entries = _board(3)
    res = asyncio.run(ops.preflight(draft, ws))
    assert res.available is False
    assert "registered target" in res.reason


def test_preflight_degrades_without_seeded_baseline(tmp_path) -> None:
    import asyncio
    import json

    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch

    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# b\n", encoding="utf-8")
    (ws / "config.json").write_text(
        json.dumps({"instance_id": "default", "generation_source_backend": "git"}),
        encoding="utf-8",
    )
    new_epoch(ws, name="a", board_source=board, brief_source=brief, weights=ScoringWeights())

    draft = TournamentDraft()
    draft.entries = _board(3)
    res = asyncio.run(ops.preflight(draft, ws))
    assert res.available is False
    assert "baseline" in res.reason


def test_preflight_rejects_sub_two_runs(tmp_path) -> None:
    import asyncio

    import pytest

    draft = TournamentDraft()
    draft.entries = _board(3)
    with pytest.raises(ValueError, match="at least 2"):
        asyncio.run(ops.preflight(draft, tmp_path, runs=1))


def test_preflight_measures_draft_contract_against_target0(tmp_path) -> None:
    """The REAL measurement, against target_0's deterministic adapter with
    the runtime call_llm config in config.json (the shape the builder relies
    on — no explicit callables). Verdict OK; the epoch record is untouched
    (a draft measurement never masquerades as the live epoch's)."""
    import asyncio
    import json
    from pathlib import Path

    import zicato_examples.target_0_convergence as _t0_pkg
    from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch

    example_dir = Path(_t0_pkg.__file__).resolve().parent
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "generation_source_backend": "git",
                "adapter": {
                    "kind": "import",
                    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
                },
                "mutable_trees": [str(example_dir / "agent")],
                "runtime": {
                    "target_call_llm": "zicato_examples.target_0_convergence.mocks:target_llm",
                    "evaluation_call_llm": "zicato_examples.target_0_convergence.mocks:aux_llm",
                },
            }
        ),
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Pre-flight brief\n", encoding="utf-8")
    weights = _scoring_from_dict(json.loads((example_dir / "scoring.json").read_text()))
    cfg = new_epoch(
        ws,
        name="t0-builder-preflight",
        board_source=example_dir / "board.jsonl",
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=example_dir / "proposer",
    )
    # Seed v0 so there is a champion tree to probe.
    from zicato import workspace_loader
    from zicato.evolve.round_baseline import _ensure_baseline_snapshot

    _ensure_baseline_snapshot(ws, cfg.id, workspace_loader.load_workspace_config(ws))

    draft = TournamentDraft.from_workspace(ws)
    res = asyncio.run(ops.preflight(draft, ws, runs=3))
    assert res.available is True, res.reason
    assert res.verdict == "ok"
    assert res.report is not None
    assert res.report["signal"] > 0.0
    assert res.noise_floor is not None
    assert res.noise_floor["max_abs_delta"] == 0.0

    # RECOMMEND-ONLY and draft-scoped: nothing persisted onto the record.
    record = load_epoch(ws, cfg.id)
    assert record.preflight is None
    assert record.noise_floor is None


# ---------------------------------------------------------------------------
# Full knob coverage — set_holdout (overfitting), set_gate (hard blocks),
# set_namespace_weights, set_proposer_quality, set_experiment_memory
# ---------------------------------------------------------------------------


def test_set_holdout_full_overfitting_coverage() -> None:
    draft = TournamentDraft()
    patch = ops.set_holdout(
        draft,
        min_board_size_for_split=10,
        rotate_holdout=False,
        restrict_proposer_visibility=False,
        random_baseline_every_n=5,
        max_generations_per_contract=40,
    )
    of = draft.scoring.overfitting
    assert of.min_board_size_for_split == 10
    assert of.rotate_holdout is False
    assert of.restrict_proposer_visibility is False
    assert of.random_baseline_every_n == 5
    assert of.max_generations_per_contract == 40
    assert patch.changed["random_baseline_every_n"] == {"from": 0, "to": 5}
    assert patch.changed["max_generations_per_contract"] == {"from": None, "to": 40}

    # ``0`` clears the ceiling (None is reserved for "leave unchanged").
    patch2 = ops.set_holdout(draft, max_generations_per_contract=0)
    assert draft.scoring.overfitting.max_generations_per_contract is None
    assert patch2.changed["max_generations_per_contract"] == {"from": 40, "to": None}

    # No-op edit records nothing.
    patch3 = ops.set_holdout(draft, rotate_holdout=False)
    assert patch3.changed == {}


def test_set_holdout_ladder_partial_mapping() -> None:
    import pytest

    draft = TournamentDraft()
    patch = ops.set_holdout(draft, ladder={"budget": 8, "noise_scale": 0.1})
    ladder = draft.scoring.overfitting.ladder
    assert ladder.budget == 8
    assert ladder.noise_scale == 0.1
    assert ladder.enabled is True  # untouched by the partial mapping
    assert patch.changed["ladder.budget"] == {"from": 16, "to": 8}

    # An explicit threshold pins it; an explicit null resets to auto.
    ops.set_holdout(draft, ladder={"threshold": 0.02})
    assert draft.scoring.overfitting.ladder.threshold == 0.02
    patch_auto = ops.set_holdout(draft, ladder={"threshold": None})
    assert draft.scoring.overfitting.ladder.threshold is None
    assert patch_auto.changed["ladder.threshold"] == {"from": 0.02, "to": None}

    # Unknown ladder keys raise; invalid values hit the dataclass validator.
    with pytest.raises(ValueError, match="unknown ladder key"):
        ops.set_holdout(draft, ladder={"nope": 1})
    with pytest.raises(ValueError, match="budget"):
        ops.set_holdout(draft, ladder={"budget": -1})


def test_set_holdout_invalid_values_rejected_by_dataclass() -> None:
    import pytest

    with pytest.raises(ValueError, match="random_baseline_every_n"):
        ops.set_holdout(TournamentDraft(), random_baseline_every_n=-1)
    with pytest.raises(ValueError, match="holdout_fraction"):
        ops.set_holdout(TournamentDraft(), fraction=1.5)


def test_set_gate_full_coverage() -> None:
    import pytest

    draft = TournamentDraft()
    patch = ops.set_gate(
        draft,
        block_on_containment_violation=True,
        block_on_gate_contradiction=True,
        regression_gate_enabled=True,
        regression_test_command=["python", "-m", "unittest", "discover"],
        regression_timeout_s=120,
        namespace_monotonicity={"rubric:": True, "schema:": False},
    )
    sc = draft.scoring
    assert sc.block_on_containment_violation is True
    assert sc.block_on_gate_contradiction is True
    assert sc.regression_gate_enabled is True
    assert sc.regression_test_command == ("python", "-m", "unittest", "discover")
    assert sc.regression_timeout_s == 120
    assert dict(sc.namespace_monotonicity) == {"rubric:": True, "schema:": False}
    assert patch.changed["block_on_containment_violation"]["to"] is True
    assert patch.changed["regression_test_command"]["to"] == [
        "python",
        "-m",
        "unittest",
        "discover",
    ]

    # It survives the draft's serialized form (the REST envelope shape).
    import json

    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["scoring"]["block_on_gate_contradiction"] is True
    assert serialized["scoring"]["namespace_monotonicity"] == {"rubric:": True, "schema:": False}

    with pytest.raises(ValueError, match="non-empty argv"):
        ops.set_gate(TournamentDraft(), regression_test_command=[])
    with pytest.raises(ValueError, match=">= 1"):
        ops.set_gate(TournamentDraft(), regression_timeout_s=0)


def test_set_gate_holdout_confirmation_bounds() -> None:
    """The holdout confirmation's own bounds are settable from the builder.

    Both knobs (issue #118) reached a working gate with no builder path at
    all; the registry's exemption guard is what named the omission. The
    reset asymmetry is the interesting half: ``None`` means "leave
    unchanged", so a NEGATIVE margin is the token that clears the pin back
    to auto (reuse ``promote_margin``).
    """
    import pytest

    draft = TournamentDraft()
    assert draft.scoring.holdout_margin is None

    patch = ops.set_gate(draft, holdout_margin=0.04, holdout_entry_regression_budget=1)
    assert draft.scoring.holdout_margin == 0.04
    assert draft.scoring.holdout_entry_regression_budget == 1
    assert patch.changed["holdout_margin"] == {"from": None, "to": 0.04}
    assert patch.changed["holdout_entry_regression_budget"] == {"from": 0, "to": 1}

    # 0 is a REAL pin, not the off token — it must not read as "clear".
    ops.set_gate(draft, holdout_margin=0.0)
    assert draft.scoring.holdout_margin == 0.0

    # …and a negative resets to auto rather than raising through the
    # dataclass validator (which rejects a negative outright).
    reset = ops.set_gate(draft, holdout_margin=-1)
    assert draft.scoring.holdout_margin is None
    assert reset.changed["holdout_margin"] == {"from": 0.0, "to": None}

    # A no-op re-post records no change (the op is idempotent per knob).
    assert ops.set_gate(draft, holdout_entry_regression_budget=1).changed == {}

    with pytest.raises(ValueError, match="holdout_entry_regression_budget"):
        ops.set_gate(TournamentDraft(), holdout_entry_regression_budget=-1)


def test_set_namespace_weights() -> None:
    import pytest

    draft = TournamentDraft()
    weights = {"drift:": 2.0, "failure:": 1.0, "rubric:": -0.5, "cost:": 0.0}
    patch = ops.set_namespace_weights(draft, namespace_weights=weights, diff_complexity_weight=0.01)
    assert dict(draft.scoring.namespace_weights) == weights
    assert draft.scoring.diff_complexity_weight == 0.01
    assert patch.changed["namespace_weights"]["to"] == weights
    assert patch.changed["diff_complexity_weight"] == {"from": 0.0, "to": 0.01}

    # The paired parsimony CEILING sets + records like the weight.
    patch_ceil = ops.set_namespace_weights(draft, diff_complexity_ceiling=10.0)
    assert draft.scoring.diff_complexity_ceiling == 10.0
    assert patch_ceil.changed["diff_complexity_ceiling"] == {"from": 0.0, "to": 10.0}

    # No-op replacement records nothing.
    patch2 = ops.set_namespace_weights(draft, namespace_weights=dict(weights))
    assert patch2.changed == {}

    with pytest.raises(ValueError, match=">= 0"):
        ops.set_namespace_weights(TournamentDraft(), diff_complexity_weight=-0.1)

    with pytest.raises(ValueError, match=">= 0"):
        ops.set_namespace_weights(TournamentDraft(), diff_complexity_ceiling=-1.0)


def test_set_proposer_quality_composes_with_screening() -> None:
    import pytest

    draft = TournamentDraft()
    ops.set_screening(draft, entries=2, veto_only=True)
    patch = ops.set_proposer_quality(draft, best_of_n=5, critique_enabled=False)
    quality = draft.scoring.proposer_quality
    assert quality.best_of_n == 5
    assert quality.critique_enabled is False
    # COMPOSITION: the screen knobs set by set_screening are untouched.
    assert quality.screen_entries == 2
    assert quality.screen_veto_only is True
    assert patch.changed["best_of_n"] == {"from": 3, "to": 5}

    # And the reverse: set_screening leaves the quality knobs alone.
    ops.set_screening(draft, entries=4)
    assert draft.scoring.proposer_quality.best_of_n == 5

    with pytest.raises(ValueError, match=">= 1"):
        ops.set_proposer_quality(TournamentDraft(), best_of_n=0)


def test_set_proposer_quality_recombine_arg() -> None:
    """The recombination-slot flag round-trips via the changed-dict pattern and
    composes with the other quality knobs (default-off ⇒ omitted from changed)."""
    draft = TournamentDraft()
    # Default-off ⇒ passing the current value is a no-op (no changed entry, no roll).
    noop = ops.set_proposer_quality(draft, recombine=False)
    assert "recombine" not in noop.changed
    assert draft.scoring.proposer_quality.recombine is False

    # Flipping it on lands on the nested block and records the from/to delta.
    patch = ops.set_proposer_quality(draft, best_of_n=4, recombine=True)
    quality = draft.scoring.proposer_quality
    assert quality.recombine is True
    assert quality.best_of_n == 4
    assert patch.changed["recombine"] == {"from": False, "to": True}

    # Flipping it back off records the reverse delta.
    off = ops.set_proposer_quality(draft, recombine=False)
    assert off.changed["recombine"] == {"from": True, "to": False}
    assert draft.scoring.proposer_quality.recombine is False


def test_set_proposer_quality_genealogy_arg() -> None:
    """The genealogy count round-trips via the changed-dict pattern; default-off
    ⇒ omitted from changed; a negative value raises."""
    import pytest

    draft = TournamentDraft()
    # Default 0 ⇒ passing the current value is a no-op (no changed entry, no roll).
    noop = ops.set_proposer_quality(draft, genealogy=0)
    assert "genealogy" not in noop.changed
    assert draft.scoring.proposer_quality.genealogy == 0

    # A positive count lands on the nested block and records the from/to delta.
    patch = ops.set_proposer_quality(draft, genealogy=4)
    assert draft.scoring.proposer_quality.genealogy == 4
    assert patch.changed["genealogy"] == {"from": 0, "to": 4}

    # Back to 0 records the reverse delta.
    off = ops.set_proposer_quality(draft, genealogy=0)
    assert off.changed["genealogy"] == {"from": 4, "to": 0}

    with pytest.raises(ValueError, match="genealogy must be >= 0"):
        ops.set_proposer_quality(TournamentDraft(), genealogy=-1)


def test_set_proposer_quality_calibration_feedback_arg() -> None:
    """The calibration_feedback count round-trips via the changed-dict pattern;
    default-off ⇒ omitted from changed; a negative value raises."""
    import pytest

    draft = TournamentDraft()
    noop = ops.set_proposer_quality(draft, calibration_feedback=0)
    assert "calibration_feedback" not in noop.changed
    assert draft.scoring.proposer_quality.calibration_feedback == 0

    patch = ops.set_proposer_quality(draft, calibration_feedback=5)
    assert draft.scoring.proposer_quality.calibration_feedback == 5
    assert patch.changed["calibration_feedback"] == {"from": 0, "to": 5}

    off = ops.set_proposer_quality(draft, calibration_feedback=0)
    assert off.changed["calibration_feedback"] == {"from": 5, "to": 0}

    with pytest.raises(ValueError, match="calibration_feedback must be >= 0"):
        ops.set_proposer_quality(TournamentDraft(), calibration_feedback=-1)


def test_set_experiment_memory() -> None:
    import json

    draft = TournamentDraft()
    assert draft.scoring.experiment_memory.cross_epoch is False
    patch = ops.set_experiment_memory(draft, cross_epoch=True)
    assert draft.scoring.experiment_memory.cross_epoch is True
    assert patch.changed["cross_epoch"] == {"from": False, "to": True}
    # No-op records nothing.
    assert ops.set_experiment_memory(draft, cross_epoch=True).changed == {}
    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["scoring"]["experiment_memory"]["cross_epoch"] is True


def test_set_goldfive_activation_partial_update_and_removal() -> None:
    draft = TournamentDraft()
    activated = ops.set_goldfive(draft, config={})
    assert activated.changed["goldfive"]["from"] is None
    assert draft.scoring.goldfive is not None
    ops.set_goldfive(draft, config={"judge": {"model": "judge-a"}})
    ops.set_goldfive(draft, config={"steering": {"threshold": "critical"}})
    assert draft.scoring.goldfive["judge"]["model"] == "judge-a"
    assert draft.scoring.goldfive["steering"]["threshold"] == "critical"
    assert ops.set_goldfive(draft, config=None).changed["goldfive"]["to"] is None
    assert draft.scoring.goldfive is None


def test_set_goldfive_list_update_is_stable_and_preserved_by_later_edits() -> None:
    draft = TournamentDraft()
    config = {"steering": {"context_editor_rules": ["prune_stale_steer"]}}

    assert ops.set_goldfive(draft, config=config).changed
    assert ops.set_goldfive(draft, config=config).changed == {}
    ops.set_goldfive(draft, config={"steering": {"threshold": "critical"}})

    stored = draft.scoring.to_json()["goldfive"]
    assert stored["steering"]["context_editor_rules"] == ["prune_stale_steer"]
    assert stored["steering"]["threshold"] == "critical"


def test_set_telemetry_dialect() -> None:
    """The telemetry dialect round-trips via the changed-dict pattern; the
    default (goldfive) is a no-op; an unknown name raises; None leaves it be."""
    import json

    import pytest

    draft = TournamentDraft()
    assert draft.scoring.telemetry_dialect == "goldfive"

    # Default-value ⇒ no-op (no changed entry, no roll).
    noop = ops.set_telemetry_dialect(draft, dialect="goldfive")
    assert noop.changed == {}
    assert draft.scoring.telemetry_dialect == "goldfive"
    # None ⇒ leave unchanged.
    assert ops.set_telemetry_dialect(draft, dialect=None).changed == {}

    # A non-default dialect lands on the field and records the from/to delta.
    patch = ops.set_telemetry_dialect(draft, dialect="adk_events")
    assert draft.scoring.telemetry_dialect == "adk_events"
    assert patch.changed["telemetry_dialect"] == {"from": "goldfive", "to": "adk_events"}

    # Reverting to goldfive records the reverse delta (the contract pins both
    # directions — reverting rolls back to the original hash).
    back = ops.set_telemetry_dialect(draft, dialect="goldfive")
    assert back.changed["telemetry_dialect"] == {"from": "adk_events", "to": "goldfive"}
    assert draft.scoring.telemetry_dialect == "goldfive"

    # An unknown name raises (the closed dialect set — never a second list).
    with pytest.raises(ValueError, match="telemetry_dialect must be one of"):
        ops.set_telemetry_dialect(draft, dialect="mystery")
    # The field is unchanged after the raise (validated before applying).
    assert draft.scoring.telemetry_dialect == "goldfive"

    ops.set_telemetry_dialect(draft, dialect="transcript")
    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["scoring"]["telemetry_dialect"] == "transcript"


# ---------------------------------------------------------------------------
# Honest cost meter — the evidence-gate confirm budget, the best-of-N
# evaluation line, and the placebo cadence
# ---------------------------------------------------------------------------


def test_cost_evidence_gate_confirm_budget_is_priced() -> None:
    draft = TournamentDraft()
    draft.entries = _board(10)
    _no_holdout(draft)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 1)

    # Gate off: no crowning-confirm line.
    est_off = ops.estimate_cost(draft)
    assert not any("crowning-confirm" in line.label for line in est_off.breakdown)

    # The recommended-scaffold gate: threshold 0.8, budget 32. Each
    # replicate is a FRESH board sweep for BOTH contestants:
    # 32 × 2 × 10 = 640 — the largest term by far (duels are 10).
    ops.set_param(draft, "promote_confidence_threshold", 0.8)
    ops.set_param(draft, "promote_confidence_replicates", 32)
    est = ops.estimate_cost(draft)
    confirm = [line for line in est.breakdown if "crowning-confirm" in line.label]
    assert len(confirm) == 1
    assert confirm[0].runs == 32 * 2 * 10
    assert "per" in confirm[0].detail and "crowning" in confirm[0].detail
    assert est.board_runs_per_round == est_off.board_runs_per_round + 640
    # It IS the largest line on the meter.
    assert confirm[0].runs == max(line.runs for line in est.breakdown)

    # Unset budget defaults to the evidence gate's own default (3).
    ops.set_param(draft, "promote_confidence_replicates", None)
    est_default = ops.estimate_cost(draft)
    confirm_default = next(
        line for line in est_default.breakdown if "crowning-confirm" in line.label
    )
    assert confirm_default.runs == 3 * 2 * 10


def test_cost_best_of_n_evaluation_line_excluded_from_headline() -> None:
    draft = TournamentDraft()
    draft.entries = _board(10)
    _no_holdout(draft)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 1)

    # Default best_of_n is 3: the evaluation line appears (1 × 3 calls)…
    est = ops.estimate_cost(draft)
    aux = [line for line in est.breakdown if line.label == "best-of-N propose calls"]
    assert len(aux) == 1
    assert aux[0].runs == 1 * 3
    assert "evaluation" in aux[0].detail
    # …but the headline counts only board runs (1 × 1 × 10 = 10).
    assert est.board_runs_per_round == 10

    # best_of_n 1 (the historical single sample): no line.
    ops.set_proposer_quality(draft, best_of_n=1)
    est1 = ops.estimate_cost(draft)
    assert not any(line.label == "best-of-N propose calls" for line in est1.breakdown)

    # A wider structure proposes field_size challengers: 4 × 3 = 12 calls.
    ops.set_proposer_quality(draft, best_of_n=3)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    est4 = ops.estimate_cost(draft)
    aux4 = next(line for line in est4.breakdown if line.label == "best-of-N propose calls")
    assert aux4.runs == 4 * 3


def test_cost_placebo_cadence_amortized() -> None:
    draft = TournamentDraft()
    draft.entries = _board(10)
    _no_holdout(draft)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 2)

    est_off = ops.estimate_cost(draft)
    assert not any("placebo" in line.label for line in est_off.breakdown)

    # Every 4th round fields one extra no-op challenger: a full duel of
    # replicates 2 × board 10 = 20 runs, amortized to ceil(20/4) = 5.
    ops.set_holdout(draft, random_baseline_every_n=4)
    est = ops.estimate_cost(draft)
    placebo = [line for line in est.breakdown if "placebo" in line.label]
    assert len(placebo) == 1
    assert placebo[0].runs == 5
    assert est.board_runs_per_round == est_off.board_runs_per_round + 5


def test_cost_all_honest_terms_compose_with_the_screen_line() -> None:
    """The full recommended-scaffold shape: screen + best-of-N + evidence
    gate + placebo all on at once — every line present, and the headline is
    exactly the sum of the board-run lines (the evaluation line excluded)."""
    draft = TournamentDraft()
    draft.entries = _board(10)
    _no_holdout(draft)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "eta", 2)
    ops.set_param(draft, "board_fraction", 0.4)
    ops.set_param(draft, "replicates", 2)
    ops.set_param(draft, "promote_confidence_threshold", 0.8)
    ops.set_param(draft, "promote_confidence_replicates", 32)
    ops.set_screening(draft, entries=2)
    ops.set_holdout(draft, random_baseline_every_n=5)

    est = ops.estimate_cost(draft)
    labels = [line.label for line in est.breakdown]
    assert any("candidate-screen" in label for label in labels)
    assert "best-of-N propose calls" in labels
    assert any("crowning-confirm" in label for label in labels)
    assert any("placebo" in label for label in labels)

    board_run_total = sum(
        line.runs for line in est.breakdown if line.label != "best-of-N propose calls"
    )
    assert est.board_runs_per_round == board_run_total
    # The evidence budget dominates: 32 × 2 × 10 = 640 of the total.
    confirm = next(line for line in est.breakdown if "crowning-confirm" in line.label)
    assert confirm.runs == 640
    assert confirm.runs > est.board_runs_per_round / 2


# ---------------------------------------------------------------------------
# Lifecycle — DraftStore named slots (fork / list / switch) + compare_drafts
# ---------------------------------------------------------------------------


def _slot_workspace(tmp_path) -> object:
    """A minimal workspace with a live contract for DraftStore init."""
    import json

    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch
    from zicato.workspace.config_io import write_workspace_config

    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n'
        '{"id": "e2", "kind": "single_turn", "budget_s": 60, "input": "bye"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"pass_weight": 1.0}), encoding="utf-8")
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "contract": {
                "board_path": str(board.resolve()),
                "rubric_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )
    new_epoch(ws, name="a", board_source=board, brief_source=brief, weights=ScoringWeights())
    return ws


def test_draftstore_fork_switch_roundtrip(tmp_path) -> None:
    import pytest

    from zicato.contract_draft.draft import DraftStore

    ws = _slot_workspace(tmp_path)
    store = DraftStore()
    assert store.list_drafts() == []

    # Build up some working state, then fork it into slot A.
    working = store.get("s", ws)
    ops.set_structure(working, "racing")
    forked = store.fork("s", "variant-a", ws)
    assert store.list_drafts() == ["variant-a"]
    # The fork inherits the working state and IS the session's draft now.
    assert forked.scoring.tournament_structure.structure == "racing"
    assert store.get("s", ws) is forked

    # Edits accumulate on the slot; fork B from A, edit B; switch back to A.
    ops.set_param(forked, "field_size", 4)
    forked_b = store.fork("s", "variant-b", ws)
    ops.set_structure(forked_b, "racing")
    assert store.list_drafts() == ["variant-a", "variant-b"]
    back = store.switch("s", "variant-a")
    assert back.scoring.tournament_structure.structure == "racing"
    assert back.scoring.tournament_structure.params["field_size"] == 4
    # B kept its own state — the fork was a real copy, no shared mutation.
    assert store.slot("variant-b").scoring.tournament_structure.structure == "racing"
    assert store.slot("variant-a").scoring.tournament_structure.structure == "racing"

    # Errors: duplicate name, malformed name, unknown switch target.
    with pytest.raises(ValueError, match="already exists"):
        store.fork("s", "variant-a", ws)
    with pytest.raises(ValueError, match="invalid draft name"):
        store.fork("s", "no spaces!", ws)
    with pytest.raises(ValueError, match="no draft named"):
        store.switch("s", "nope")


def test_draftstore_fork_board_edits_do_not_leak(tmp_path) -> None:
    from zicato.contract_draft.draft import DraftStore

    ws = _slot_workspace(tmp_path)
    store = DraftStore()
    store.get("s", ws)
    a = store.fork("s", "a", ws)
    b = store.fork("s", "b", ws)
    ops.edit_board_entry(b, _entry("extra"))
    assert {e.id for e in b.entries} == {"e1", "e2", "extra"}
    assert {e.id for e in a.entries} == {"e1", "e2"}


def test_compare_drafts_keyed_diff() -> None:
    a = TournamentDraft()
    a.entries = _board(3)
    b = TournamentDraft()
    b.entries = _board(3)

    # Identical drafts: nothing changed.
    same = ops.compare_drafts(a, b)
    assert same["changed_components"] == []
    assert same["scoring"] == {}
    assert same["board"] == {"added": [], "removed": [], "changed": []}

    # Scoring diff is keyed on the contract-canonical scoring keys.
    ops.set_structure(b, "racing")
    ops.set_gate(b, promote_margin=0.05)
    # Board: b gains an entry, loses one, and edits one in place.
    ops.edit_board_entry(b, _entry("extra"))
    b.entries = [e for e in b.entries if e.id != "e0"]
    import dataclasses as _dc

    b.entries = [_dc.replace(e, input="changed") if e.id == "e1" else e for e in b.entries]
    ops.set_brief(b, "different brief")

    diff = ops.compare_drafts(a, b)
    assert set(diff["changed_components"]) == {"scoring", "board", "brief"}
    assert diff["scoring"]["promote_margin"] == {"a": 0.01, "b": 0.05}
    assert diff["scoring"]["tournament_structure"]["a"]["structure"] == "gauntlet"
    assert diff["scoring"]["tournament_structure"]["b"]["structure"] == "racing"
    assert diff["board"]["added"] == ["extra"]
    assert diff["board"]["removed"] == ["e0"]
    assert diff["board"]["changed"] == ["e1"]
    assert diff["brief"]["changed"] is True
    assert diff["proposer"]["changed"] is False


# ---------------------------------------------------------------------------
# validate: the board-authoring codes (all recommend-only; the dotted-path
# checks are SHAPE-ONLY — validate never imports an operator-supplied path)
# ---------------------------------------------------------------------------


def _codes(draft: TournamentDraft) -> dict[str, list]:
    out: dict[str, list] = {}
    for w in ops.validate(draft):
        out.setdefault(w.code, []).append(w)
    return out


def test_validate_duplicate_entry_id_refuses() -> None:
    draft = TournamentDraft()
    draft.entries = [_entry("dup"), _entry("dup"), _entry("ok")]
    warns = _codes(draft)["duplicate_entry_id"]
    assert len(warns) == 1
    assert warns[0].severity == "refuse"
    assert "'dup'" in warns[0].message
    # A clean board does not fire it.
    draft.entries = _board(3)
    assert "duplicate_entry_id" not in _codes(draft)


def test_validate_entry_id_unsafe() -> None:
    draft = TournamentDraft()
    draft.entries = [_entry("ok-id.1"), _entry("bad id/slash")]
    warns = _codes(draft)["entry_id_unsafe"]
    assert len(warns) == 1
    assert warns[0].severity == "warning"
    assert "bad id/slash" in warns[0].message


def test_validate_dotted_path_malformed_predicate_and_judge() -> None:
    import dataclasses as _dc

    from goldfive import DriftSeverity as _Sev

    from zicato.core.types import Expectation, ExpectationKind

    draft = TournamentDraft()
    good_pred = _dc.replace(
        _entry("good-pred"),
        expectation=Expectation(kind=ExpectationKind.PREDICATE, spec="pkg.mod:check"),
    )
    bad_pred = _dc.replace(
        _entry("bad-pred"),
        expectation=Expectation(kind=ExpectationKind.PREDICATE, spec="not a path!"),
    )
    bad_judge = _dc.replace(
        _entry("bad-judge"),
        judges=(
            JudgeSpec(
                name="pyjudge",
                mode=JudgeMode.PYTHON,
                body="also not a path",
                severity=_Sev("warning"),
            ),
        ),
    )
    inline_judge = _dc.replace(
        _entry("inline-judge"),
        judges=(
            JudgeSpec(
                name="crit",
                mode=JudgeMode.INLINE,
                body="stays on topic (free text, never a path)",
                severity=_Sev("warning"),
            ),
        ),
    )
    draft.entries = [good_pred, bad_pred, bad_judge, inline_judge]
    warns = _codes(draft)["dotted_path_malformed"]
    assert len(warns) == 2
    assert all(w.severity == "warning" for w in warns)
    joined = " ".join(w.message for w in warns)
    assert "bad-pred" in joined and "bad-judge" in joined
    # SECURITY: the message points at the runtime auditor, not an import.
    assert all("zicato board audit" in w.message for w in warns)
    # Dot-form paths are fine too.
    dot_pred = _dc.replace(
        _entry("dot-pred"),
        expectation=Expectation(kind=ExpectationKind.PREDICATE, spec="pkg.mod.check"),
    )
    draft.entries = [dot_pred]
    assert "dotted_path_malformed" not in _codes(draft)


def test_validate_rubric_spec_invalid() -> None:
    import dataclasses as _dc
    import json as _json

    from zicato.core.types import Expectation, ExpectationKind

    def rubric_entry(eid: str, spec: str):
        return _dc.replace(
            _entry(eid),
            expectation=Expectation(kind=ExpectationKind.RUBRIC, spec=spec),
        )

    draft = TournamentDraft()
    good = _json.dumps({"rubric": "clear answer", "threshold": 7, "scale": [0, 10]})
    draft.entries = [
        rubric_entry("good", good),
        rubric_entry("not-json", "not json {"),
        rubric_entry("no-rubric", _json.dumps({"threshold": 7})),
        rubric_entry("bad-threshold", _json.dumps({"rubric": "x", "threshold": "high"})),
        rubric_entry("bad-scale", _json.dumps({"rubric": "x", "scale": [1, 2, 3]})),
    ]
    warns = _codes(draft)["rubric_spec_invalid"]
    assert len(warns) == 4
    assert all(w.severity == "warning" for w in warns)
    named = " ".join(w.message for w in warns)
    assert "good" not in named


def test_validate_json_schema_spec_invalid() -> None:
    import dataclasses as _dc
    import json as _json

    from zicato.core.types import Expectation, ExpectationKind

    def schema_entry(eid: str, spec: str):
        return _dc.replace(
            _entry(eid),
            expectation=Expectation(kind=ExpectationKind.JSON_SCHEMA, spec=spec),
        )

    draft = TournamentDraft()
    draft.entries = [
        schema_entry("good", _json.dumps({"type": "object"})),
        schema_entry("good-bool", "true"),
        schema_entry("broken", "{nope"),
        schema_entry("wrong-shape", _json.dumps([1, 2])),
    ]
    warns = _codes(draft)["json_schema_spec_invalid"]
    assert len(warns) == 2
    assert all(w.severity == "warning" for w in warns)
    named = " ".join(w.message for w in warns)
    assert "broken" in named and "wrong-shape" in named


def test_validate_entry_budget_outlier() -> None:
    import dataclasses as _dc

    draft = TournamentDraft()
    draft.entries = [
        _entry("a"),
        _entry("b"),
        _entry("c"),
        _dc.replace(_entry("huge"), wall_clock_budget_seconds=6000),
    ]
    warns = _codes(draft)["entry_budget_outlier"]
    assert len(warns) == 1
    assert warns[0].severity == "info"
    assert "huge" in warns[0].message
    # 10x the median exactly is NOT an outlier (strictly greater fires).
    draft.entries = [
        _entry("a"),
        _entry("b"),
        _dc.replace(_entry("edge"), wall_clock_budget_seconds=600),
    ]
    assert "entry_budget_outlier" not in _codes(draft)


def test_validate_judge_only_board() -> None:
    draft = TournamentDraft()
    draft.entries = _board(2)
    assert "judge_only_board" not in _codes(draft)
    ops.set_board_meta(draft, judge_only=True)
    warns = _codes(draft)["judge_only_board"]
    assert len(warns) == 1
    assert warns[0].severity == "info"


def test_validate_clean_board_fires_no_authoring_codes() -> None:
    draft = TournamentDraft()
    draft.entries = _board(4)
    codes = set(_codes(draft))
    assert not codes & {
        "duplicate_entry_id",
        "entry_id_unsafe",
        "dotted_path_malformed",
        "rubric_spec_invalid",
        "json_schema_spec_invalid",
        "entry_budget_outlier",
        "judge_only_board",
    }


# ---------------------------------------------------------------------------
# DraftStore undo history: remember / pop_undo
# ---------------------------------------------------------------------------


def test_draftstore_remember_dedups_and_pop_undo_restores(tmp_path) -> None:
    from zicato.contract_draft.draft import DraftStore

    _seed_min_workspace(tmp_path)
    ws = tmp_path / ".zicato"
    store = DraftStore()
    draft = store.get("s", ws)

    # Nothing remembered yet: nothing to undo.
    assert store.pop_undo("s") is None

    store.remember("s")  # pre-op snapshot (gauntlet state)
    ops.set_experimental(draft, tournament_structures=True)
    ops.set_structure(draft, "swiss")
    store.remember("s")  # a second, distinct snapshot
    store.remember("s")  # dedup: identical to the top — records nothing
    ops.set_gate(draft, promote_margin=0.09)

    snap = store.pop_undo("s")
    assert snap is not None
    assert snap.scoring.tournament_structure.structure == "swiss"
    assert snap.scoring.promote_margin != 0.09

    ops.restore_draft(draft, snap, op="undo")
    assert draft.scoring.tournament_structure.structure == "swiss"
    assert draft.scoring.promote_margin == 0.01

    # The next pop skips snapshots equal to the restored current state and
    # hands back the original gauntlet draft.
    snap2 = store.pop_undo("s")
    assert snap2 is not None
    assert snap2.scoring.tournament_structure.structure == "gauntlet"
    ops.restore_draft(draft, snap2, op="undo")
    assert store.pop_undo("s") is None


def test_draftstore_history_is_bounded_to_twenty(tmp_path) -> None:
    from zicato.contract_draft.draft import DraftStore

    _seed_min_workspace(tmp_path)
    ws = tmp_path / ".zicato"
    store = DraftStore()
    draft = store.get("s", ws)

    for i in range(30):
        store.remember("s")
        ops.set_gate(draft, promote_margin=0.01 + (i + 1) * 0.001)

    popped = 0
    while store.pop_undo("s") is not None:
        # Restore nothing — just drain; every popped snapshot differs from
        # the (untouched) current draft, so the drain counts the history.
        popped += 1
        break_guard = popped > 25
        assert not break_guard
    # deque(maxlen=20): the oldest ten snapshots fell off.
    # (pop_undo drains one per call above; count the rest.)
    remaining = 0
    while store.pop_undo("s") is not None:
        remaining += 1
    assert popped + remaining <= 20


def test_draftstore_undo_restores_in_place_keeps_slot_binding(tmp_path) -> None:
    """The restore is IN PLACE: a session bound to a named slot stays bound,
    and the slot itself sees the restored state."""
    from zicato.contract_draft.draft import DraftStore

    _seed_min_workspace(tmp_path)
    ws = tmp_path / ".zicato"
    store = DraftStore()
    store.get("s", ws)
    forked = store.fork("s", "variant", ws)

    store.remember("s")
    ops.set_structure(forked, "racing")
    snap = store.pop_undo("s")
    assert snap is not None
    ops.restore_draft(store.get("s", ws), snap, op="undo")

    # Identity intact: the slot IS the session draft, and it was restored.
    assert store.slot("variant") is store.get("s", ws)
    assert store.slot("variant").scoring.tournament_structure.structure == "gauntlet"


def _seed_min_workspace(tmp_path) -> None:
    """A minimal registered workspace the DraftStore can init drafts from."""
    import json as _json

    from zicato.core.types import ScoringWeights as _SW
    from zicato.epoch.lifecycle import new_epoch as _new_epoch
    from zicato.workspace.config_io import write_workspace_config as _wcfg

    ws = tmp_path / ".zicato"
    ws.mkdir(exist_ok=True)
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "budget_s": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(_json.dumps({"promote_margin": 0.01}), encoding="utf-8")
    _wcfg(
        ws,
        {
            "instance_id": "default",
            "adk_entrypoint": "pkg.mod:agent",
            "mutable_trees": [],
            "source_roots": [],
            "contract": {
                "board_path": str(board.resolve()),
                "rubric_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )
    _new_epoch(
        workspace_root=ws,
        name="alpha",
        board_source=board,
        brief_source=brief,
        weights=_SW(),
        entrypoint="pkg.mod:agent",
    )
