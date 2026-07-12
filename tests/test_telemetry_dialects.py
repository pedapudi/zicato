"""Tests for telemetry dialects (TELEMETRY-DIALECTS.md).

``LossProfile`` is the convergence point; a *dialect* is a named producer
that turns a run's raw telemetry into the ``LossProfile`` inputs. These
tests pin:

* the ``adk_events`` known-answer reduction — a hand-written event log
  fixture reduces to EXACT, hand-computable numbers;
* the ``transcript`` floor tier — the explicit zero-drift degrade
  decision (drift term structurally 0), predicates still drive pass/fail;
* malformed-line tolerance (counted + surfaced, never a crash);
* determinism (re-reduce → byte-identical ``LossProfile``);
* the config-validation story (fail-fast on an unknown dialect name;
  recommend-only capability warnings);
* contract pinning (the dialect rolls the epoch both directions, and is
  omitted-at-default so existing hashes are untouched).

The ``goldfive`` default byte-identity is proven by the whole rest of the
suite staying green (``test_telemetry_reducer*`` etc.) plus the
``tools/parity.sh`` CONTRACT-HASH / MOCK-GOLDEN gates.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from zicato.core import (
    DIALECT_ADK_EVENTS,
    DIALECT_GOLDFIVE,
    DIALECT_TRANSCRIPT,
    KNOWN_TELEMETRY_DIALECTS,
    BoardEntry,
    ExpectationResult,
    ScoringWeights,
    UserPersona,
)
from zicato.telemetry.dialects import (
    dialect_capability_warnings,
    reduce_adk_events,
)
from zicato.telemetry.reducer import reduce_loss


def _write_jsonl(path: Path, lines: list[dict], *, trailing_garbage: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
        if trailing_garbage is not None:
            f.write(trailing_garbage + "\n")


def _single_turn_entry(entry_id: str = "e1") -> BoardEntry:
    return BoardEntry(id=entry_id, kind="single_turn", wall_clock_budget_seconds=60, input="hi")


# The canonical adk_events fixture used across several tests. Hand-computed
# in the docstring of ``test_adk_events_known_answer``.
_ADK_FIXTURE: list[dict] = [
    {"type": "run_start", "run_id": "run-adk-1", "session_id": "sess-adk-1"},
    {"type": "tool_call", "tool": "search", "args": {"q": "revenue"}},
    {"type": "tool_response", "tool": "search", "status": "ok"},
    {"type": "tool_call", "tool": "fetch", "args": {"url": "http://a"}},
    {"type": "tool_response", "tool": "fetch", "status": "error"},
    {"type": "tool_call", "tool": "fetch", "args": {"url": "http://a"}},  # retry loop
    {"type": "tool_response", "tool": "fetch", "status": "error"},
    {"type": "tool_call", "tool": "summarize", "args": {"n": 3}},
    {"type": "tool_response", "tool": "summarize", "status": "ok"},
    {"type": "agent_transfer", "from": "coordinator", "to": "specialist"},
    {"type": "error", "message": "unhandled exception in specialist"},
    {"type": "model_usage", "input_tokens": 100, "output_tokens": 50},
    {"type": "unknown_future_event", "foo": "bar"},  # skipped
]


# ---------------------------------------------------------------------------
# adk_events — known-answer reduction
# ---------------------------------------------------------------------------


def test_adk_events_known_answer(tmp_path: Path) -> None:
    """Exact, hand-computable numbers from the adk_events fixture.

    Signal table (§3.2), all under default weights (info=1, warning=3,
    critical=10; per_kind all 1):

    * tool_call = 4 (search, fetch, fetch, summarize) → task_started
    * tool_response error = 2 → task_failed → ratio 2/4 = 0.5
    * error event = 1 → DriftCount(tool_error, critical, 1) → 10*1*1 = 10
    * retry loop (fetch/{url:a} repeats) = 1 → DriftCount(looping_tool_call,
      warning, 1) → 3*1*1 = 3
    * agent_transfer = 1 → DriftCount(agent_transfer, info, 1) → 1*1*1 = 1
    * model_usage = 1 → llm_call_count 1; tokens 100+50 = 150
    * unknown event → skipped; trailing garbage line → malformed (skipped)

    drift_loss = (10 + 3 + 1) + 0.5*plan_rev(0) + 10.0*0.5 + 0.0*runtime
               = 14 + 5.0 = 19.0
    """
    events = tmp_path / "adk.jsonl"
    _write_jsonl(events, _ADK_FIXTURE, trailing_garbage="this is not json")
    entry = _single_turn_entry()
    weights = ScoringWeights(telemetry_dialect=DIALECT_ADK_EVENTS)

    lp = reduce_loss(events, entry, "gen1", "ep1", None, 1234, False, weights)

    assert [(c.kind, c.severity, c.count) for c in lp.drift_counts] == [
        ("agent_transfer", "info", 1),
        ("looping_tool_call", "warning", 1),
        ("tool_error", "critical", 1),
    ]
    assert lp.task_failure_ratio == 0.5
    assert lp.plan_revisions == 0
    assert lp.drift_loss == 19.0
    assert lp.tokens_spent == 150
    assert lp.output_chars == 0
    assert lp.schema_failures == 0
    assert lp.pass_fail is None
    assert lp.run_id == "run-adk-1"
    assert lp.adk_session_id == "sess-adk-1"
    assert lp.per_judge_loss == ()

    metric = {(m.name, m.count) for m in lp.metric_counts}
    assert ("drift:tool_error", 1.0) in metric
    assert ("drift:looping_tool_call", 1.0) in metric
    assert ("drift:agent_transfer", 1.0) in metric
    assert ("cost:llm_calls", 1.0) in metric
    assert ("cost:tokens_spent", 150.0) in metric


def test_adk_events_per_kind_weight_applies(tmp_path: Path) -> None:
    """adk drift folds through the SAME per_kind_weights knob as goldfive."""
    events = tmp_path / "adk.jsonl"
    _write_jsonl(events, _ADK_FIXTURE)
    entry = _single_turn_entry()
    # Double the tool_error kind: critical 10 * 2 * 1 = 20 (was 10) → +10.
    weights = ScoringWeights(
        telemetry_dialect=DIALECT_ADK_EVENTS, per_kind_weights={"tool_error": 2.0}
    )
    lp = reduce_loss(events, entry, "g", "e", None, 0, False, weights)
    assert lp.drift_loss == 29.0  # 19.0 + extra 10 from the doubled tool_error


def test_adk_events_retry_loop_ignores_arg_key_order(tmp_path: Path) -> None:
    """A retry is detected regardless of arg key ordering (determinism §6)."""
    events = tmp_path / "adk.jsonl"
    _write_jsonl(
        events,
        [
            {"type": "tool_call", "tool": "f", "args": {"a": 1, "b": 2}},
            {"type": "tool_call", "tool": "f", "args": {"b": 2, "a": 1}},  # same, reordered
        ],
    )
    sig = reduce_adk_events(events, _single_turn_entry())
    assert [(c.kind, c.count) for c in sig.drift_counts] == [("looping_tool_call", 1)]


def test_adk_events_malformed_lines_counted_not_fatal(tmp_path: Path) -> None:
    events = tmp_path / "adk.jsonl"
    with open(events, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "error"}) + "\n")
        f.write("{not json\n")
        f.write("42\n")  # valid JSON but not an object → malformed
        f.write("\n")  # blank → skipped, not counted
        f.write(json.dumps({"type": "agent_transfer"}) + "\n")
    sig = reduce_adk_events(events, _single_turn_entry())
    assert sig.malformed_line_count == 2
    assert sig.warnings and "malformed" in sig.warnings[0]
    # The well-formed events on either side of the garbage still reduced.
    kinds = {c.kind for c in sig.drift_counts}
    assert kinds == {"tool_error", "agent_transfer"}


def test_adk_events_token_shapes_tolerated(tmp_path: Path) -> None:
    events = tmp_path / "adk.jsonl"
    _write_jsonl(
        events,
        [
            {"type": "model_usage", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"type": "model_usage", "total_tokens": 7},  # fallback single count
        ],
    )
    sig = reduce_adk_events(events, _single_turn_entry())
    assert sig.llm_call_count == 2
    assert sig.token_count == 22  # 15 (nested) + 7 (total)


def test_adk_events_missing_file(tmp_path: Path) -> None:
    sig = reduce_adk_events(tmp_path / "nope.jsonl", _single_turn_entry())
    assert sig.drift_counts == ()
    assert sig.malformed_line_count == 0


# ---------------------------------------------------------------------------
# transcript — the floor tier / zero-drift degrade decision
# ---------------------------------------------------------------------------


def test_transcript_known_answer_zero_drift(tmp_path: Path) -> None:
    """The floor: no drift, predicates drive pass/fail, output_chars only."""
    events = tmp_path / "transcript.jsonl"
    _write_jsonl(
        events,
        [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."},
        ],
    )
    entry = _single_turn_entry()
    weights = ScoringWeights(telemetry_dialect=DIALECT_TRANSCRIPT)
    er = ExpectationResult(kind="predicate", passed=True)

    lp = reduce_loss(events, entry, "g", "e", er, 500, False, weights)

    # Explicit zero-drift stance (§4.1): the drift term is structurally 0.
    assert lp.drift_counts == ()
    assert lp.drift_loss == 0.0
    assert lp.task_failure_ratio == 0.0
    assert lp.tokens_spent == 0
    assert lp.output_chars == len("The capital of France is Paris.") == 31
    # Predicates still fully drive the outcome side of the scalar.
    assert lp.pass_fail is True
    assert lp.score == 1.0
    metric = {(m.name, m.count) for m in lp.metric_counts}
    assert ("output:chars", 31.0) in metric
    assert ("cost:llm_calls", 0.0) in metric  # always emitted, even at zero


def test_transcript_drift_weights_do_not_alter_scalar(tmp_path: Path) -> None:
    """Drift weights are inert under transcript — the scalar is unmoved."""
    events = tmp_path / "t.jsonl"
    _write_jsonl(events, [{"role": "assistant", "content": "hello world"}])
    entry = _single_turn_entry()
    er = ExpectationResult(kind="predicate", passed=False)
    base = reduce_loss(
        events, entry, "g", "e", er, 0, False, ScoringWeights(telemetry_dialect=DIALECT_TRANSCRIPT)
    )
    heavy = reduce_loss(
        events,
        entry,
        "g",
        "e",
        er,
        0,
        False,
        ScoringWeights(
            telemetry_dialect=DIALECT_TRANSCRIPT,
            drift_weight=99.0,
            per_kind_weights={"tool_error": 100.0},
        ),
    )
    assert base.drift_loss == heavy.drift_loss == 0.0


def test_transcript_multi_turn_features_still_work(tmp_path: Path) -> None:
    """Memory/context FEATURES (not loss) survive on the floor tier."""
    events = tmp_path / "t.jsonl"
    repeated = "the meeting is scheduled for three in the afternoon on tuesday"
    _write_jsonl(
        events,
        [
            {"role": "user", "content": "when is the meeting?"},
            {"role": "assistant", "content": repeated},
            {"role": "user", "content": "and where?"},
            {"role": "assistant", "content": repeated},  # verbatim repeat
        ],
    )
    entry = BoardEntry(
        id="mt",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=120,
        user_persona=UserPersona(goal="ask", constraints="", stop_when="done"),
        max_turns=6,
    )
    weights = ScoringWeights(telemetry_dialect=DIALECT_TRANSCRIPT)
    lp = reduce_loss(events, entry, "g", "e", None, 0, False, weights)
    assert lp.turns_completed == 2
    assert lp.memory_failure_count == 1  # the verbatim repeat
    assert lp.drift_loss == 0.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", [DIALECT_ADK_EVENTS, DIALECT_TRANSCRIPT])
def test_reduction_is_deterministic(tmp_path: Path, dialect: str) -> None:
    events = tmp_path / "e.jsonl"
    if dialect == DIALECT_ADK_EVENTS:
        _write_jsonl(events, _ADK_FIXTURE, trailing_garbage="garbage")
    else:
        _write_jsonl(events, [{"role": "assistant", "content": "x" * 50}])
    entry = _single_turn_entry()
    weights = ScoringWeights(telemetry_dialect=dialect)
    a = reduce_loss(events, entry, "g", "e", None, 1234, False, weights)
    b = reduce_loss(events, entry, "g", "e", None, 1234, False, weights)
    assert asdict(a) == asdict(b)


# ---------------------------------------------------------------------------
# Config-validation story (warn-or-refuse)
# ---------------------------------------------------------------------------


def test_unknown_dialect_refused_fail_fast() -> None:
    with pytest.raises(ValueError, match="telemetry_dialect must be one of"):
        ScoringWeights(telemetry_dialect="not_a_dialect")


def test_known_dialects_construct() -> None:
    for name in KNOWN_TELEMETRY_DIALECTS:
        assert ScoringWeights(telemetry_dialect=name).telemetry_dialect == name


def test_capability_warnings_goldfive_silent() -> None:
    assert dialect_capability_warnings(ScoringWeights()) == ()
    assert dialect_capability_warnings(ScoringWeights(telemetry_dialect=DIALECT_GOLDFIVE)) == ()


def test_capability_warnings_transcript_flags_drift_knobs() -> None:
    warns = dialect_capability_warnings(
        ScoringWeights(
            telemetry_dialect=DIALECT_TRANSCRIPT,
            drift_weight=5.0,
            per_kind_weights={"tool_error": 2.0},
            per_judge_weights={"quality": 2.0},
        )
    )
    joined = " ".join(warns)
    assert "drift_weight" in joined
    assert "per_kind_weights" in joined
    assert "per_judge_weights" in joined


def test_capability_warnings_adk_flags_only_judge_weights() -> None:
    # adk CAN produce drift, so per_kind_weights is fine; but it carries no
    # process judgements, so per_judge_weights is inert.
    warns = dialect_capability_warnings(
        ScoringWeights(
            telemetry_dialect=DIALECT_ADK_EVENTS,
            per_kind_weights={"tool_error": 2.0},
            per_judge_weights={"quality": 2.0},
        )
    )
    joined = " ".join(warns)
    assert "per_judge_weights" in joined
    assert "per_kind_weights" not in joined


# ---------------------------------------------------------------------------
# Contract pinning — the dialect rolls the epoch, omitted-at-default
# ---------------------------------------------------------------------------


def test_dialect_omitted_at_default_in_scoring_canon() -> None:
    """Default 'goldfive' is omitted from the canonical scoring dict."""
    from zicato.epoch.contract import scoring_to_canon

    canon = scoring_to_canon(ScoringWeights())
    assert "telemetry_dialect" not in canon


def test_non_default_dialect_pins_the_contract_both_directions() -> None:
    from zicato.epoch.contract import scoring_to_canon

    default_canon = scoring_to_canon(ScoringWeights())
    adk_canon = scoring_to_canon(ScoringWeights(telemetry_dialect=DIALECT_ADK_EVENTS))
    # A non-default dialect reintroduces the key and changes the canon.
    assert adk_canon.get("telemetry_dialect") == DIALECT_ADK_EVENTS
    assert adk_canon != default_canon
    # Reverting to goldfive restores the byte-identical canonical form.
    reverted_canon = scoring_to_canon(ScoringWeights(telemetry_dialect=DIALECT_GOLDFIVE))
    assert reverted_canon == default_canon


def test_dialect_round_trips_through_scoring_serde() -> None:
    w = ScoringWeights(telemetry_dialect=DIALECT_ADK_EVENTS)
    assert ScoringWeights.from_json(w.to_json()).telemetry_dialect == DIALECT_ADK_EVENTS
