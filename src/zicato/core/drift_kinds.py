"""Registered set of goldfive drift-kind strings.

Zicato consumes goldfive's drift taxonomy as its loss signal. The taxonomy
lives upstream in goldfive's proto schema (``proto/goldfive/v1/types.proto``,
``DriftKind`` enum) and Python enum mirror (``goldfive/types.py``,
``class DriftKind(StrEnum)``); the wire-canonical form is the bare lowercase
string (e.g. ``"off_topic"``, ``"looping_reasoning"``).

We deliberately do NOT import the upstream enum here. Zicato's core types
are model-agnostic data; binding the in-process set of valid drift kinds to
goldfive's importable symbol would force a hard runtime dependency on
goldfive at type-check time and would couple zicato's parse-time behavior
to whatever generated stub layout goldfive is currently shipping. Instead
we keep a frozen mirror of the registered string values and validate
against that. When goldfive adds a new ``DriftKind`` member, add the matching
lowercase string here.

The set below is the floor as of the goldfive proto schema at the time
of writing. New goldfive drift kinds may be added without breaking the
zicato type surface — extending this set is forward-compatible.
"""

from __future__ import annotations

from typing import Any

#: All drift-kind strings zicato will accept inside :class:`DriftCount`,
#: :class:`ExpectedDriftMovement`, :class:`DriftMovementActual`, and the
#: ``required_drift_kinds`` field of synthetic-adversarial board entries.
#:
#: Sourced from ``goldfive/proto/goldfive/v1/types.proto`` (DriftKind enum)
#: and the matching ``goldfive.types.DriftKind`` Python enum. Values are
#: the lowercase wire-canonical form goldfive emits in its event envelope.
GOLDFIVE_DRIFT_KINDS: frozenset[str] = frozenset(
    {
        "tool_error",
        "agent_refusal",
        "new_work_discovered",
        "plan_divergence",
        "user_steer",
        "user_cancel",
        "user_pause",
        "task_failed_recoverable",
        "task_failed_fatal",
        "context_pressure",
        "blocked",
        "wrong_agent",
        "agent_transfer",
        "model_refusal",
        "stopped_early",
        "too_many_steps",
        "goal_unreachable",
        "task_timeout",
        "repeated_failure",
        "unexpected_output",
        "schema_violation",
        "hallucination_suspected",
        "safety_concern",
        "resource_exhausted",
        "ambiguous_intent",
        "custom",
        "looping_tool_call",
        "looping_reasoning",
        "off_topic",
        "intent_divergence",
        "uncertain_progress",
        "self_reported_stuck",
        "reasoning_cluster_tightening",
        "confabulation_risk",
        "runaway_delegation",
        "refine_validation_failed",
        "human_intervention_required",
        "goal_drift",
        "llm_call_timeout",
        "justified_deviation",
        "capability_mismatch",
    }
)


def validate_drift_kind(kind: str) -> None:
    """Raise :class:`ValueError` if *kind* is not a registered drift kind.

    Intended for parse-time validation of operator-authored board entries,
    proposer-emitted hypotheses, and post-run loss profiles. The check is
    a single ``in`` against :data:`GOLDFIVE_DRIFT_KINDS`; callers who need
    to validate large batches should call this once per value rather than
    re-deriving the set.
    """
    if kind not in GOLDFIVE_DRIFT_KINDS:
        raise ValueError(f"unknown drift kind: {kind!r}")


def normalize_wire_drift_kind(raw: Any) -> str | None:
    """Normalise a wire-form drift-kind string to its lowercase canonical form.

    Handles the shapes goldfive's event payloads carry:

      * Bare lowercase already (``"off_topic"``) — returned as-is.
      * Uppercase enum name (``"DRIFT_KIND_OFF_TOPIC"``) — prefix stripped,
        lowercased.
      * ``"DRIFT_KIND_UNSPECIFIED"`` / ``"DRIFT_KIND_"`` / empty / non-string
        — returned as ``None``.

    A non-string ``raw`` (the loss reducer always coerces to ``str`` first;
    the index path may hand the raw JSON value through) yields ``None``
    rather than raising, so both callers share one parse-time contract.

    The aggregation step counts whatever string this hands back; the set of
    *valid* drift kinds on the zicato side is validated separately via
    :func:`validate_drift_kind`.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("DRIFT_KIND_"):
        suffix = raw[len("DRIFT_KIND_") :].lower()
        if suffix in ("", "unspecified"):
            return None
        return suffix
    return raw.lower()


def normalize_wire_severity(raw: Any) -> str | None:
    """Map a wire-form severity string to ``"info"`` / ``"warning"`` / ``"critical"``.

    Accepts a bare lowercase severity, an uppercase ``DRIFT_SEVERITY_*``
    enum name, or mixed case. Anything outside the three scoring severities
    — including ``"DRIFT_SEVERITY_UNSPECIFIED"``, an unknown spelling,
    empty, or a non-string — yields ``None``.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("DRIFT_SEVERITY_"):
        suffix = raw[len("DRIFT_SEVERITY_") :].lower()
        return suffix if suffix in ("info", "warning", "critical") else None
    lo = raw.lower()
    return lo if lo in ("info", "warning", "critical") else None
