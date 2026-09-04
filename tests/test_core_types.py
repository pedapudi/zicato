"""Tests for the foundational dataclasses and workspace helpers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from goldfive import DriftSeverity

from zicato.core import (
    GOLDFIVE_DRIFT_KINDS,
    BoardEntry,
    DriftCount,
    Expectation,
    ExpectationKind,
    ExpectationResult,
    JudgeMode,
    JudgeSpec,
    LossProfile,
    MutationPoint,
    OutputScope,
    Patch,
    RuntimeConfig,
    ScoringWeights,
    ScriptedTurn,
    UserPersona,
    analysis_path,
    assert_distinct_callables,
    board_path,
    epoch_dir,
    events_jsonl_path,
    experiment_json_path,
    generation_dir,
    journal_path,
    lineage_path,
    loss_profile_path,
    rubric_path,
    run_dir,
    scoring_path,
    validate_board_entry,
    validate_drift_kind,
)
from zicato.core.drift_kinds import DriftSeverity as MirrorDriftSeverity

# ---------------------------------------------------------------------------
# Board-authoring enums
# ---------------------------------------------------------------------------


def test_output_scope_members_and_str_equality() -> None:
    """``OutputScope`` is a str-enum with the two documented members."""
    assert {m.value for m in OutputScope} == {"final_output", "conversation_end"}
    assert OutputScope.FINAL == "final_output"
    assert OutputScope.TRANSCRIPT == "conversation_end"


def test_expectation_kind_members_and_str_equality() -> None:
    """``ExpectationKind`` is a str-enum covering the five OUTCOME matchers."""
    assert {m.value for m in ExpectationKind} == {
        "expected_text",
        "regex",
        "json_schema",
        "predicate",
        "rubric",
    }
    assert ExpectationKind.PREDICATE == "predicate"


def test_judge_mode_members_and_str_equality() -> None:
    """``JudgeMode`` is a two-member str-enum."""
    assert {m.value for m in JudgeMode} == {"inline", "python"}
    assert JudgeMode.INLINE == "inline"


def test_expectation_defaults_reads_to_final() -> None:
    """A bare :class:`Expectation` reads the final output by default."""
    e = Expectation(kind=ExpectationKind.REGEX, spec="x")
    assert e.reads is OutputScope.FINAL


# ---------------------------------------------------------------------------
# JudgeSpec
# ---------------------------------------------------------------------------


def test_judge_spec_is_frozen() -> None:
    """:class:`JudgeSpec` is a frozen dataclass."""
    js = JudgeSpec(name="on_task", mode=JudgeMode.INLINE, body="c", severity=DriftSeverity.WARNING)
    with pytest.raises(dataclasses.FrozenInstanceError):
        js.name = "renamed"  # type: ignore[misc]


def test_judge_spec_carries_goldfive_severity() -> None:
    """``JudgeSpec.severity`` is a goldfive :class:`DriftSeverity` member."""
    js = JudgeSpec(
        name="no_pii", mode=JudgeMode.PYTHON, body="p.j.f", severity=DriftSeverity.CRITICAL
    )
    assert js.severity is DriftSeverity.CRITICAL
    assert isinstance(js.severity, DriftSeverity)


# ---------------------------------------------------------------------------
# validate_drift_kind
# ---------------------------------------------------------------------------


def test_validate_drift_kind_accepts_known_kinds() -> None:
    for kind in ["off_topic", "looping_reasoning", "tool_error", "goal_drift"]:
        assert kind in GOLDFIVE_DRIFT_KINDS
        validate_drift_kind(kind)  # must not raise


def test_validate_drift_kind_rejects_unknown_kinds() -> None:
    with pytest.raises(ValueError, match="unknown drift kind"):
        validate_drift_kind("definitely_not_a_real_drift_kind")


def test_validate_drift_kind_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        validate_drift_kind("")


def test_validate_drift_kind_is_case_sensitive() -> None:
    # goldfive emits lowercase wire-canonical strings; uppercase should fail.
    with pytest.raises(ValueError):
        validate_drift_kind("OFF_TOPIC")


# ---------------------------------------------------------------------------
# BoardEntry.validate
# ---------------------------------------------------------------------------


def _single_turn_entry(**overrides: object) -> BoardEntry:
    kwargs: dict[str, object] = dict(
        id="e1",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )
    kwargs.update(overrides)
    return BoardEntry(**kwargs)  # type: ignore[arg-type]


def test_board_entry_single_turn_valid() -> None:
    entry = _single_turn_entry()
    entry.validate()


def test_board_entry_single_turn_missing_input_rejected() -> None:
    entry = BoardEntry(id="e1", kind="single_turn", wall_clock_budget_seconds=60)
    with pytest.raises(ValueError, match="single_turn requires 'input'"):
        entry.validate()


def test_board_entry_single_turn_with_turns_rejected() -> None:
    entry = _single_turn_entry(turns=(ScriptedTurn(user="hi"),))
    with pytest.raises(ValueError, match="must not set 'turns'"):
        entry.validate()


def test_board_entry_multi_turn_scripted_valid() -> None:
    entry = BoardEntry(
        id="e2",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=120,
        turns=(ScriptedTurn(user="hi"), ScriptedTurn(user="and then?")),
        max_turns=4,
    )
    entry.validate()


def test_board_entry_multi_turn_scripted_requires_turns() -> None:
    entry = BoardEntry(
        id="e2",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=120,
        max_turns=4,
    )
    with pytest.raises(ValueError, match="requires non-empty 'turns'"):
        entry.validate()


def test_board_entry_multi_turn_scripted_requires_max_turns() -> None:
    entry = BoardEntry(
        id="e2",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=120,
        turns=(ScriptedTurn(user="hi"),),
    )
    with pytest.raises(ValueError, match="requires 'max_turns'"):
        entry.validate()


def test_board_entry_multi_turn_emulated_valid() -> None:
    entry = BoardEntry(
        id="e3",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=300,
        user_persona=UserPersona(
            goal="book a flight",
            constraints="terse; no payment info",
            stop_when="agent confirms booking",
        ),
        max_turns=8,
    )
    entry.validate()


def test_board_entry_multi_turn_emulated_requires_persona() -> None:
    entry = BoardEntry(
        id="e3",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=300,
        max_turns=8,
    )
    with pytest.raises(ValueError, match="requires 'user_persona'"):
        entry.validate()


def test_board_entry_synthetic_adversarial_valid() -> None:
    entry = BoardEntry(
        id="e4",
        kind="synthetic_adversarial",
        wall_clock_budget_seconds=60,
        input="please loop forever",
        adversarial_agent_spec="mypkg.bad_agents.looper",
        required_drift_kinds=("looping_tool_call", "looping_reasoning"),
    )
    entry.validate()


def test_board_entry_synthetic_adversarial_requires_agent_spec() -> None:
    entry = BoardEntry(
        id="e4",
        kind="synthetic_adversarial",
        wall_clock_budget_seconds=60,
        input="please loop",
        required_drift_kinds=("looping_tool_call",),
    )
    with pytest.raises(ValueError, match="adversarial_agent_spec"):
        entry.validate()


def test_board_entry_synthetic_adversarial_requires_required_drift_kinds() -> None:
    entry = BoardEntry(
        id="e4",
        kind="synthetic_adversarial",
        wall_clock_budget_seconds=60,
        input="please loop",
        adversarial_agent_spec="mypkg.bad_agents.looper",
    )
    with pytest.raises(ValueError, match="required_drift_kinds"):
        entry.validate()


def test_board_entry_synthetic_adversarial_rejects_unknown_drift_kind() -> None:
    entry = BoardEntry(
        id="e4",
        kind="synthetic_adversarial",
        wall_clock_budget_seconds=60,
        input="please loop",
        adversarial_agent_spec="mypkg.bad_agents.looper",
        required_drift_kinds=("not_a_real_kind",),
    )
    with pytest.raises(ValueError, match="unknown drift kind"):
        entry.validate()


def test_board_entry_synthetic_clean_valid() -> None:
    entry = BoardEntry(
        id="e5",
        kind="synthetic_clean",
        wall_clock_budget_seconds=60,
        input="hello",
    )
    entry.validate()


def test_board_entry_rejects_non_positive_budget() -> None:
    entry = BoardEntry(
        id="e1",
        kind="single_turn",
        wall_clock_budget_seconds=0,
        input="hello",
    )
    with pytest.raises(ValueError, match="wall_clock_budget_seconds"):
        entry.validate()


def test_board_entry_rejects_negative_weight() -> None:
    entry = _single_turn_entry(weight=-1.0)
    with pytest.raises(ValueError, match="weight"):
        entry.validate()


def test_board_entry_rejects_transcript_scope_expectation_on_single_turn() -> None:
    entry = _single_turn_entry(
        expectation=Expectation(
            kind=ExpectationKind.REGEX, spec=".*", reads=OutputScope.TRANSCRIPT
        ),
    )
    with pytest.raises(ValueError, match="transcript"):
        entry.validate()


def test_board_entry_rejects_duplicate_judge_names() -> None:
    """Two judges sharing a name on one entry is rejected."""
    js = JudgeSpec(name="dup", mode=JudgeMode.INLINE, body="c", severity=DriftSeverity.INFO)
    entry = _single_turn_entry(judges=(js, js))
    with pytest.raises(ValueError, match="duplicate judge name"):
        entry.validate()


def test_board_entry_accepts_judges() -> None:
    """A valid entry with distinct-named judges passes ``validate``."""
    entry = _single_turn_entry(
        judges=(
            JudgeSpec(
                name="on_task", mode=JudgeMode.INLINE, body="c", severity=DriftSeverity.WARNING
            ),
            JudgeSpec(
                name="no_pii", mode=JudgeMode.PYTHON, body="p.j", severity=DriftSeverity.CRITICAL
            ),
        )
    )
    entry.validate()


# ---------------------------------------------------------------------------
# validate_board_entry — JSON round-trip
# ---------------------------------------------------------------------------


def test_validate_board_entry_single_turn_round_trip() -> None:
    d = {
        "id": "e1",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 90,
        "input": "ask the agent something",
        "weight": 2.0,
        "tags": ["regression", "smoke"],
        "context": {"locale": "en-US"},
        "expectation": {
            "kind": "regex",
            "spec": "(?i)done",
        },
    }
    entry = validate_board_entry(d)
    assert entry.id == "e1"
    assert entry.kind == "single_turn"
    assert entry.input == "ask the agent something"
    assert entry.weight == 2.0
    assert entry.tags == ("regression", "smoke")
    assert entry.context == {"locale": "en-US"}
    assert entry.expectation is not None
    assert entry.expectation.kind is ExpectationKind.REGEX
    assert entry.expectation.reads is OutputScope.FINAL


def test_validate_board_entry_multi_turn_scripted_round_trip() -> None:
    d = {
        "id": "e2",
        "kind": "multi_turn_scripted",
        "wall_clock_budget_seconds": 240,
        "turns": [{"user": "hi"}, {"user": "go on"}],
        "max_turns": 6,
    }
    entry = validate_board_entry(d)
    assert entry.kind == "multi_turn_scripted"
    assert entry.turns is not None
    assert len(entry.turns) == 2
    assert entry.turns[0].user == "hi"
    assert entry.max_turns == 6


def test_validate_board_entry_multi_turn_emulated_round_trip() -> None:
    d = {
        "id": "e3",
        "kind": "multi_turn_emulated",
        "wall_clock_budget_seconds": 300,
        "user_persona": {
            "goal": "schedule a meeting",
            "constraints": "be brief",
            "stop_when": "calendar confirmed",
        },
        "max_turns": 8,
        "expectation": {
            "kind": "rubric",
            "spec": '{"rubric": "did the agent schedule cleanly?"}',
            "reads": "conversation_end",
        },
        "judges": [
            {
                "name": "no_double_booking",
                "mode": "inline",
                "body": "the agent never books an already-occupied slot",
                "severity": "critical",
            }
        ],
    }
    entry = validate_board_entry(d)
    assert entry.user_persona is not None
    assert entry.user_persona.goal == "schedule a meeting"
    assert entry.expectation is not None
    assert entry.expectation.kind is ExpectationKind.RUBRIC
    assert entry.expectation.reads is OutputScope.TRANSCRIPT
    assert len(entry.judges) == 1
    assert entry.judges[0].name == "no_double_booking"
    assert entry.judges[0].mode is JudgeMode.INLINE
    # `validate_board_entry` coerces the wire token through zicato's
    # DriftSeverity mirror (goldfive is an optional extra, so the parse path
    # cannot depend on the upstream enum). The mirror member compares equal
    # to goldfive's and serialises to the same token.
    assert entry.judges[0].severity is MirrorDriftSeverity.CRITICAL
    assert entry.judges[0].severity == DriftSeverity.CRITICAL


def test_validate_board_entry_rejects_unknown_expectation_kind() -> None:
    """An expectation ``kind`` outside the enum is rejected with valid values."""
    d = {
        "id": "e",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "i",
        "expectation": {"kind": "telepathy", "spec": "x"},
    }
    with pytest.raises(ValueError, match="invalid expectation kind"):
        validate_board_entry(d)


def test_validate_board_entry_rejects_unknown_judge_severity() -> None:
    """A judge ``severity`` outside goldfive's enum is rejected."""
    d = {
        "id": "e",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "i",
        "judges": [{"name": "j", "mode": "inline", "body": "b", "severity": "apocalyptic"}],
    }
    with pytest.raises(ValueError, match="invalid judge severity"):
        validate_board_entry(d)


