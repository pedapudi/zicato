"""Decision-event aggregation over goldfive ``events.jsonl`` files.

Five event types matter for decision-telemetry analysis:

* ``LadderTransitionDecided`` (tag 40) — the intervention ladder picked
  a level for a drift (``from_level`` / ``to_level`` / ``reason`` /
  ``drift_kind`` / ``drift_id`` / ``severity``).
* ``DetectorDispatchOrdered`` (tag 41) — the steerer's per-session
  detector ordering (``dispatch_order`` / ``reason``).
* ``PolicyApplied`` (tag 42) — any non-drift steerer policy decision
  (``policy_name`` / ``outcome`` / ``reason`` / ``detail``).
* ``RetryBudgetSpent`` (tag 43) — refine retry accounting
  (``operation`` / ``attempt`` / ``budget_remaining`` / ``reason``).
* ``SteeringDecisionMade`` (tag 39) — per-detector verdict
  (``detector_name`` / ``outcome``).

Reading goes through :mod:`zicato.telemetry.event_log`, which needs no
goldfive proto stubs, so the aggregator keeps working in environments
where they are absent or behind upstream's schema. It also means an
event's payload case and field names are spelled here exactly as they
are in the reducer and the dashboard.

Tolerant on every axis:

* No file → contributes zero counts.
* File with no decision-telemetry events (older goldfive) → contributes
  zero counts.
* Malformed JSON lines → skipped silently.
* Unknown payload cases → ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.telemetry.event_log import read_event_log

# The five ``Event.payload`` oneof cases the analyzer counts. Spelled
# snake_case, which is what the reader hands back whichever spelling the
# file on disk used.
_LADDER_KEY = "ladder_transition_decided"
_DISPATCH_KEY = "detector_dispatch_ordered"
_POLICY_KEY = "policy_applied"
_RETRY_KEY = "retry_budget_spent"
_STEERING_KEY = "steering_decision_made"

_DECISION_KEYS: frozenset[str] = frozenset(
    (_LADDER_KEY, _DISPATCH_KEY, _POLICY_KEY, _RETRY_KEY, _STEERING_KEY)
)


@dataclass(frozen=True, slots=True)
class DecisionEventSummary:
    """Aggregated decision-event counts across one epoch.

    Every field is a JSON-friendly primitive (dict / list / int) so the
    summary can be rendered into a prompt block, persisted as JSON for
    debugging, and round-tripped through tests without converters.

    Fields
    ------
    ladder_transitions:
        ``"{from_level}->{to_level}"`` → count. The ``from_level`` field
        is the empty string for the first ladder pick on a fresh
        condition (per the proto); we render it as ``"(none)"`` in the
        key so the prompt block reads cleanly.
    ladder_reasons:
        ``reason`` → count for ``LadderTransitionDecided`` events.
        Surfaces the dominant rationales the ladder used.
    dispatch_orders:
        One tuple per ``DetectorDispatchOrdered`` event, preserving the
        order the steerer chose. Repeated orders deduplicate naturally
        in the prompt block by counting tuple frequency.
    policy_outcomes:
        ``policy_name`` → ``outcome`` → count. Captures the per-policy
        outcome distribution so the LLM can spot policies that mostly
        suppress vs mostly apply.
    retry_attempts:
        ``operation`` → list of ``attempt`` values observed. The list
        preserves order of occurrence so the LLM can spot operations
        that repeatedly exhaust their budget.
    steering_decisions:
        ``detector_name`` → ``outcome`` → count. The
        ``SteeringDecisionMade`` event already drives the per-detector
        outcome breakdown; we replicate it here so a downstream
        consumer can produce one analysis prompt from one summary
        without cross-referencing other aggregates.
    total_events_seen:
        Sum of every decision-telemetry event the aggregator counted
        (across all five payload kinds). Useful as a quick sanity
        signal in the prompt block: ``0`` means the epoch ran on a
        goldfive without decision telemetry and the LLM should say so
        rather than hallucinate.
    """

    ladder_transitions: dict[str, int] = field(default_factory=dict)
    ladder_reasons: dict[str, int] = field(default_factory=dict)
    dispatch_orders: list[tuple[str, ...]] = field(default_factory=list)
    policy_outcomes: dict[str, dict[str, int]] = field(default_factory=dict)
    retry_attempts: dict[str, list[int]] = field(default_factory=dict)
    steering_decisions: dict[str, dict[str, int]] = field(default_factory=dict)
    total_events_seen: int = 0


def _absorb_ladder(payload: dict[str, Any], summary_acc: _Accumulator) -> None:
    from_level = str(payload.get("from_level") or "")
    to_level = str(payload.get("to_level") or "")
    if not to_level:
        # A ``LadderTransitionDecided`` without a ``to_level`` is
        # meaningless — skip silently.
        return
    rendered_from = from_level if from_level else "(none)"
    key = f"{rendered_from}->{to_level}"
    summary_acc.ladder_transitions[key] = summary_acc.ladder_transitions.get(key, 0) + 1

    reason = str(payload.get("reason") or "").strip()
    if reason:
        summary_acc.ladder_reasons[reason] = summary_acc.ladder_reasons.get(reason, 0) + 1


def _absorb_dispatch(payload: dict[str, Any], summary_acc: _Accumulator) -> None:
    order_raw = payload.get("dispatch_order")
    if not isinstance(order_raw, list):
        return
    order_tuple = tuple(str(x) for x in order_raw)
    summary_acc.dispatch_orders.append(order_tuple)


def _absorb_policy(payload: dict[str, Any], summary_acc: _Accumulator) -> None:
    policy_name = str(payload.get("policy_name") or "").strip()
    outcome = str(payload.get("outcome") or "").strip()
    if not policy_name:
        return
    bucket = summary_acc.policy_outcomes.setdefault(policy_name, {})
    out_key = outcome or "(unspecified)"
    bucket[out_key] = bucket.get(out_key, 0) + 1


def _absorb_retry(payload: dict[str, Any], summary_acc: _Accumulator) -> None:
    operation = str(payload.get("operation") or "").strip()
    if not operation:
        return
    raw_attempt = payload.get("attempt", 0)
    try:
        attempt = int(raw_attempt)
    except (TypeError, ValueError):
        attempt = 0
    summary_acc.retry_attempts.setdefault(operation, []).append(attempt)


def _absorb_steering(payload: dict[str, Any], summary_acc: _Accumulator) -> None:
    detector_name = str(payload.get("detector_name") or "").strip()
    outcome = str(payload.get("outcome") or "").strip()
    if not detector_name:
        return
    bucket = summary_acc.steering_decisions.setdefault(detector_name, {})
    out_key = outcome or "(unspecified)"
    bucket[out_key] = bucket.get(out_key, 0) + 1


@dataclass
class _Accumulator:
    """Mutable scratchpad used during aggregation. Frozen on return."""

    ladder_transitions: dict[str, int] = field(default_factory=dict)
    ladder_reasons: dict[str, int] = field(default_factory=dict)
    dispatch_orders: list[tuple[str, ...]] = field(default_factory=list)
    policy_outcomes: dict[str, dict[str, int]] = field(default_factory=dict)
    retry_attempts: dict[str, list[int]] = field(default_factory=dict)
    steering_decisions: dict[str, dict[str, int]] = field(default_factory=dict)
    total_events_seen: int = 0


_ABSORBERS = {
    _LADDER_KEY: _absorb_ladder,
    _DISPATCH_KEY: _absorb_dispatch,
    _POLICY_KEY: _absorb_policy,
    _RETRY_KEY: _absorb_retry,
    _STEERING_KEY: _absorb_steering,
}


def aggregate_decision_events(events_jsonl_paths: list[Path]) -> DecisionEventSummary:
    """Build a :class:`DecisionEventSummary` over a list of ``events.jsonl`` paths.

    Each path is replayed in order; counts accumulate across all files.
    Missing files, malformed lines, and absent decision-telemetry
    payloads are all tolerated — the caller only needs to know that
    ``total_events_seen == 0`` means "no decision telemetry available".

    Parameters
    ----------
    events_jsonl_paths:
        Absolute paths to goldfive ``events.jsonl`` files. Typically
        every ``runs/{entry_id}/events.jsonl`` under every generation
        in the epoch the analyzer is summarising; the analyzer's
        :func:`zicato.analyzer.insights.analyze_epoch_telemetry`
        builds the path list.

    Returns
    -------
    DecisionEventSummary
        Frozen view of the aggregated counts. When no decision-
        telemetry events were observed every dict / list is empty and
        :attr:`total_events_seen` is ``0`` — the analyzer's
        prompt-renderer treats that as a clean "nothing to report"
        signal.
    """

    acc = _Accumulator()
    for path in events_jsonl_paths:
        for event in read_event_log(path).records:
            absorber = _ABSORBERS.get(event.case)
            if absorber is None:
                continue
            absorber(event.payload, acc)
            acc.total_events_seen += 1

    return DecisionEventSummary(
        ladder_transitions=dict(acc.ladder_transitions),
        ladder_reasons=dict(acc.ladder_reasons),
        dispatch_orders=list(acc.dispatch_orders),
        policy_outcomes={k: dict(v) for k, v in acc.policy_outcomes.items()},
        retry_attempts={k: list(v) for k, v in acc.retry_attempts.items()},
        steering_decisions={k: dict(v) for k, v in acc.steering_decisions.items()},
        total_events_seen=acc.total_events_seen,
    )


__all__ = [
    "DecisionEventSummary",
    "aggregate_decision_events",
]
