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
    (ws / "config.json").write_text(json.dumps({"instance_id": "default"}), encoding="utf-8")
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
    (ws / "config.json").write_text(json.dumps({"instance_id": "default"}), encoding="utf-8")
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
                "adapter": {
                    "kind": "import",
                    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
                },
                "mutable_trees": [str(example_dir / "agent")],
                "runtime": {
                    "harness_call_llm": "zicato_examples.target_0_convergence.mocks:harness_llm",
                    "auxiliary_call_llm": "zicato_examples.target_0_convergence.mocks:aux_llm",
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
    from zicato.orchestrator import _ensure_baseline_snapshot

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


def test_set_namespace_weights() -> None:
    import pytest

    draft = TournamentDraft()
    weights = {"drift:": 2.0, "rubric:": -0.5, "cost:": 0.0}
    patch = ops.set_namespace_weights(draft, namespace_weights=weights, diff_complexity_weight=0.01)
    assert dict(draft.scoring.namespace_weights) == weights
    assert draft.scoring.diff_complexity_weight == 0.01
    assert patch.changed["namespace_weights"]["to"] == weights
    assert patch.changed["diff_complexity_weight"] == {"from": 0.0, "to": 0.01}

    # No-op replacement records nothing.
    patch2 = ops.set_namespace_weights(draft, namespace_weights=dict(weights))
    assert patch2.changed == {}

    with pytest.raises(ValueError, match=">= 0"):
        ops.set_namespace_weights(TournamentDraft(), diff_complexity_weight=-0.1)


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
