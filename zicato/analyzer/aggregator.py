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

The aggregator is JSONL-only — it reads the files goldfive's persistence
sink wrote and does not require the goldfive proto stubs to be
importable. The reducer's
:func:`zicato.telemetry.reducer._load_events_as_dicts` already proves
this shape works; the analyzer follows the same plain-JSON-fallback
discipline so it keeps working in environments where the proto stubs
are absent or behind upstream's schema.

Tolerant on every axis:

* No file → contributes zero counts.
* File with no decision-telemetry events (older goldfive) → contributes
  zero counts.
* Malformed JSON lines → skipped silently.
* Unknown payload keys → ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# goldfive's ``JSONLPersistenceSink.emit`` serializes each event with
# ``MessageToJson(event, sort_keys=True, indent=None)`` — WITHOUT
# ``preserving_proto_field_name=True`` — so the on-disk JSONL is
# *camelCase* (``steeringDecisionMade``, ``detectorName``, ``fromLevel``,
# ``dispatchOrder``, ``budgetRemaining``). The analyzer keys on
# snake_case below and normalizes every event/payload dict's keys to
# snake_case on read (see :func:`_snake_keys`), so the matching works on
# the real on-disk shape AND on a snake_case producer (the reducer's
# proto-reparse path, which uses ``preserving_proto_field_name=True``).
# Normalizing keeps the analyzer proto-stub-free — it never imports the
# goldfive proto module.
#
# The five ``Event.payload`` oneof field names the analyzer cares about,
# in snake_case (the post-normalization form):
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


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(name: str) -> str:
    """Normalize a JSON key to snake_case.

    ``camelCase`` / ``PascalCase`` -> ``snake_case``; an already
    snake_case key passes through unchanged. ``steeringDecisionMade`` ->
    ``steering_decision_made``; ``detectorName`` -> ``detector_name``;
    ``outcome`` -> ``outcome``.
    """

    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _snake_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``d`` with top-level keys snake-cased.

    Applied to both the Event envelope (so the payload oneof key matches)
    and the payload sub-dict (so its field names match). Shallow is
    sufficient: payload fields are scalars / lists, never nested dicts
    the analyzer reaches into.
    """

    return {_to_snake(k): v for k, v in d.items()}


def _iter_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON objects from ``path``, one per line, skipping junk.

    Mirrors the reducer's plain-JSON fallback: malformed lines are
    silently skipped so a single bad line cannot wipe out an entire
    file's signal. Non-dict top-level values (a stray bare number, a
    list) are also skipped — the analyzer only cares about goldfive
    Event-dict shapes. Top-level keys are normalized to snake_case so
    the camelCase shape goldfive's persistence sink writes is matched.
    """

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield _snake_keys(obj)
    except OSError:
        # Missing / unreadable files contribute zero counts. The caller
        # is the analyzer entry point, which already accepts the
        # "no telemetry yet" path; we mirror that here.
        return


def _payload_for(event: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Return the payload sub-dict under ``key`` if present and a dict.

    The payload's own field names are snake-cased too (goldfive writes
    them camelCase: ``fromLevel``, ``detectorName``, ``dispatchOrder``,
    ``budgetRemaining``), so the absorbers' snake_case lookups match.
    """

    raw = event.get(key)
    if isinstance(raw, dict):
        return _snake_keys(raw)
    return None


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
        for event in _iter_json_lines(path):
            for key, absorber in _ABSORBERS.items():
                payload = _payload_for(event, key)
                if payload is None:
                    continue
                absorber(payload, acc)
                acc.total_events_seen += 1
                # An Event proto's payload is a oneof — at most one of
                # the five keys can be set per envelope. We can break
                # as soon as one absorber fired so two absorbers do not
                # double-count a malformed event that happens to carry
                # two payload keys at once.
                break

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
