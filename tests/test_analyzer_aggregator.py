"""Tests for the decision-event aggregator.

The aggregator works on the JSON-line shape goldfive's persistence sink
writes. We construct synthetic JSONL files directly in these tests
rather than going through goldfive — the aggregator's contract is "read
the JSON shape goldfive writes" and our fixtures keep the format
verbatim. This keeps the test suite running on environments where
goldfive's proto stubs are not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.analyzer.aggregator import (
    DecisionEventSummary,
    aggregate_decision_events,
)


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


def _envelope(seq: int, payload_key: str, payload: dict) -> dict:
    """Wrap a payload in goldfive's Event envelope shape."""

    return {
        "event_id": f"evt_{seq}",
        "run_id": "run_test",
        "sequence": seq,
        "emitted_at": {"seconds": 1_700_000_000 + seq, "nanos": 0},
        "session_id": "sess_test",
        payload_key: payload,
    }


def test_aggregate_decision_events_with_full_telemetry(tmp_path: Path) -> None:
    """Synthetic JSONL with 4 ladder transitions + 2 policy applications + others."""

    events = [
        _envelope(
            0,
            "ladder_transition_decided",
            {
                "from_level": "observe",
                "to_level": "nudge",
                "reason": "first occurrence",
                "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                "drift_id": "drift_1",
                "severity": "DRIFT_SEVERITY_WARNING",
            },
        ),
        _envelope(
            1,
            "ladder_transition_decided",
            {
                "from_level": "nudge",
                "to_level": "cancel_reinvoke",
                "reason": "repeat (count=2)",
                "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                "drift_id": "drift_2",
                "severity": "DRIFT_SEVERITY_WARNING",
            },
        ),
        _envelope(
            2,
            "ladder_transition_decided",
            {
                "from_level": "",  # first ladder pick on a fresh condition
                "to_level": "nudge",
                "reason": "first occurrence",
                "drift_kind": "DRIFT_KIND_LOOPING_REASONING",
                "drift_id": "drift_3",
                "severity": "DRIFT_SEVERITY_INFO",
            },
        ),
        _envelope(
            3,
            "ladder_transition_decided",
            {
                "from_level": "observe",
                "to_level": "nudge",
                "reason": "first occurrence",
                "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                "drift_id": "drift_4",
                "severity": "DRIFT_SEVERITY_WARNING",
            },
        ),
        _envelope(
            4,
            "policy_applied",
            {
                "policy_name": "observation_only_gate",
                "outcome": "applied",
                "reason": "observation_only=true",
                "detail": "",
            },
        ),
        _envelope(
            5,
            "policy_applied",
            {
                "policy_name": "user_steer_cooldown",
                "outcome": "suppressed",
                "reason": "cooldown active",
                "detail": "remaining=2",
            },
        ),
    ]
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, events)

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 6
    # Two of the four transitions share key "observe->nudge".
    assert summary.ladder_transitions["observe->nudge"] == 2
    assert summary.ladder_transitions["nudge->cancel_reinvoke"] == 1
    assert summary.ladder_transitions["(none)->nudge"] == 1
    # Ladder reasons aggregate across transitions.
    assert summary.ladder_reasons["first occurrence"] == 3
    assert summary.ladder_reasons["repeat (count=2)"] == 1
    # Policies are keyed by policy_name with per-outcome subdicts.
    assert summary.policy_outcomes["observation_only_gate"] == {"applied": 1}
    assert summary.policy_outcomes["user_steer_cooldown"] == {"suppressed": 1}


def test_aggregate_tolerates_missing_decision_events(tmp_path: Path) -> None:
    """An events.jsonl with no decision-telemetry events yields a zero summary."""

    # Only RunStarted-style envelopes, no decision-telemetry payloads.
    events = [
        _envelope(0, "run_started", {"run_id": "run_test", "goal_summary": "hi"}),
        _envelope(1, "task_started", {"task_id": "t1", "detail": "synthetic"}),
        _envelope(2, "drift_detected", {"id": "drift_x", "kind": "DRIFT_KIND_OFF_TOPIC"}),
    ]
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, events)

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 0
    assert summary.ladder_transitions == {}
    assert summary.ladder_reasons == {}
    assert summary.dispatch_orders == []
    assert summary.policy_outcomes == {}
    assert summary.retry_attempts == {}
    assert summary.steering_decisions == {}


