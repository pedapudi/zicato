"""Tests for the generalised metric surface in :func:`reduce_loss`.

These tests cover the new namespaced :class:`MetricCount` outputs
(``cost:*``, ``output:*``, ``schema:*``, ``drift:*``) alongside the
existing :attr:`LossProfile.drift_counts` invariant. The drift-side
expectations are tested in :mod:`tests.test_telemetry_reducer`.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.core import (
    BoardEntry,
    ScoringWeights,
)
from zicato.telemetry.reducer import (
    read_loss_profile,
    reduce_loss,
    write_loss_profile,
)


def _write_events_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, sort_keys=True) + "\n")


def _single_turn_entry() -> BoardEntry:
    return BoardEntry(
        id="ent-1",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _weights() -> ScoringWeights:
    return ScoringWeights()


def test_metric_counts_includes_drift_namespace_for_each_drift_count(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_OFF_TOPIC",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            },
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_TOOL_ERROR",
                    "severity": "DRIFT_SEVERITY_INFO",
                },
            },
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    names = {m.name for m in profile.metric_counts}
    assert "drift:off_topic" in names
    assert "drift:tool_error" in names
    # And cost:llm_calls is always present (even at zero).
    assert "cost:llm_calls" in names


def test_metric_counts_emits_cost_llm_calls_from_call_end_events(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {"run_id": "r", "goldfive_llm_call_end": {"span_id": "a"}},
            {"run_id": "r", "goldfive_llm_call_end": {"span_id": "b"}},
            {"run_id": "r", "goldfive_llm_call_end": {"span_id": "c"}},
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    by_name = {m.name: m.count for m in profile.metric_counts}
    assert by_name["cost:llm_calls"] == 3.0


def test_metric_counts_emits_cost_tokens_spent_when_payload_carries_tokens(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {
                "run_id": "r",
                "goldfive_llm_call_end": {
                    "span_id": "a",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                },
            },
            {
                "run_id": "r",
                "goldfive_llm_call_end": {
                    "span_id": "b",
                    "input_tokens": 200,
                },
            },
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    by_name = {m.name: m.count for m in profile.metric_counts}
    # Sum across the three extension fields seen: 1000 + 500 + 200.
    assert by_name["cost:tokens_spent"] == 1700.0
    # First-class scalar field agrees.
    assert profile.tokens_spent == 1700


def test_metric_counts_omits_cost_tokens_spent_when_payload_lacks_tokens(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [{"run_id": "r", "goldfive_llm_call_end": {"span_id": "a"}}],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    names = {m.name for m in profile.metric_counts}
    assert "cost:tokens_spent" not in names
    assert profile.tokens_spent == 0


def test_metric_counts_emits_output_chars_from_final_output_when_provided(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(events_path, [{"run_id": "r"}])
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
        final_output="hello world",
    )
    by_name = {m.name: m.count for m in profile.metric_counts}
    assert by_name["output:chars"] == float(len("hello world"))
    assert profile.output_chars == len("hello world")


def test_metric_counts_emits_schema_failures_when_schema_violation_observed(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_SCHEMA_VIOLATION",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            },
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_SCHEMA_VIOLATION",
                    "severity": "DRIFT_SEVERITY_CRITICAL",
                },
            },
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    by_name = {m.name: m.count for m in profile.metric_counts}
    assert by_name["schema:failures"] == 2.0
    assert profile.schema_failures == 2


def test_metric_counts_omits_zero_metrics_for_compact_json(tmp_path: Path) -> None:
    """Zero-valued cost/output/schema metrics are suppressed (cost:llm_calls
    is the one exception — always emitted to give downstream a stable key)."""
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(events_path, [{"run_id": "r"}])
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=0,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    names = {m.name for m in profile.metric_counts}
    assert names == {"cost:llm_calls"}


def test_loss_profile_json_round_trip_carries_metric_counts(tmp_path: Path) -> None:
    """Reducer output round-trips through write/read_loss_profile."""
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {"run_id": "r", "goldfive_llm_call_end": {"span_id": "a", "input_tokens": 50}},
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_OFF_TOPIC",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            },
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
        final_output="ok",
    )
    target = tmp_path / "loss.json"
    write_loss_profile(profile, target)
    re_read = read_loss_profile(target)

    # Drift_counts identical.
    assert re_read.drift_counts == profile.drift_counts
    # metric_counts identical (order preserved).
    assert re_read.metric_counts == profile.metric_counts
    # Scalar fields preserved.
    assert re_read.tokens_spent == profile.tokens_spent
    assert re_read.output_chars == profile.output_chars
    assert re_read.schema_failures == profile.schema_failures


def test_read_loss_profile_back_compat_loads_old_json_without_metric_fields(
    tmp_path: Path,
) -> None:
    """A loss.json without metric_counts / scalar fields still loads."""
    old_payload = {
        "run_id": "r1",
        "entry_id": "e1",
        "generation_id": "v0",
        "epoch_id": "ep",
        "drift_counts": [{"kind": "off_topic", "severity": "warning", "count": 2}],
        "plan_revisions": 0,
        "task_failure_ratio": 0.0,
        "runtime_ms": 100,
        "wall_clock_budget_exceeded": False,
        "expectation_result": None,
        "drift_loss": 1.0,
        "pass_fail": None,
        "turns_completed": None,
        "memory_failure_count": None,
        "context_loss_count": None,
    }
    target = tmp_path / "old.json"
    with open(target, "w", encoding="utf-8") as f:
        json.dump(old_payload, f)
    profile = read_loss_profile(target)
    assert profile.metric_counts == ()
    assert profile.tokens_spent == 0
    assert profile.output_chars == 0
    assert profile.schema_failures == 0
    # unified_metrics() synthesises the drift-namespace view from
    # drift_counts when metric_counts is empty.
    unified = profile.unified_metrics()
    assert len(unified) == 1
    assert unified[0].name == "drift:off_topic"


def test_unified_metrics_after_reducer_emit_does_not_double_count_drift(
    tmp_path: Path,
) -> None:
    """The reducer puts drift entries in metric_counts under the ``"drift:"``
    namespace; :meth:`LossProfile.unified_metrics` must not double-count them."""
    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(
        events_path,
        [
            {
                "run_id": "r",
                "drift_detected": {
                    "kind": "DRIFT_KIND_OFF_TOPIC",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            },
        ],
    )
    profile = reduce_loss(
        events_jsonl_path=events_path,
        entry=_single_turn_entry(),
        generation_id="v0",
        epoch_id="ep1",
        expectation_result=None,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        weights=_weights(),
    )
    unified = profile.unified_metrics()
    names = [m.name for m in unified]
    assert names.count("drift:off_topic") == 1