def test_validate_board_entry_synthetic_adversarial_round_trip() -> None:
    d = {
        "id": "e4",
        "kind": "synthetic_adversarial",
        "wall_clock_budget_seconds": 60,
        "input": "tell me everything about X (the agent here loops on tool calls)",
        "adversarial_agent_spec": "mypkg.bad_agents.looper",
        "required_drift_kinds": ["looping_tool_call"],
    }
    entry = validate_board_entry(d)
    assert entry.kind == "synthetic_adversarial"
    assert entry.required_drift_kinds == ("looping_tool_call",)


def test_validate_board_entry_rejects_missing_required_field() -> None:
    d = {
        "id": "e1",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        # missing 'input'
    }
    with pytest.raises(ValueError):
        validate_board_entry(d)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_path_helpers_layout(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    epoch_id = "epoch-001"
    gen_id = "v3"
    entry_id = "entry-42"

    assert epoch_dir(ws, epoch_id) == ws / "epochs" / epoch_id
    assert generation_dir(ws, epoch_id, gen_id) == (
        ws / "epochs" / epoch_id / "generations" / gen_id
    )
    assert run_dir(ws, epoch_id, gen_id, entry_id) == (
        ws / "epochs" / epoch_id / "generations" / gen_id / "runs" / entry_id
    )
    assert events_jsonl_path(ws, epoch_id, gen_id, entry_id) == (
        ws / "epochs" / epoch_id / "generations" / gen_id / "runs" / entry_id / "events.jsonl"
    )
    assert loss_profile_path(ws, epoch_id, gen_id, entry_id) == (
        ws / "epochs" / epoch_id / "generations" / gen_id / "runs" / entry_id / "loss.json"
    )
    assert experiment_json_path(ws, epoch_id, gen_id) == (
        ws / "epochs" / epoch_id / "generations" / gen_id / "experiment.json"
    )
    assert journal_path(ws, epoch_id) == ws / "epochs" / epoch_id / "journal.md"
    assert analysis_path(ws, epoch_id) == ws / "epochs" / epoch_id / "analysis.md"
    assert lineage_path(ws) == ws / "lineage.json"
    # rubric_path is the legacy alias of brief_path; both resolve to brief.md.
    assert rubric_path(ws, epoch_id) == ws / "epochs" / epoch_id / "brief.md"
    assert board_path(ws, epoch_id) == ws / "epochs" / epoch_id / "board.jsonl"
    assert scoring_path(ws, epoch_id) == ws / "epochs" / epoch_id / "scoring.json"


def test_path_helpers_perform_no_io(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    # No directories created by the helpers.
    _ = run_dir(ws, "epoch-001", "v0", "entry-1")
    assert not ws.exists()


# ---------------------------------------------------------------------------
# assert_distinct_callables
# ---------------------------------------------------------------------------


async def _stub_call_llm(system: str, user: str, model: str) -> str:  # noqa: ARG001
    return ""


async def _other_call_llm(system: str, user: str, model: str) -> str:  # noqa: ARG001
    return ""


def test_assert_distinct_callables_accepts_two_distinct_callables() -> None:
    assert_distinct_callables(_stub_call_llm, _other_call_llm)


def test_assert_distinct_callables_rejects_shared_identity() -> None:
    with pytest.raises(RuntimeError, match="distinct callables"):
        assert_distinct_callables(_stub_call_llm, _stub_call_llm)


# ---------------------------------------------------------------------------
# Frozen dataclass replace works
# ---------------------------------------------------------------------------


def test_mutation_point_replace_produces_new_instance() -> None:
    point = MutationPoint(
        id="m1",
        kind="span",
        file=Path("/abs/file.py"),
        source_root=Path("/abs"),
        line_start=10,
        line_end=12,
        content="old",
        content_hash="abc",
    )
    new_point = dataclasses.replace(point, content="new", content_hash="def")
    assert new_point is not point
    assert new_point.content == "new"
    assert new_point.content_hash == "def"
    # Original is untouched.
    assert point.content == "old"
    assert point.content_hash == "abc"


def test_patch_replace_produces_new_instance() -> None:
    patch = Patch(
        id="p1",
        mutation_id="m1",
        op="replace",
        new_content="new prompt",
        new_numeric=None,
        new_enum=None,
        rationale="tighten the system prompt",
    )
    new_patch = dataclasses.replace(patch, rationale="updated rationale")
    assert new_patch is not patch
    assert new_patch.rationale == "updated rationale"
    assert patch.rationale == "tighten the system prompt"


def test_loss_profile_replace_preserves_fields() -> None:
    profile = LossProfile(
        run_id="r1",
        entry_id="e1",
        generation_id="v0",
        epoch_id="epoch-001",
        drift_counts=(DriftCount(kind="tool_error", severity="warning", count=2),),
        plan_revisions=1,
        task_failure_ratio=0.0,
        runtime_ms=1234,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="regex", passed=True, detail="matched"),
        drift_loss=6.0,
        pass_fail=True,
    )
    updated = dataclasses.replace(profile, drift_loss=4.5)
    assert updated.drift_loss == 4.5
    assert updated.drift_counts == profile.drift_counts
    assert profile.drift_loss == 6.0


def test_frozen_dataclass_blocks_attribute_assignment() -> None:
    point = MutationPoint(
        id="m1",
        kind="span",
        file=Path("/abs/file.py"),
        source_root=Path("/abs"),
        line_start=1,
        line_end=2,
        content="x",
        content_hash="h",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.content = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RuntimeConfig.parallelism
# ---------------------------------------------------------------------------


def _two_distinct_callables() -> tuple[object, object]:
    """Return two identity-unequal async LLM callables for RuntimeConfig."""

    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return harness_call, aux_call


def test_runtime_config_parallelism_defaults_to_four(tmp_path: Path) -> None:
    """``parallelism`` defaults to a modest, sane bounded-concurrency value."""
    harness_call, aux_call = _two_distinct_callables()
    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,  # type: ignore[arg-type]
        evaluation_call_llm=aux_call,  # type: ignore[arg-type]
    )
    assert config.parallelism == 4


def test_runtime_config_parallelism_accepts_one(tmp_path: Path) -> None:
    """``parallelism=1`` is valid — it selects fully sequential execution."""
    harness_call, aux_call = _two_distinct_callables()
    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,  # type: ignore[arg-type]
        evaluation_call_llm=aux_call,  # type: ignore[arg-type]
        parallelism=1,
    )
    assert config.parallelism == 1