def test_aggregate_handles_no_files() -> None:
    """An empty path list returns a zero summary (no I/O)."""

    summary = aggregate_decision_events([])
    assert isinstance(summary, DecisionEventSummary)
    assert summary.total_events_seen == 0


def test_aggregate_handles_missing_file(tmp_path: Path) -> None:
    """A path that doesn't exist contributes zero counts silently."""

    missing = tmp_path / "does_not_exist.jsonl"
    summary = aggregate_decision_events([missing])
    assert summary.total_events_seen == 0


def test_aggregate_multiple_files_concatenate(tmp_path: Path) -> None:
    """Counts across two JSONL files sum correctly."""

    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    _write_jsonl(
        path_a,
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {
                    "from_level": "observe",
                    "to_level": "nudge",
                    "reason": "first occurrence",
                    "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                    "drift_id": "drift_1",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            ),
        ],
    )
    _write_jsonl(
        path_b,
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {
                    "from_level": "observe",
                    "to_level": "nudge",
                    "reason": "first occurrence",
                    "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                    "drift_id": "drift_2",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            ),
            _envelope(
                1,
                "steering_decision_made",
                {
                    "detector_name": "reasoning_judge",
                    "outcome": "no_drift",
                    "reason": "cosine 0.42 below 0.6",
                    "score": 0.42,
                },
            ),
        ],
    )

    summary = aggregate_decision_events([path_a, path_b])

    # Two ladder transitions across files; both share key.
    assert summary.ladder_transitions["observe->nudge"] == 2
    assert summary.steering_decisions["reasoning_judge"] == {"no_drift": 1}
    assert summary.total_events_seen == 3


