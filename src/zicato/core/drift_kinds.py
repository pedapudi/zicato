"""Zicato's string mirror of goldfive's drift taxonomy.

Zicato consumes goldfive's drift taxonomy as its loss signal. The taxonomy
lives upstream in goldfive's proto schema (``proto/goldfive/v1/types.proto``,
``DriftKind`` / ``DriftSeverity`` enums) and Python enum mirror
(``goldfive/types.py``); the wire-canonical form is the bare lowercase
string (e.g. ``"off_topic"``, ``"looping_reasoning"``, ``"warning"``).

We deliberately do NOT import the upstream enums here. Zicato's core types
are model-agnostic data; binding the in-process vocabulary to goldfive's
importable symbols would force a hard runtime dependency on goldfive at
type-check time and would couple zicato's parse-time behavior to whatever
generated stub layout goldfive is currently shipping. Instead this module
keeps a frozen mirror of the registered string values — both the drift-kind
set and, since goldfive became an optional extra, the ``DriftKind`` /
``DriftSeverity`` enums themselves.

The mirrors are :class:`enum.StrEnum` subclasses with the SAME member names,
values, and declaration order as upstream, so a mirror member and the
corresponding goldfive member compare equal, hash equal, and serialise to
the same ``.value``. Board files, contract hashes, and the wire form are
therefore identical whichever type produced them. ``tests/test_drift_kind_
mirror.py`` pins that correspondence against the real goldfive enums
whenever goldfive is installed, so skew shows up as a failing test rather
than as silent divergence.

When goldfive adds a new ``DriftKind`` member, append the matching member
here (in upstream declaration order — the order is observable in the
``valid values are: ...`` error messages). Extending is forward-compatible;
reordering is not.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any


class DriftKind(StrEnum):
    """Mirror of ``goldfive.DriftKind``.

    Member names, values, and declaration order match upstream exactly.
    See the module docstring for why this is a mirror rather than a
    re-export.
    """

    TOOL_ERROR = "tool_error"
    AGENT_REFUSAL = "agent_refusal"
    NEW_WORK_DISCOVERED = "new_work_discovered"
    PLAN_DIVERGENCE = "plan_divergence"
    USER_STEER = "user_steer"
    USER_CANCEL = "user_cancel"
    USER_PAUSE = "user_pause"
    TASK_FAILED_RECOVERABLE = "task_failed_recoverable"
    TASK_FAILED_FATAL = "task_failed_fatal"
    CONTEXT_PRESSURE = "context_pressure"
    BLOCKED = "blocked"
    WRONG_AGENT = "wrong_agent"
    AGENT_TRANSFER = "agent_transfer"
    MODEL_REFUSAL = "model_refusal"
    STOPPED_EARLY = "stopped_early"
    TOO_MANY_STEPS = "too_many_steps"
    GOAL_UNREACHABLE = "goal_unreachable"
    TASK_TIMEOUT = "task_timeout"
    REPEATED_FAILURE = "repeated_failure"
    UNEXPECTED_OUTPUT = "unexpected_output"
    SCHEMA_VIOLATION = "schema_violation"
    HALLUCINATION_SUSPECTED = "hallucination_suspected"
    SAFETY_CONCERN = "safety_concern"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    AMBIGUOUS_INTENT = "ambiguous_intent"
    CUSTOM = "custom"
    LOOPING_TOOL_CALL = "looping_tool_call"
    LOOPING_REASONING = "looping_reasoning"
    REASONING_CLUSTER_TIGHTENING = "reasoning_cluster_tightening"
    OFF_TOPIC = "off_topic"
    INTENT_DIVERGENCE = "intent_divergence"
    UNCERTAIN_PROGRESS = "uncertain_progress"
    SELF_REPORTED_STUCK = "self_reported_stuck"
    CONFABULATION_RISK = "confabulation_risk"
    RUNAWAY_DELEGATION = "runaway_delegation"
    REFINE_VALIDATION_FAILED = "refine_validation_failed"
    GOAL_DRIFT = "goal_drift"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
    LLM_CALL_TIMEOUT = "llm_call_timeout"
    JUSTIFIED_DEVIATION = "justified_deviation"
    CAPABILITY_MISMATCH = "capability_mismatch"


class DriftSeverity(StrEnum):
    """Mirror of ``goldfive.DriftSeverity``.

    The three scoring severities a drift verdict is reported at. Upstream
    carries no ``UNSPECIFIED`` member and neither does this mirror;
    :func:`normalize_wire_severity` is the tolerant reader for wire values
    outside the vocabulary.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


#: All drift-kind strings zicato will accept inside :class:`DriftCount`,
#: :class:`ExpectedDriftMovement`, :class:`DriftMovementActual`, and the
#: ``required_drift_kinds`` field of synthetic-adversarial board entries.
#:
#: Sourced from ``goldfive/proto/goldfive/v1/types.proto`` (DriftKind enum)
#: and the matching ``goldfive.types.DriftKind`` Python enum. Values are
#: the lowercase wire-canonical form goldfive emits in its event envelope.
GOLDFIVE_DRIFT_KINDS: frozenset[str] = frozenset(m.value for m in DriftKind)


@lru_cache(maxsize=1)
def _goldfive_severity_type() -> type | None:
    """Return ``goldfive.DriftSeverity`` if goldfive is installed, else ``None``.

    Resolved lazily and cached: goldfive is an optional extra, and this is
    the only place zicato needs the upstream *type* (as opposed to the
    mirror) at runtime — see :func:`is_drift_severity`.
    """
    try:
        from goldfive import DriftSeverity as _GoldfiveDriftSeverity  # noqa: PLC0415
    except ImportError:
        return None
    return _GoldfiveDriftSeverity if isinstance(_GoldfiveDriftSeverity, type) else None


def is_drift_severity(value: Any) -> bool:
    """Return whether *value* is a drift-severity enum member.

    Accepts a :class:`DriftSeverity` mirror member or — when goldfive is
    installed — an upstream ``goldfive.DriftSeverity`` member, so operator
    board code written against either symbol keeps working. A bare string
    is NOT accepted: the point of the check at the ``Judge.custom`` /
    ``Judge.python`` boundary is to force a typed choice rather than a
    free-form token, and widening it to strings would let a typo through
    to the runtime as a silently-defaulted severity.
    """
    if isinstance(value, DriftSeverity):
        return True
    goldfive_type = _goldfive_severity_type()
    return goldfive_type is not None and isinstance(value, goldfive_type)


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