def test_runtime_config_parallelism_accepts_high_value(tmp_path: Path) -> None:
    """An operator may raise ``parallelism`` when the LLM endpoint allows."""
    harness_call, aux_call = _two_distinct_callables()
    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,  # type: ignore[arg-type]
        evaluation_call_llm=aux_call,  # type: ignore[arg-type]
        parallelism=32,
    )
    assert config.parallelism == 32


@pytest.mark.parametrize("bad", [0, -1, -8])
def test_runtime_config_rejects_sub_one_parallelism(tmp_path: Path, bad: int) -> None:
    """``parallelism < 1`` is a construction-time ``ValueError``."""
    harness_call, aux_call = _two_distinct_callables()
    with pytest.raises(ValueError, match="parallelism must be >= 1"):
        RuntimeConfig(
            instance_id="test",
            workspace_root=tmp_path,
            target_call_llm=harness_call,  # type: ignore[arg-type]
            evaluation_call_llm=aux_call,  # type: ignore[arg-type]
            parallelism=bad,
        )


# ---------------------------------------------------------------------------
# ScoringWeights.per_judge_weights
# ---------------------------------------------------------------------------


def test_scoring_weights_per_judge_weights_defaults_empty() -> None:
    """``per_judge_weights`` defaults to an empty mapping."""
    assert ScoringWeights().per_judge_weights == {}


def test_scoring_weights_per_judge_weights_accepts_mapping() -> None:
    """``per_judge_weights`` is keyed on :attr:`JudgeSpec.name`."""
    sw = ScoringWeights(per_judge_weights={"no_pii": 3.0, "on_task": 1.0})
    assert sw.per_judge_weights["no_pii"] == 3.0
    assert sw.per_judge_weights["on_task"] == 1.0


def test_scoring_weights_per_judge_weights_replace_preserves() -> None:
    """``dataclasses.replace`` keeps ``per_judge_weights`` intact."""
    sw = ScoringWeights(per_judge_weights={"safety": 5.0})
    updated = dataclasses.replace(sw, pass_weight=2.0)
    assert updated.per_judge_weights == {"safety": 5.0}
    assert updated.pass_weight == 2.0
