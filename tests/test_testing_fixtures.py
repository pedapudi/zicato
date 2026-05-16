"""Tests for :mod:`zicato.testing.fixtures`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import (
    BoardEntry,
    DriftCount,
    DriftMovementActual,
    EpochConfig,
    Expectation,
    Experiment,
    Generation,
    HypothesisSpec,
    LossProfile,
    MutationPoint,
    OutcomeRecord,
    Patch,
    Pattern,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
    ScriptedTurn,
    UserPersona,
)
from zicato.core.workspace import assert_distinct_callables
from zicato.testing.fixtures import (
    make_board_entry,
    make_drift_count,
    make_drift_movement_actual,
    make_epoch_config,
    make_expectation,
    make_experiment,
    make_generation,
    make_hypothesis_spec,
    make_loss_profile,
    make_mutation_point,
    make_outcome_record,
    make_patch,
    make_pattern,
    make_run_result,
    make_runtime_config,
    make_scoring_weights,
    make_scripted_turn,
    make_synthetic_events_jsonl,
    make_user_persona,
)

# ---------------------------------------------------------------------------
# Single-instance per-type checks: every factory builds the right type.
# ---------------------------------------------------------------------------


def test_make_expectation_default() -> None:
    e = make_expectation()
    assert isinstance(e, Expectation)
    assert e.kind == "expected_text"
    assert e.spec == "ok"
    assert e.reads == "final_output"


def test_make_user_persona_default() -> None:
    p = make_user_persona()
    assert isinstance(p, UserPersona)
    assert p.goal and p.constraints and p.stop_when


def test_make_scripted_turn_default() -> None:
    t = make_scripted_turn()
    assert isinstance(t, ScriptedTurn)
    assert t.user == "hi"


def test_make_drift_count_default() -> None:
    d = make_drift_count()
    assert isinstance(d, DriftCount)
    assert d.kind == "off_topic"
    assert d.severity == "warning"
    assert d.count == 1


def test_make_run_result_default() -> None:
    r = make_run_result()
    assert isinstance(r, RunResult)
    assert r.final_output == "ok"
    assert r.transcript == ("ok",)


def test_make_loss_profile_default() -> None:
    lp = make_loss_profile()
    assert isinstance(lp, LossProfile)
    assert lp.drift_counts == ()
    assert lp.drift_loss == 0.0
    assert lp.pass_fail is None


def test_make_hypothesis_spec_default() -> None:
    hs = make_hypothesis_spec()
    assert isinstance(hs, HypothesisSpec)
    assert len(hs.modulating) >= 1
    assert len(hs.expected_drift_movements) >= 1


def test_make_experiment_default() -> None:
    e = make_experiment()
    assert isinstance(e, Experiment)
    assert len(e.patches) == 1
    assert e.outcome is None
    # Hypothesis touches the same mutation id the single patch addresses.
    assert e.hypothesis.modulating == (e.patches[0].mutation_id,)


def test_make_mutation_point_default() -> None:
    mp = make_mutation_point()
    assert isinstance(mp, MutationPoint)
    assert mp.kind == "span"
    assert len(mp.content_hash) == 64  # sha256 hex length


def test_make_patch_default() -> None:
    p = make_patch()
    assert isinstance(p, Patch)
    assert p.op == "replace"
    assert p.new_content is not None
    assert p.new_numeric is None
    assert p.new_enum is None


def test_make_pattern_default() -> None:
    p = make_pattern()
    assert isinstance(p, Pattern)
    assert p.kind == "drift_kind_frequency"
    assert p.severity == "warning"


def test_make_scoring_weights_default() -> None:
    sw = make_scoring_weights()
    assert isinstance(sw, ScoringWeights)
    assert sw.drift_weight == 1.0
    assert sw.severity_weights["info"] == 1.0


def test_make_epoch_config_default() -> None:
    ec = make_epoch_config()
    assert isinstance(ec, EpochConfig)
    assert isinstance(ec.scoring, ScoringWeights)


def test_make_generation_default() -> None:
    g = make_generation()
    assert isinstance(g, Generation)
    assert g.id == "v0"
    assert g.parent_id is None


def test_make_drift_movement_actual_default() -> None:
    dm = make_drift_movement_actual()
    assert isinstance(dm, DriftMovementActual)
    assert dm.hypothesis_match is True


def test_make_outcome_record_default() -> None:
    o = make_outcome_record()
    assert isinstance(o, OutcomeRecord)
    assert o.tournament_decision == "promoted"


# ---------------------------------------------------------------------------
# BoardEntry: every kind validates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "single_turn",
        "multi_turn_scripted",
        "multi_turn_emulated",
        "synthetic_adversarial",
        "synthetic_clean",
    ],
)
def test_make_board_entry_every_kind_validates(kind: str) -> None:
    entry = make_board_entry(kind=kind)
    assert isinstance(entry, BoardEntry)
    entry.validate()  # must not raise


def test_make_board_entry_overrides_win() -> None:
    entry = make_board_entry(id="custom_id", wall_clock_budget_seconds=120)
    assert entry.id == "custom_id"
    assert entry.wall_clock_budget_seconds == 120


# ---------------------------------------------------------------------------
# RuntimeConfig: distinct-callable invariant is satisfied by defaults.
# ---------------------------------------------------------------------------


def test_make_runtime_config_defaults_satisfy_distinct_callables() -> None:
    rc = make_runtime_config()
    assert isinstance(rc, RuntimeConfig)
    # The workspace helper raises on identity-equal callables; the
    # factory's defaults are explicitly two DIFFERENT instances.
    assert rc.harness_call_llm is not rc.auxiliary_call_llm
    assert_distinct_callables(rc.harness_call_llm, rc.auxiliary_call_llm)


def test_make_runtime_config_caller_supplied_callables_pass_through() -> None:
    async def h(s: str, u: str, m: str) -> str:
        return "h"

    async def a(s: str, u: str, m: str) -> str:
        return "a"

    rc = make_runtime_config(harness_call_llm=h, auxiliary_call_llm=a)
    assert rc.harness_call_llm is h
    assert rc.auxiliary_call_llm is a


# ---------------------------------------------------------------------------
# make_synthetic_events_jsonl
# ---------------------------------------------------------------------------


def test_make_synthetic_events_jsonl_writes_a_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        path,
        drift_events=[("off_topic", "warning")],
        task_starts=1,
        conversation_turns=1,
    )
    assert path.exists()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # run_started + conv_started + task_started + drift + conv_ended + run_completed
    assert len(lines) >= 4

    # Each line is parseable JSON.
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_make_synthetic_events_jsonl_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "events.jsonl"
    make_synthetic_events_jsonl(path, task_starts=1, conversation_turns=0)
    assert path.exists()


def test_make_synthetic_events_jsonl_counts(tmp_path: Path) -> None:
    """Event counts respect the operator-supplied knobs."""
    path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        path,
        drift_events=[("off_topic", "info"), ("looping_reasoning", "warning")],
        plan_revisions=2,
        task_failures=3,
        task_starts=4,
        conversation_turns=2,
    )
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 1 run_started + 2 conv_started + 4 task_started + 3 task_failed +
    # 2 drift_detected + 2 plan_revised + 2 conv_ended + 1 run_completed = 17
    assert len(lines) == 17


def test_make_synthetic_events_jsonl_round_trips_when_goldfive_available(
    tmp_path: Path,
) -> None:
    """When goldfive is importable, the JSONL round-trips through replay_from_jsonl."""
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import replay_from_jsonl

    path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        path,
        drift_events=[("off_topic", "warning")],
        task_starts=1,
        conversation_turns=1,
    )
    events = replay_from_jsonl(path)
    assert len(events) >= 4
    # First and last are stable terminal markers.
    assert events[0].WhichOneof("payload") == "run_started"
    assert events[-1].WhichOneof("payload") == "run_completed"