def test_aggregate_skips_malformed_lines(tmp_path: Path) -> None:
    """Malformed JSON lines are skipped; valid lines still aggregate."""

    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                "not valid json",
                json.dumps(
                    _envelope(
                        0,
                        "policy_applied",
                        {
                            "policy_name": "supersession_integration",
                            "outcome": "applied",
                            "reason": "",
                            "detail": "",
                        },
                    )
                ),
                "{ broken",
                "",
                json.dumps(
                    _envelope(
                        1,
                        "retry_budget_spent",
                        {
                            "operation": "refine",
                            "attempt": 2,
                            "budget_remaining": 0,
                            "reason": "budget_exhausted",
                        },
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 2
    assert summary.policy_outcomes["supersession_integration"] == {"applied": 1}
    assert summary.retry_attempts["refine"] == [2]


def test_aggregate_dispatch_orderings_record_tuples(tmp_path: Path) -> None:
    """DetectorDispatchOrdered events surface as ordered string tuples."""

    path = tmp_path / "events.jsonl"
    _write_jsonl(
        path,
        [
            _envelope(
                0,
                "detector_dispatch_ordered",
                {
                    "dispatch_order": ["reasoning_judge", "tool_loops", "goal_drift"],
                    "reason": "default",
                },
            ),
            _envelope(
                1,
                "detector_dispatch_ordered",
                {
                    "dispatch_order": ["tool_loops", "goal_drift"],
                    "reason": "reasoning_drift_mode=disabled excludes embedding detectors",
                },
            ),
            _envelope(
                2,
                "detector_dispatch_ordered",
                {
                    "dispatch_order": ["reasoning_judge", "tool_loops", "goal_drift"],
                    "reason": "default",
                },
            ),
        ],
    )

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 3
    assert ("reasoning_judge", "tool_loops", "goal_drift") in summary.dispatch_orders
    assert ("tool_loops", "goal_drift") in summary.dispatch_orders
    # Two sessions ran the default ordering.
    assert summary.dispatch_orders.count(("reasoning_judge", "tool_loops", "goal_drift")) == 2


def test_aggregate_retry_attempts_collect_values(tmp_path: Path) -> None:
    """RetryBudgetSpent events surface attempt numbers per operation."""

    path = tmp_path / "events.jsonl"
    _write_jsonl(
        path,
        [
            _envelope(
                0,
                "retry_budget_spent",
                {
                    "operation": "refine",
                    "attempt": 1,
                    "budget_remaining": 1,
                    "reason": "call_llm raised: rate_limited",
                },
            ),
            _envelope(
                1,
                "retry_budget_spent",
                {
                    "operation": "refine",
                    "attempt": 2,
                    "budget_remaining": 0,
                    "reason": "budget_exhausted",
                },
            ),
            _envelope(
                2,
                "retry_budget_spent",
                {
                    "operation": "generate",
                    "attempt": 1,
                    "budget_remaining": 0,
                    "reason": "validated",
                },
            ),
        ],
    )

    summary = aggregate_decision_events([path])

    assert summary.retry_attempts == {
        "refine": [1, 2],
        "generate": [1],
    }


def test_aggregate_steering_decisions_count_outcomes(tmp_path: Path) -> None:
    """SteeringDecisionMade events aggregate per detector_name + outcome."""

    path = tmp_path / "events.jsonl"
    _write_jsonl(
        path,
        [
            _envelope(
                i,
                "steering_decision_made",
                {
                    "detector_name": "reasoning_judge",
                    "outcome": "no_drift" if i % 2 == 0 else "drift_emitted",
                    "reason": "",
                    "score": 0.0,
                },
            )
            for i in range(5)
        ],
    )

    summary = aggregate_decision_events([path])

    assert summary.steering_decisions["reasoning_judge"]["no_drift"] == 3
    assert summary.steering_decisions["reasoning_judge"]["drift_emitted"] == 2


def test_aggregate_ignores_non_dict_lines(tmp_path: Path) -> None:
    """Lines that parse but aren't dicts (lists, bare numbers) are skipped."""

    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                "42",
                "[1, 2, 3]",
                json.dumps(
                    _envelope(
                        0,
                        "policy_applied",
                        {
                            "policy_name": "same_turn_dedup",
                            "outcome": "skipped",
                            "reason": "",
                            "detail": "",
                        },
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 1
    assert summary.policy_outcomes["same_turn_dedup"] == {"skipped": 1}


def test_aggregate_handles_goldfive_camelcase_shape(tmp_path: Path) -> None:
    """Regression for issue #1.

    goldfive's ``JSONLPersistenceSink`` serializes with ``MessageToJson``
    WITHOUT ``preserving_proto_field_name`` — the real on-disk JSONL is
    camelCase. The aggregator used to key on snake_case only, so it
    matched zero events and ``steering_decisions`` was always empty. The
    aggregator must normalize camelCase keys (envelope AND payload).
    """

    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                # SteeringDecisionMade — camelCase envelope key + camelCase
                # payload field ``detectorName``.
                json.dumps(
                    {
                        "eventId": "evt_0",
                        "runId": "run_test",
                        "sequence": 0,
                        "sessionId": "sess_test",
                        "steeringDecisionMade": {
                            "detectorName": "reasoning_judge",
                            "outcome": "drift_emitted",
                        },
                    }
                ),
                # LadderTransitionDecided — camelCase ``fromLevel`` /
                # ``toLevel`` payload fields.
                json.dumps(
                    {
                        "eventId": "evt_1",
                        "runId": "run_test",
                        "sequence": 1,
                        "ladderTransitionDecided": {
                            "fromLevel": "observe",
                            "toLevel": "nudge",
                            "reason": "first_occurrence",
                        },
                    }
                ),
                # RetryBudgetSpent — camelCase ``budgetRemaining``.
                json.dumps(
                    {
                        "eventId": "evt_2",
                        "runId": "run_test",
                        "sequence": 2,
                        "retryBudgetSpent": {
                            "operation": "refine_steer",
                            "attempt": 2,
                            "budgetRemaining": 1,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = aggregate_decision_events([path])

    # The whole point of issue #1: steering_decisions is NOT empty.
    assert summary.total_events_seen == 3
    assert summary.steering_decisions == {"reasoning_judge": {"drift_emitted": 1}}
    assert summary.ladder_transitions == {"observe->nudge": 1}
    assert summary.ladder_reasons == {"first_occurrence": 1}
    assert summary.retry_attempts == {"refine_steer": [2]}


def test_aggregate_mixed_camel_and_snake_in_one_file(tmp_path: Path) -> None:
    """A file with both casings aggregates without loss — the normalization
    is idempotent on already-snake_case keys."""

    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "sequence": 0,
                        "steeringDecisionMade": {
                            "detectorName": "goal_drift",
                            "outcome": "no_drift",
                        },
                    }
                ),
                json.dumps(
                    _envelope(
                        1,
                        "steering_decision_made",
                        {"detector_name": "goal_drift", "outcome": "no_drift"},
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = aggregate_decision_events([path])

    assert summary.total_events_seen == 2
    assert summary.steering_decisions == {"goal_drift": {"no_drift": 2}}
