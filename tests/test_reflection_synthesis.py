"""WS-SYNTH — episode → suggestion synthesis (EVAL-SYNTHESIS.md §3 / §4).

Per-synthesizer known-answers over real-shaped episodes (reusing the miner's
extractors so the input is exactly what WS-MINE emits), the loader round-trip
pin (every drafted entry / judge survives the REAL board loader), the
scripted-LLM coverage / judge paths incl. the malformed-response drop,
provenance + target-set stamping per type, and determinism / order-independence.
No live calls — the aux seam is exercised with scripted callables only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from goldfive import DriftSeverity

from zicato.board.jsonl import load_board, save_board
from zicato.core.board import (
    BoardEntry,
    Expectation,
    ExpectationKind,
    JudgeMode,
    JudgeSpec,
    ScriptedTurn,
)
from zicato.reflection import mining as m
from zicato.reflection import synthesis as s
from zicato.reflection.adjudicator import VERDICT_FN, VERDICT_FP, JudgeAdjudication
from zicato.reflection.corpus import ObservationRun
from zicato.tournament.detail import HypothesisGrade, MovementGrade

# ---------------------------------------------------------------------------
# real-shaped fixture builders
# ---------------------------------------------------------------------------


def _obs(**kw: object) -> ObservationRun:
    base: dict[str, object] = dict(
        reflection_id="mining",
        candidate_id="v3",
        entry_id="login",
        replicate=0,
        scalar=1.0,
        drift_loss=0.0,
        pass_fail=True,
        runtime_ms=10,
        aborted=False,
        abort_cause=None,
        fidelity="verbatim",
        has_result=False,
        has_judge_io=False,
        loss_ref=None,
        transcript_ref=None,
    )
    base.update(kw)
    return ObservationRun(**base)  # type: ignore[arg-type]


def _adj(judge: str, run_ref: str, verdict: str) -> JudgeAdjudication:
    return JudgeAdjudication(
        judge_name=judge,
        run_ref=run_ref,
        observed="fired" if verdict == VERDICT_FP else "silent",
        adjudicated="should_be_silent" if verdict == VERDICT_FP else "should_fire",
        verdict=verdict,
        severity_match=None,
        evidence_span=f"span-{run_ref}",
        meta_judge_rationale="rationale",
        meta_judge_model="meta-model",
        adjudicator_self_agreement=None,
        operator_confirmed=None,
        fidelity="verbatim",
        prompt_version=2,
        k_adj=1,
    )


def _single(
    id_: str = "login", *, budget: int = 30, input_: str = "Log in with user 5"
) -> BoardEntry:
    return BoardEntry(
        id=id_,
        kind="single_turn",
        wall_clock_budget_seconds=budget,
        input=input_,
        expectation=Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="welcome"),
    )


def _with_judge(entry: BoardEntry, judge: JudgeSpec) -> BoardEntry:
    import dataclasses

    return dataclasses.replace(entry, judges=(judge,))


_TONE = JudgeSpec(
    name="tone",
    mode=JudgeMode.INLINE,
    body="The agent stays polite.",
    severity=DriftSeverity.WARNING,
)


def _failure_episode(entry_id: str = "login") -> m.MinedEpisode:
    (ep,) = m.failure_episodes([_obs(entry_id=entry_id, pass_fail=False, loss_ref="/w/a")])
    return ep


def _dead_episode(entry_id: str = "login", pairs: int = 4) -> m.MinedEpisode:
    eps = m.staleness_episodes(
        dead_entries=[{"entry_id": entry_id, "discrimination_pairs": pairs}], gap_findings=[]
    )
    return eps[0]


def _fp_episode(judge: str = "tone") -> m.MinedEpisode:
    (ep,) = m.judge_disagreement_episodes([_adj(judge, "v3:login", VERDICT_FP)])
    return ep


def _fn_episode(judge: str = "cite") -> m.MinedEpisode:
    (ep,) = m.judge_disagreement_episodes([_adj(judge, "v3:login", VERDICT_FN)])
    return ep


def _coverage_episode(mutation: str = "prompt_a") -> m.MinedEpisode:
    eps = m.coverage_gap_episodes(
        mutation_churn={mutation: ["v1", "v2"]}, discriminating_entries=0, dead_entries=[]
    )
    return eps[0]


def _scripted(fn):
    async def _cb(system: str, user: str, third: str) -> str:
        return fn(system, user, third)

    return _cb


# ---------------------------------------------------------------------------
# (a) regression entries
# ---------------------------------------------------------------------------


def test_regression_pins_failing_entry() -> None:
    board = [_single("login")]
    (sug,) = s.synthesize_mechanical([_failure_episode("login")], board_entries=board)
    assert sug.suggestion_type == s.SUGGESTION_REGRESSION_ENTRY
    assert sug.synthesizer == s.SYNTH_MECHANICAL
    assert sug.target_slice == s.TARGET_TRAIN  # §4 exception: regression may target train
    assert sug.entry is not None
    assert sug.entry.id == "login__regression_predicate_miss"
    # expectation is the ORIGINAL's, unchanged — the pin is derived, not invented
    assert sug.entry.expectation == board[0].expectation
    assert sug.entry.input == board[0].input
    assert "regression" in sug.entry.tags and "synthesized" in sug.entry.tags


def test_holdout_landing_forces_rotation_entry_into_holdout() -> None:
    # SHOULD-FIX-A: a rotation-typed entry gets a suffixed id until split_board
    # places it in holdout — so admission's leakage check sees a real holdout.
    from zicato.board.split import split_board
    from zicato.core.scoring_config import OverfittingConfig

    board = [_single(f"e{i}") for i in range(8)]
    entry = _single("cov__x")
    cfg = OverfittingConfig(enabled=True, holdout_fraction=0.5, min_board_size_for_split=2)
    landed_entry, landed = s._search_holdout_id(entry, board, cfg, seed="ep1")
    assert landed is True
    assert landed_entry.id != "cov__x"  # a suffixed id was needed
    _train, holdout = split_board([*board, landed_entry], cfg, seed="ep1")
    assert landed_entry.id in holdout


def test_holdout_landing_degrades_when_it_cannot_land(tmp_path: Path) -> None:
    # Overfitting disabled ⇒ no holdout ever ⇒ keep the base id + an honest note,
    # so admission's LEAK flag stays honest rather than false-alarming.
    from zicato.core.scoring_config import OverfittingConfig

    board = [_single(f"e{i}") for i in range(8)]
    entry = _single("cov__x")
    off = OverfittingConfig(enabled=False)
    kept, landed = s._search_holdout_id(entry, board, off, seed="ep1")
    assert landed is False
    assert kept.id == "cov__x"

    # The synthesize() post-process stamps the honest note when a rotation entry
    # cannot be forced into holdout (disabled overfitting on disk).
    ws = tmp_path / ".zicato"
    epoch_dir = ws / "epochs" / "ep1"
    epoch_dir.mkdir(parents=True)
    (epoch_dir / "scoring.json").write_text(
        json.dumps({"overfitting": {"enabled": False}}), encoding="utf-8"
    )
    sug = s.Suggestion(
        suggestion_id="sug-x",
        suggestion_type=s.SUGGESTION_HARDER_VARIANT,
        synthesizer=s.SYNTH_MECHANICAL,
        subject="cov__x",
        target_slice=s.TARGET_INCOMING_ROTATION,
        rationale="r",
        provenance={"source_episodes": [], "target_slice": "incoming_rotation"},
        entry=entry,
    )
    (out,) = s._land_rotation_entries([sug], board, workspace_root=ws, epoch_id="ep1")
    assert out.provenance["holdout_landing"] == "could not force holdout landing"
    assert out.entry is not None and out.entry.id == "cov__x"


def test_infra_abort_episode_seeds_no_regression() -> None:
    # SHOULD-FIX-C: an infra (endpoint-500) abort mines to a HINT_INFRA_FLAKE
    # episode the mechanical synthesiser routes to NOTHING — no false regression.
    board = [_single("login")]
    (ep,) = m.failure_episodes(
        [_obs(entry_id="login", aborted=True, abort_cause="endpoint_error", loss_ref="/w/a")]
    )
    assert ep.suggestion_hint == m.HINT_INFRA_FLAKE
    assert s.synthesize_mechanical([ep], board_entries=board) == []


def test_regression_dropped_when_source_entry_absent() -> None:
    # The failing entry is not on the current board — nothing to pin.
    assert s.synthesize_mechanical([_failure_episode("gone")], board_entries=[]) == []


def test_two_failure_classes_yield_distinct_regression_ids(tmp_path: Path) -> None:
    # One entry that failed two ways must not mint two colliding board ids.
    board = [_single("login")]
    predicate = _failure_episode("login")
    (abort,) = m.failure_episodes(
        [_obs(entry_id="login", aborted=True, abort_cause="budget_exhausted", loss_ref="/w/b")]
    )
    sugs = s.synthesize_mechanical([predicate, abort], board_entries=board)
    ids = {sug.entry.id for sug in sugs if sug.entry is not None}
    assert ids == {"login__regression_predicate_miss", "login__regression_abort"}
    # they co-exist on one board — save_board rejects duplicate ids, so this pins it
    path = tmp_path / "board.jsonl"
    save_board([sug.entry for sug in sugs if sug.entry is not None], path)
    assert len(load_board(path)) == 2


def test_regression_provenance_block_stamped() -> None:
    board = [_single("login")]
    (sug,) = s.synthesize_mechanical([_failure_episode("login")], board_entries=board)
    prov = sug.provenance
    assert prov["synth_version"] == s.SYNTH_VERSION
    assert prov["miner_version"] == m.MINER_VERSION
    assert prov["suggestion_type"] == s.SUGGESTION_REGRESSION_ENTRY
    assert prov["target_slice"] == s.TARGET_TRAIN
    assert prov["source_episodes"] == [_failure_episode("login").episode_id]
    assert prov["source_lineage_ids"] == ["v3"]
    # and it rides in the drafted entry's opaque context as a JSON string
    stamped = json.loads(sug.entry.context["synthesis_provenance"])
    assert stamped == prov


# ---------------------------------------------------------------------------
# (b) harder variants
# ---------------------------------------------------------------------------


def test_harder_variant_perturbs_dead_entry() -> None:
    board = [_single("login")]
    (sug,) = s.synthesize_mechanical([_dead_episode("login")], board_entries=board)
    assert sug.suggestion_type == s.SUGGESTION_HARDER_VARIANT
    assert sug.target_slice == s.TARGET_INCOMING_ROTATION  # §4 rotation default
    assert sug.entry is not None
    assert sug.evidence["perturbation"] in s._PERTURBATION_ORDER
    assert sug.entry.id.startswith("login__hv_")


def test_harder_variant_board_wide_gap_seeds_nothing() -> None:
    # A generalization-gap firing is board-wide rotation demand, not one entry.
    gap = m.staleness_episodes(
        dead_entries=[], gap_findings=[_GapFinding("critical", "gap widened", "v9")]
    )
    assert s.synthesize_mechanical(gap, board_entries=[_single("login")]) == []


def test_harder_variant_perturbation_choice_is_deterministic() -> None:
    board = [_single("login")]
    ep = _dead_episode("login")
    a = s.synthesize_mechanical([ep], board_entries=board)[0]
    b = s.synthesize_mechanical([ep], board_entries=board)[0]
    assert a.evidence["perturbation"] == b.evidence["perturbation"]
    assert a.suggestion_id == b.suggestion_id


def test_each_perturbation_produces_a_valid_entry() -> None:
    # Exercise every vocabulary member directly — each must round-trip.
    single = _single("e", budget=40, input_="handle 7 items")
    for kind in s._PERTURBATION_ORDER:
        assert s._perturbation_effective(single, kind)
        out = s._apply_perturbation(single, kind)
        assert out is not None
        assert s._entry_reject_reason(out) is None
    # tighten_budget on a 1s budget is a no-op (nothing to tighten)
    assert not s._perturbation_effective(_single("e", budget=1), s.PERTURB_TIGHTEN_BUDGET)
    # substitute_numeral needs a digit present
    assert not s._perturbation_effective(
        _single("e", input_="no digits here"), s.PERTURB_SUBSTITUTE_NUMERAL
    )


def test_substitute_numeral_transforms_deterministically() -> None:
    assert s._substitute_numerals("user 5 code 42") == "user 11 code 85"


def test_harder_variant_scripted_turns_entry() -> None:
    scripted = BoardEntry(
        id="chat",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=60,
        turns=(ScriptedTurn(user="say 3 things"),),
        max_turns=2,
    )
    (sug,) = s.synthesize_mechanical([_dead_episode("chat")], board_entries=[scripted])
    assert sug.entry is not None
    assert s._entry_reject_reason(sug.entry) is None


# ---------------------------------------------------------------------------
# (c) rubric revisions
# ---------------------------------------------------------------------------


def test_rubric_revision_tightens_inline_judge() -> None:
    board = [_with_judge(_single("login"), _TONE)]
    (sug,) = s.synthesize_mechanical([_fp_episode("tone")], board_entries=board)
    assert sug.suggestion_type == s.SUGGESTION_RUBRIC_REVISION
    assert sug.target_slice == s.TARGET_EXISTING_JUDGE
    assert sug.judge is not None
    assert sug.judge.name == "tone"
    assert sug.target_entry_id == "login"
    # the revision keeps the original body and appends a derived tightening
    assert sug.judge.body.startswith(_TONE.body)
    assert sug.evidence["body_before"] == _TONE.body
    assert sug.evidence["body_after"] == sug.judge.body
    assert "span-v3:login" in sug.judge.body  # the FP evidence span is cited


def test_rubric_revision_skips_python_judge() -> None:
    py_judge = JudgeSpec(
        name="tone", mode=JudgeMode.PYTHON, body="pkg.mod.fn", severity=DriftSeverity.WARNING
    )
    board = [_with_judge(_single("login"), py_judge)]
    assert s.synthesize_mechanical([_fp_episode("tone")], board_entries=board) == []


def test_rubric_revision_dropped_when_judge_absent() -> None:
    assert s.synthesize_mechanical([_fp_episode("ghost")], board_entries=[_single("login")]) == []


# ---------------------------------------------------------------------------
# LLM tier — coverage entries + new judges (scripted callables)
# ---------------------------------------------------------------------------


_GOOD_COVERAGE = json.dumps(
    {
        "id": "model-picks-this",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 45,
        "input": "Exercise the prompt_a surface with a boundary case.",
        "expectation": {"kind": "regex", "spec": "ok"},
        "tags": ["from-model"],
    }
)


def test_coverage_entry_llm_validates_and_forces_id() -> None:
    (sug,) = asyncio.run(
        s.synthesize_suggestions(
            [_coverage_episode("prompt_a")],
            board_entries=[],
            aux_call_llm=_scripted(lambda *_: _GOOD_COVERAGE),
        )
    )
    assert sug.suggestion_type == s.SUGGESTION_COVERAGE_ENTRY
    assert sug.synthesizer == s.SYNTH_LLM
    assert sug.target_slice == s.TARGET_INCOMING_ROTATION
    assert sug.entry is not None
    # the id is forced deterministic (not whatever the model proposed) + tagged
    assert sug.entry.id == "coverage__prompt_a"
    assert "coverage" in sug.entry.tags and "synthesized" in sug.entry.tags
    assert "from-model" in sug.entry.tags


def test_coverage_entry_llm_salvages_fenced_response() -> None:
    fenced = f"Here you go:\n```json\n{_GOOD_COVERAGE}\n```\nhope that helps"
    (sug,) = asyncio.run(
        s.synthesize_suggestions(
            [_coverage_episode("prompt_a")],
            board_entries=[],
            aux_call_llm=_scripted(lambda *_: fenced),
        )
    )
    assert sug.entry is not None and sug.entry.id == "coverage__prompt_a"


def test_coverage_entry_llm_drops_invalid_draft() -> None:
    # A multi_turn_scripted draft with no turns is loader-invalid → dropped.
    bad = json.dumps({"kind": "multi_turn_scripted", "wall_clock_budget_seconds": 10})
    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_coverage_episode("prompt_a")],
                board_entries=[],
                aux_call_llm=_scripted(lambda *_: bad),
            )
        )
        == []
    )


def test_coverage_entry_llm_drops_oversized_response() -> None:
    # An oversized aux response is a malfunction (a single draft is small): it is
    # capped and dropped with a reason, never parsed.
    huge = " " * (s._MAX_AUX_RESPONSE_BYTES + 1) + _GOOD_COVERAGE
    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_coverage_episode("prompt_a")],
                board_entries=[],
                aux_call_llm=_scripted(lambda *_: huge),
            )
        )
        == []
    )


def test_coverage_entry_llm_drops_unparseable() -> None:
    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_coverage_episode("prompt_a")],
                board_entries=[],
                aux_call_llm=_scripted(lambda *_: "no json anywhere"),
            )
        )
        == []
    )


_GOOD_JUDGE = json.dumps(
    {
        "name": "catches_leak",
        "body": "The agent never reveals the secret token.",
        "severity": "critical",
    }
)


def test_judge_llm_drafts_and_resolves_target_entry() -> None:
    (sug,) = asyncio.run(
        s.synthesize_suggestions(
            [_fn_episode("cite")],
            board_entries=[_single("login")],
            aux_call_llm=_scripted(lambda *_: _GOOD_JUDGE),
        )
    )
    assert sug.suggestion_type == s.SUGGESTION_JUDGE
    assert sug.synthesizer == s.SYNTH_LLM
    assert sug.judge is not None and sug.judge.name == "catches_leak"
    assert sug.judge.severity == DriftSeverity.CRITICAL
    assert sug.target_entry_id == "login"  # parsed from the run_ref "v3:login"
    assert sug.target_slice == s.TARGET_INCOMING_ROTATION


def test_judge_llm_drops_bad_name() -> None:
    bad = json.dumps({"name": "Bad Name!", "body": "x", "severity": "warning"})
    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_fn_episode("cite")], board_entries=[], aux_call_llm=_scripted(lambda *_: bad)
            )
        )
        == []
    )


def test_judge_llm_drops_unknown_severity() -> None:
    bad = json.dumps({"name": "ok_name", "body": "x", "severity": "catastrophic"})
    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_fn_episode("cite")], board_entries=[], aux_call_llm=_scripted(lambda *_: bad)
            )
        )
        == []
    )


def test_llm_call_exception_drops_not_crashes() -> None:
    def _boom(*_: object) -> str:
        raise RuntimeError("endpoint down")

    assert (
        asyncio.run(
            s.synthesize_suggestions(
                [_coverage_episode("prompt_a")],
                board_entries=[],
                aux_call_llm=_scripted(_boom),
            )
        )
        == []
    )


def test_no_aux_callable_yields_only_mechanical() -> None:
    # A coverage / FN episode with no aux callable produces no LLM suggestion.
    out = asyncio.run(
        s.synthesize_suggestions(
            [_coverage_episode("prompt_a"), _fn_episode("cite")],
            board_entries=[],
            aux_call_llm=None,
        )
    )
    assert out == []


# ---------------------------------------------------------------------------
# loader round-trip pin — every drafted entry survives the REAL board loader
# ---------------------------------------------------------------------------


def test_all_drafted_entries_round_trip_through_load_board(tmp_path: Path) -> None:
    board = [_with_judge(_single("login"), _TONE)]
    episodes = [_failure_episode("login"), _dead_episode("login")]
    mech = s.synthesize_mechanical(episodes, board_entries=board)
    cov = asyncio.run(
        s.synthesize_suggestions(
            [_coverage_episode("prompt_a")],
            board_entries=board,
            aux_call_llm=_scripted(lambda *_: _GOOD_COVERAGE),
        )
    )
    entries = [sug.entry for sug in [*mech, *cov] if sug.entry is not None]
    assert entries  # something to check
    path = tmp_path / "board.jsonl"
    save_board(entries, path)
    reloaded = load_board(path)
    assert {e.id for e in reloaded} == {e.id for e in entries}
    # the provenance block survives the full save → load round-trip intact
    for e in reloaded:
        assert "synthesis_provenance" in e.context
        assert json.loads(e.context["synthesis_provenance"])["synth_version"] == s.SYNTH_VERSION


# ---------------------------------------------------------------------------
# provenance / target-set per type + determinism
# ---------------------------------------------------------------------------


def test_target_slice_per_suggestion_type() -> None:
    board = [_with_judge(_single("login"), _TONE)]
    reg = s.synthesize_mechanical([_failure_episode("login")], board_entries=board)[0]
    hv = s.synthesize_mechanical([_dead_episode("login")], board_entries=board)[0]
    rub = s.synthesize_mechanical([_fp_episode("tone")], board_entries=board)[0]
    cov = asyncio.run(
        s.synthesize_suggestions(
            [_coverage_episode("prompt_a")],
            board_entries=board,
            aux_call_llm=_scripted(lambda *_: _GOOD_COVERAGE),
        )
    )[0]
    judge = asyncio.run(
        s.synthesize_suggestions(
            [_fn_episode("cite")],
            board_entries=board,
            aux_call_llm=_scripted(lambda *_: _GOOD_JUDGE),
        )
    )[0]
    assert reg.target_slice == s.TARGET_TRAIN
    assert hv.target_slice == s.TARGET_INCOMING_ROTATION
    assert cov.target_slice == s.TARGET_INCOMING_ROTATION
    assert judge.target_slice == s.TARGET_INCOMING_ROTATION
    assert rub.target_slice == s.TARGET_EXISTING_JUDGE


def test_output_is_deterministic_and_order_independent() -> None:
    board = [_with_judge(_single("login"), _TONE), _single("pay", input_="pay 9 now")]
    episodes = [
        _failure_episode("login"),
        _dead_episode("pay"),
        _fp_episode("tone"),
    ]
    forward = s.synthesize_mechanical(episodes, board_entries=board)
    reverse = s.synthesize_mechanical(list(reversed(episodes)), board_entries=board)
    assert [x.suggestion_id for x in forward] == [x.suggestion_id for x in reverse]


def test_suggestion_id_is_content_stable() -> None:
    board = [_single("login")]
    a = s.synthesize_mechanical([_failure_episode("login")], board_entries=board)[0]
    b = s.synthesize_mechanical([_failure_episode("login")], board_entries=board)[0]
    assert a.suggestion_id == b.suggestion_id
    assert a.suggestion_id.startswith("sug-")


def test_unresolved_claim_metric_routes_to_coverage_and_judge() -> None:
    # A drift: metric seeds a judge; a plain metric seeds a coverage entry.
    grade = HypothesisGrade(
        generation_id="v4",
        core_idea="idea",
        movements=(
            MovementGrade(
                metric_name="citations.count",
                predicted_direction="increase",
                predicted_magnitude="medium",
                actual_from=None,
                actual_to=None,
                actual_magnitude="",
                sign_match=False,
                magnitude_match=False,
                matched=False,
            ),
        ),
        predictions=1,
        matches=0,
        accuracy=0.0,
    )
    eps = m.unresolved_claim_episodes([grade])
    out = asyncio.run(
        s.synthesize_suggestions(
            eps, board_entries=[], aux_call_llm=_scripted(lambda *_: _GOOD_COVERAGE)
        )
    )
    assert out and out[0].suggestion_type == s.SUGGESTION_COVERAGE_ENTRY


def test_to_json_shape() -> None:
    board = [_with_judge(_single("login"), _TONE)]
    (sug,) = s.synthesize_mechanical([_fp_episode("tone")], board_entries=board)
    blob = sug.to_json()
    assert blob["suggestion_type"] == s.SUGGESTION_RUBRIC_REVISION
    assert blob["judge"]["name"] == "tone"
    assert blob["entry"] is None
    assert blob["provenance"]["synth_version"] == s.SYNTH_VERSION


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _GapFinding:
    def __init__(self, severity: str, summary: str, generation_id: str) -> None:
        self.severity = severity
        self.summary = summary
        self.detail = {"generation_id": generation_id}
