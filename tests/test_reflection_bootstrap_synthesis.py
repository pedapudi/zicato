"""WS-BOOT — the trajectory-bootstrap synthesis tier (TRAJECTORY-BOOTSTRAP.md §5).

Per-episode-kind known-answers over traj-a's REAL fixtures (imported through the
real :func:`import_trajectories` — never a hand-built ``ImportedTrace``), the
§5.1 multi-turn emulated mapping, the §5.2 HONESTY rules (a drift-signal episode
NEVER yields an output predicate), the loader round-trip gate (incl. the
multi-turn/emulator artifact), the LLM-tier drop paths, determinism, the
default-safe shim pin, and the §7 signature introspection guard. The aux seam is
exercised with scripted callables only — no live calls.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from zicato.board.jsonl import load_board, save_board
from zicato.core.board import BoardEntry, ExpectationKind, validate_board_entry
from zicato.reflection import synthesis as s
from zicato.reflection.mining import imported_trace_episodes
from zicato.reflection.trace_import import ImportedTrace, import_trajectories

_FIXTURES = Path(__file__).parent / "fixtures" / "trajectories"
_BOOTSTRAP_FIXTURES = Path(__file__).parent / "fixtures" / "trajectories_bootstrap"

# The five drift-signal kinds present in traj-a's real fixtures, keyed to the
# trace they occur on. adk_run trips all four drift signals; goldfive_run trips
# three; budget_run (bootstrap-owned real fixture) trips the budget blowout.
_DRIFT_KINDS = ("error_cascade", "abort_pattern", "retry_loop", "transfer_churn")


def _bootstrap(trace_dir: Path, *, aux: Any = None) -> list[s.Suggestion]:
    traces = import_trajectories(trace_dir)
    episodes = imported_trace_episodes(traces)
    return s.synthesize_bootstrap_suggestions(
        episodes, traces_by_id={t.trace_id: t for t in traces}, aux_call_llm=aux
    )


def _entries_by_signal_kind(suggestions: Sequence[s.Suggestion]) -> dict[str, list[BoardEntry]]:
    out: dict[str, list[BoardEntry]] = {}
    for sug in suggestions:
        assert sug.entry is not None
        out.setdefault(str(sug.evidence.get("signal_kind")), []).append(sug.entry)
    return out


def _scripted_aux(payload: str) -> Any:
    async def aux(system: str, user: str, ctx: str) -> str:
        return payload

    return aux


# ---------------------------------------------------------------------------
# §5.2 — per-drift-signal known answers + the HONESTY rule
# ---------------------------------------------------------------------------


def test_drift_signal_entries_carry_a_judge_and_no_expectation() -> None:
    """Each drift-signal episode → a single_turn entry, inline judge, NO expectation."""
    suggestions = _bootstrap(_FIXTURES)
    entries = _entries_by_signal_kind(suggestions)
    for kind in _DRIFT_KINDS:
        assert kind in entries, f"no bootstrap entry for {kind}"
        for entry in entries[kind]:
            # HONESTY: a drift-signal property is invisible to a RunResult matcher,
            # so the expectation is ABSENT (drift-loss-only) — never fabricated.
            assert entry.expectation is None, f"{kind} fabricated an expectation"
            # The property is carried as an inline process judge naming the failure.
            assert len(entry.judges) == 1
            assert entry.judges[0].name == f"bootstrap_{kind}"
            assert entry.judges[0].mode.value == "inline"
            assert entry.judges[0].body.strip()
            assert f"bootstrap:{kind}" in entry.tags


def test_no_drift_signal_ever_yields_an_output_predicate() -> None:
    """§5.2 core honesty invariant: a drift signal NEVER binds an output predicate."""
    suggestions = _bootstrap(_FIXTURES)
    for sug in suggestions:
        kind = str(sug.evidence.get("signal_kind"))
        if kind in _DRIFT_KINDS:
            assert sug.entry is not None
            assert (
                sug.entry.expectation is None
            ), f"drift signal {kind} produced an expectation — dishonest per §5.2"


def test_budget_blowout_binds_the_honest_structural_predicate() -> None:
    """Budget blowout → a tightened wall-clock budget + the real not_aborted predicate."""
    suggestions = _bootstrap(_BOOTSTRAP_FIXTURES)
    budget = [sug for sug in suggestions if sug.evidence.get("signal_kind") == "budget_blowout"]
    assert len(budget) == 1
    entry = budget[0].entry
    assert entry is not None
    # The ONE honest output predicate: a structural not_aborted check (not a
    # fabricated drift-absence predicate). It resolves to the shipped callable.
    assert entry.expectation is not None
    assert entry.expectation.kind == ExpectationKind.PREDICATE
    assert entry.expectation.spec == "zicato.reflection.bootstrap_predicates.not_aborted"
    assert not entry.judges  # no process judge — the budget check is structural
    # Tightened below the derived cost (120k tokens → 60 call-eq → 600s → *0.75).
    assert entry.wall_clock_budget_seconds == 450


def test_not_aborted_predicate_resolves_and_grades() -> None:
    from zicato.reflection.bootstrap_predicates import not_aborted

    class _Clean:
        aborted = False

    class _Aborted:
        aborted = True

    assert not_aborted(_Clean()) is True
    assert not_aborted(_Aborted()) is False
    assert not_aborted(object()) is True  # defensive: missing attribute → not aborted


# ---------------------------------------------------------------------------
# §5.1 — INPUT reconstruction (single-turn + multi-turn emulated mapping)
# ---------------------------------------------------------------------------


def test_single_turn_entry_input_is_the_reconstructed_opening_turn() -> None:
    suggestions = _bootstrap(_FIXTURES)
    # adk_run's opening user turn is the flight-booking request.
    entry = next(
        sug.entry
        for sug in suggestions
        if sug.entry is not None and sug.entry.id == "bootstrap__error_cascade__trace-a0be332d"
    )
    assert entry.kind == "single_turn"
    assert entry.input == "Book me a flight to Lisbon next Tuesday and add it to my calendar."
    assert entry.user_persona is None


def test_multi_turn_trace_maps_to_emulated_entry_scripted_from_recorded_user() -> None:
    """§5.1: a multi-turn trace → multi_turn_emulated, persona scripted from the user side."""
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux(json.dumps({"rubric": "answer well"})))
    multi = [
        sug.entry
        for sug in suggestions
        if sug.entry is not None and sug.entry.kind == "multi_turn_emulated"
    ]
    assert len(multi) == 1  # transcript_run.jsonl (2 user turns)
    entry = multi[0]
    assert entry.input is None
    assert entry.turns is None
    assert entry.user_persona is not None
    # max_turns = recorded user turns + 1.
    assert entry.max_turns == 3
    # The persona is scripted from the RECORDED user side (intent, not verbatim).
    persona = entry.user_persona
    assert "Summarise the attached paper" in persona.goal
    assert "replay the recorded user's turns" in persona.constraints.lower()
    # Both recorded turns ride the compact digest.
    assert "turn 1:" in persona.constraints.lower()
    assert "turn 2:" in persona.constraints.lower()
    assert "which ablation" in persona.constraints.lower()
    assert persona.stop_when


def test_neutral_opener_is_flagged_when_no_user_turn_reconstructs() -> None:
    """§5.1: an empty opener falls back to a neutral opener, flagged in provenance."""
    traces = import_trajectories(_FIXTURES)
    # Synthesise a signal episode on a trace with no user turns by clearing them.
    import dataclasses

    base = next(t for t in traces if t.source_file == "adk_run.jsonl")
    stripped = dataclasses.replace(base, signals=dataclasses.replace(base.signals, user_turns=()))
    episodes = [
        e for e in imported_trace_episodes([base]) if e.suggestion_hint == "bootstrap_entry"
    ]
    got = s.synthesize_bootstrap_suggestions(
        episodes[:1], traces_by_id={stripped.trace_id: stripped}
    )
    assert got, "expected a suggestion even with no reconstructable opener"
    entry = got[0].entry
    assert entry is not None
    assert entry.input == s._BOOTSTRAP_NEUTRAL_OPENER
    assert "synthesised_neutral_opener" in got[0].provenance.get("reconstruction_flags", [])


# ---------------------------------------------------------------------------
# §5.3 — provenance + target-slice policy
# ---------------------------------------------------------------------------


def test_provenance_carries_foreign_source_and_empty_lineage() -> None:
    suggestions = _bootstrap(_FIXTURES)
    for sug in suggestions:
        prov = sug.provenance
        fs = prov.get("foreign_source")
        assert fs is not None
        assert fs["kind"] == "trajectory_bootstrap"
        assert fs["dialect"] in ("adk_events", "goldfive", "transcript")
        assert fs["source_file"].endswith(".jsonl")
        assert fs["trace_id"].startswith("trace-")
        # Foreign trace → no generations → leakage check trivially green (§5.3).
        assert prov["source_lineage_ids"] == []
        # The train default + its §5.3 justification ride the suggestion.
        assert sug.target_slice == "train"
        assert "train" in sug.rationale
        assert fs["source_file"] in sug.rationale


# ---------------------------------------------------------------------------
# loader round-trips — the standing gate (§5.2)
# ---------------------------------------------------------------------------


def test_every_drafted_entry_round_trips_through_the_real_loader(tmp_path: Path) -> None:
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux(json.dumps({"rubric": "be correct"})))
    suggestions += _bootstrap(_BOOTSTRAP_FIXTURES)
    assert suggestions
    for sug in suggestions:
        assert sug.entry is not None
        sug.entry.validate()
        path = tmp_path / f"{sug.entry.id}.jsonl"
        save_board([sug.entry], path)
        reloaded = load_board(path)
        assert len(reloaded) == 1
        assert reloaded[0].id == sug.entry.id
        assert reloaded[0].kind == sug.entry.kind


def test_surface_draft_artifact_re_validates_as_an_add_board_entry_op() -> None:
    """The surface draft_artifact is a valid add_board_entry op payload (multi-turn incl.)."""
    from zicato.reflection.trace_import import import_trajectories as _imp

    traces = _imp(_FIXTURES)
    episodes = imported_trace_episodes(traces)
    surface = s.synthesize(episodes, imported_traces=traces, allow_llm=False)
    assert surface
    for sug in surface:
        assert sug.artifact_kind == "board_entry"
        assert sug.proposed_op is not None
        assert sug.proposed_op["op"] == "add_board_entry"
        entry_dict = sug.proposed_op["args"]["entry"]
        # The op's entry re-validates through the real board validator.
        validate_board_entry(entry_dict)

    # The multi_turn_emulated draft (behavioral tier) ALSO survives the op path —
    # the emulator persona + max_turns round-trip through the add_board_entry op.
    behavioral = _bootstrap(_FIXTURES, aux=_scripted_aux(json.dumps({"rubric": "be correct"})))
    multi = [
        sug.entry
        for sug in behavioral
        if sug.entry is not None and sug.entry.kind == "multi_turn_emulated"
    ]
    assert multi
    for entry in multi:
        op_dict = s._entry_op_dict(entry)
        rebuilt = validate_board_entry(op_dict)
        assert rebuilt.kind == "multi_turn_emulated"
        assert rebuilt.user_persona is not None
        assert rebuilt.input is None


# ---------------------------------------------------------------------------
# LLM tier drop paths (§5.2) — the aux seam degrades, never crashes
# ---------------------------------------------------------------------------


def test_behavioral_tier_needs_the_aux_seam() -> None:
    """No aux callable → the behavioral (rubric) episodes ship NOTHING."""
    suggestions = _bootstrap(_FIXTURES, aux=None)
    assert all(sug.suggestion_type != "coverage_entry" for sug in suggestions)
    assert all(sug.evidence.get("signal_kind") != "behavioral" for sug in suggestions)


def test_behavioral_tier_drops_on_non_json_response() -> None:
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux("this is not json at all"))
    assert all(sug.suggestion_type != "coverage_entry" for sug in suggestions)


def test_behavioral_tier_drops_when_rubric_key_missing() -> None:
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux(json.dumps({"notrubric": "x"})))
    assert all(sug.suggestion_type != "coverage_entry" for sug in suggestions)


def test_behavioral_tier_drops_oversized_response() -> None:
    huge = json.dumps({"rubric": "x" * (65 * 1024)})
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux(huge))
    assert all(sug.suggestion_type != "coverage_entry" for sug in suggestions)


def test_behavioral_tier_ships_a_rubric_coverage_entry_on_a_good_response() -> None:
    payload = json.dumps(
        {"rubric": "The reply must answer the question accurately.", "threshold": 0.7}
    )
    suggestions = _bootstrap(_FIXTURES, aux=_scripted_aux(payload))
    behavioral = [sug for sug in suggestions if sug.suggestion_type == "coverage_entry"]
    assert behavioral
    for sug in behavioral:
        assert sug.synthesizer == "llm"
        assert sug.target_slice == "train"
        entry = sug.entry
        assert entry is not None
        assert entry.expectation is not None
        assert entry.expectation.kind == ExpectationKind.RUBRIC
        spec = json.loads(entry.expectation.spec)
        assert spec["rubric"] == "The reply must answer the question accurately."


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_bootstrap_is_deterministic_across_reruns() -> None:
    payload = json.dumps({"rubric": "be correct", "threshold": 0.5})
    first = _bootstrap(_FIXTURES, aux=_scripted_aux(payload))
    second = _bootstrap(_FIXTURES, aux=_scripted_aux(payload))
    assert [x.suggestion_id for x in first] == [y.suggestion_id for y in second]
    assert [x.to_json() for x in first] == [y.to_json() for y in second]


# ---------------------------------------------------------------------------
# the default-safe shim pin (§7 / task 5)
# ---------------------------------------------------------------------------


def test_synthesize_without_imported_traces_is_byte_identical() -> None:
    """The extended shim with no imported_traces == today: no bootstrap suggestions."""
    traces = import_trajectories(_FIXTURES)
    episodes = imported_trace_episodes(traces)
    # Passing the SAME (bootstrap) episodes but NO imported_traces yields nothing
    # (the reconstructions are unreachable) — the default path is inert.
    without = s.synthesize(episodes)
    assert without == []
    empty = s.synthesize(episodes, imported_traces=())
    assert empty == []


def test_empty_traces_by_id_yields_no_suggestions() -> None:
    traces = import_trajectories(_FIXTURES)
    episodes = imported_trace_episodes(traces)
    assert s.synthesize_bootstrap_suggestions(episodes, traces_by_id={}) == []


# ---------------------------------------------------------------------------
# §7 — the literal seam signatures (introspection guard — the seams never drift)
# ---------------------------------------------------------------------------


def test_synthesize_bootstrap_suggestions_signature_matches_doc_section_7() -> None:
    sig = inspect.signature(s.synthesize_bootstrap_suggestions)
    params = sig.parameters
    assert list(params) == ["episodes", "traces_by_id", "aux_call_llm"]
    assert params["episodes"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["traces_by_id"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["traces_by_id"].default is inspect.Parameter.empty
    assert params["aux_call_llm"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["aux_call_llm"].default is None


def test_synthesize_shim_signature_matches_doc_section_7() -> None:
    sig = inspect.signature(s.synthesize)
    params = sig.parameters
    assert list(params) == [
        "episodes",
        "allow_llm",
        "workspace_root",
        "epoch_id",
        "imported_traces",
    ]
    assert params["allow_llm"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["allow_llm"].default is False
    assert params["imported_traces"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["imported_traces"].default == ()


def test_traces_by_id_annotation_is_a_mapping_of_imported_trace() -> None:
    hints = inspect.get_annotations(s.synthesize_bootstrap_suggestions, eval_str=True)
    assert hints["traces_by_id"] == Mapping[str, ImportedTrace]
