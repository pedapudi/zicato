"""Telemetry dialects: pluggable ``LossProfile`` producers.

[TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md) is the
design. The principle: :class:`~zicato.core.LossProfile` is the
convergence point, and a *dialect* is a named producer that turns a
run's raw telemetry into the ``LossProfile`` inputs. Everything
downstream of the reducer (scoring, the promote gate, the analytical
index, board reflection) reads ``LossProfile`` and never knows which
dialect produced it.

This module owns the dialect-agnostic bundle (:class:`DialectSignals`),
the two NON-goldfive producers (:func:`reduce_adk_events` /
:func:`reduce_transcript`), and the pure capability-mismatch checker
(:func:`dialect_capability_warnings`). The ``goldfive`` producer lives in
:mod:`zicato.telemetry.reducer` (it wraps the existing event walk and its
helpers) and is registered there; keeping it there avoids a circular
import and keeps the byte-identical default path exactly where it was.

Every producer is a DETERMINISTIC re-reduction of a durable file
(TELEMETRY.md §7): the same JSONL + the same inputs yield a byte-identical
:class:`DialectSignals`, hence a byte-identical ``LossProfile``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from zicato.core import (
    DIALECT_ADK_EVENTS,
    DIALECT_GOLDFIVE,
    DIALECT_TRANSCRIPT,
    BoardEntry,
    DriftCount,
    ScoringWeights,
)

__all__ = [
    "DialectSignals",
    "DialectReducer",
    "reduce_adk_events",
    "reduce_transcript",
    "dialect_capability_warnings",
    "DIALECT_GOLDFIVE",
    "DIALECT_ADK_EVENTS",
    "DIALECT_TRANSCRIPT",
]


# ---------------------------------------------------------------------------
# The dialect-agnostic signal bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DialectSignals:
    """The raw signals a dialect produces for :func:`reduce_loss` to score.

    A flat bundle mirroring exactly the intermediate values the reducer's
    event walk has always computed — so the ``goldfive`` producer returns
    one of these unchanged and the dialect-agnostic tail of
    :func:`reduce_loss` (the not-completed penalty, the drift-loss
    dispatch, the pass/continuous-score derivation, the generalised metric
    surface, ``LossProfile`` assembly) is byte-identical to before.

    Fields
    ------
    drift_counts:
        Per ``(kind, severity)`` drift rows, ALREADY sorted (so map
        iteration order never leaks into the profile).
    plan_revisions:
        Count of plan-revision signals. ``0`` for dialects that carry no
        plan telemetry (``adk_events`` / ``transcript``).
    task_started / task_failed:
        The task denominator / numerator the reducer turns into
        ``task_failure_ratio`` (the ×10 pure-failure term).
    llm_call_count:
        Count of model/LLM calls — the ``cost:llm_calls`` metric.
    token_count:
        Summed token usage — ``cost:tokens_spent`` / ``tokens_spent``.
    agent_text_chars:
        Summed agent-side text length — the ``output:chars`` fallback when
        the caller passes no explicit ``final_output``.
    run_id / adk_session_id:
        Run identity + the harmonograf deep-link session id. Empty strings
        when the dialect's source carries no id (the reducer then falls
        back to a synthetic ``run_id``).
    agent_turns / user_turns:
        The reconstructed transcript the multi-turn memory/context
        heuristics run over (used only for non-``single_turn`` entries).
    malformed_line_count:
        Count of source lines the producer could not parse. Never fatal —
        surfaced as a reduction warning (see :attr:`warnings`).
    warnings:
        Human-readable reduction warnings (malformed lines, capability
        mismatches) the reducer logs. Advisory only.
    """

    drift_counts: tuple[DriftCount, ...] = ()
    plan_revisions: int = 0
    task_started: int = 0
    task_failed: int = 0
    llm_call_count: int = 0
    token_count: int = 0
    agent_text_chars: int = 0
    run_id: str = ""
    adk_session_id: str = ""
    agent_turns: tuple[str, ...] = ()
    user_turns: tuple[str, ...] = ()
    malformed_line_count: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)


class DialectReducer(Protocol):
    """The producer contract: ``(events_jsonl_path, entry) -> DialectSignals``.

    Every dialect — ``goldfive`` (in :mod:`zicato.telemetry.reducer`),
    ``adk_events``, ``transcript`` — is a callable of this shape. Pure,
    deterministic, filesystem-read-only.
    """

    def __call__(self, events_jsonl_path: Path, entry: BoardEntry) -> DialectSignals: ...


# ---------------------------------------------------------------------------
# Tolerant JSONL reading (shared by the non-goldfive dialects)
# ---------------------------------------------------------------------------


def _iter_json_objects(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read a JSONL file into ``(objects, malformed_line_count)``.

    Tolerant by contract (TELEMETRY-DIALECTS.md §3.1): a line that is not
    JSON, or is JSON but not an object, is COUNTED as malformed and
    skipped — never raised. A missing file yields ``([], 0)``.
    """
    objs: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return objs, 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(parsed, dict):
                objs.append(parsed)
            else:
                malformed += 1
    return objs, malformed


def _first_str(obj: dict[str, Any], *keys: str) -> str:
    """First non-empty string value among ``keys`` (alias tolerance)."""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


# ---------------------------------------------------------------------------
# adk_events dialect
# ---------------------------------------------------------------------------

# Drift-vocabulary kinds the adk_events signal table maps into (§3.2). Each
# folds through the SAME severity × per-kind × count machinery a goldfive
# drift instrument folds through, so an operator tunes them with the same
# ``severity_weights`` / ``per_kind_weights`` knobs.
_ADK_ERROR_KIND: str = "tool_error"
_ADK_LOOP_KIND: str = "looping_tool_call"
_ADK_TRANSFER_KIND: str = "agent_transfer"

_SEV_INFO: str = "info"
_SEV_WARNING: str = "warning"
_SEV_CRITICAL: str = "critical"

_ERROR_STATUSES: frozenset[str] = frozenset({"error", "failure", "failed", "fail"})


def _event_type(obj: dict[str, Any]) -> str:
    """The event kind, tolerant of ``type`` / ``event_type`` / ``kind``."""
    return _first_str(obj, "type", "event_type", "kind")


def _tool_response_is_error(obj: dict[str, Any]) -> bool:
    """Whether a ``tool_response`` event indicates a failure.

    An error is signalled by any of: a ``status`` in
    :data:`_ERROR_STATUSES`, ``is_error == True``, or a truthy ``error``.
    """
    status = _first_str(obj, "status").lower()
    if status in _ERROR_STATUSES:
        return True
    if obj.get("is_error") is True:
        return True
    return bool(obj.get("error"))


def _tool_signature(obj: dict[str, Any]) -> str:
    """A canonical ``(tool, args)`` signature for retry-loop detection.

    ``args`` are serialised with ``sort_keys=True`` so key ordering in the
    source cannot flip a retry-loop verdict (the §6 determinism rule).
    ``default=str`` keeps a non-JSON-native arg value from raising.
    """
    tool = _first_str(obj, "tool", "name", "tool_name")
    args: Any = None
    for k in ("args", "arguments", "input"):
        if k in obj:
            args = obj[k]
            break
    try:
        args_canon = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_canon = repr(args)
    return f"{tool}\x00{args_canon}"


def _usage_tokens(obj: dict[str, Any]) -> int:
    """Summed token usage for a ``model_usage`` event, tolerant of shape.

    Prefers ``input_tokens + output_tokens`` (also ``prompt_tokens`` /
    ``completion_tokens``); falls back to a single ``total_tokens`` /
    ``tokens`` when neither directional count is present. Reads a nested
    ``usage`` object when the counts live there.
    """
    src: dict[str, Any] = obj
    nested = obj.get("usage")
    if isinstance(nested, dict):
        src = nested
    total = 0
    got = False
    for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
        v = src.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            total += int(v)
            got = True
    if not got:
        for k in ("total_tokens", "tokens"):
            v = src.get(k)
            if isinstance(v, int | float) and not isinstance(v, bool):
                total += int(v)
                break
    return total


def _message_text(obj: dict[str, Any]) -> str:
    """Message body, tolerant of ``text`` / ``content`` / ``message``."""
    return _first_str(obj, "text", "content", "message")


def _message_role(obj: dict[str, Any]) -> str:
    """The message role bucket: ``"agent"``, ``"user"``, or ``""``.

    Reads the event ``type`` first (``agent_message`` / ``user_message``);
    only a generic ``message`` event or a TYPELESS line (a bare transcript
    ``{"role": …, "content": …}`` shape) may fall back to the ``role``
    field (``assistant`` / ``agent`` / ``model`` → agent; ``user`` /
    ``human`` → user). Any OTHER event type returns ``""`` even when it
    carries a ``role`` — the §3.1 "unknown type is skipped" contract: a
    forward-compat event (a reasoning step, a log line) must not inflate
    the output envelope or mint phantom transcript turns.
    """
    etype = _event_type(obj)
    if etype == "agent_message":
        return "agent"
    if etype == "user_message":
        return "user"
    if etype not in ("", "message"):
        return ""
    role = _first_str(obj, "role").lower()
    if role in ("assistant", "agent", "model"):
        return "agent"
    if role in ("user", "human"):
        return "user"
    return ""


def reduce_adk_events(events_jsonl_path: Path, entry: BoardEntry) -> DialectSignals:
    """Reduce a generic ADK-style agent event-log JSONL (§3).

    See the signal table in TELEMETRY-DIALECTS.md §3.2 for the mapping of
    each event-log signal into the drift-signal vocabulary. Unknown event
    types are skipped; malformed lines are counted and surfaced as a
    warning; a missing field contributes nothing. Deterministic: the walk
    is order-stable and ``drift_counts`` is sorted before it is frozen.
    """
    objs, malformed = _iter_json_objects(events_jsonl_path)

    drift_bucket: dict[tuple[str, str], int] = {}
    task_started = 0
    task_failed = 0
    llm_call_count = 0
    token_count = 0
    agent_text_chars = 0
    error_events = 0
    transfer_events = 0
    loop_events = 0
    run_id = ""
    adk_session_id = ""
    seen_tool_sigs: set[str] = set()
    agent_turns: list[str] = []
    user_turns: list[str] = []

    for obj in objs:
        if not run_id:
            run_id = _first_str(obj, "run_id", "runId", "invocation_id", "invocationId")
        if not adk_session_id:
            adk_session_id = _first_str(obj, "session_id", "sessionId")
        etype = _event_type(obj)
        if etype == "tool_call":
            task_started += 1
            sig = _tool_signature(obj)
            if sig in seen_tool_sigs:
                loop_events += 1
            else:
                seen_tool_sigs.add(sig)
        elif etype == "tool_response":
            if _tool_response_is_error(obj):
                task_failed += 1
        elif etype in ("error", "exception"):
            error_events += 1
        elif etype in ("agent_transfer", "transfer"):
            transfer_events += 1
        elif etype == "model_usage":
            llm_call_count += 1
            token_count += _usage_tokens(obj)
        else:
            # agent_message / user_message / generic message → transcript;
            # everything else (unknown type) is skipped.
            role = _message_role(obj)
            if role == "agent":
                text = _message_text(obj)
                agent_text_chars += len(text)
                agent_turns.append(text)
            elif role == "user":
                user_turns.append(_message_text(obj))

    if error_events:
        drift_bucket[(_ADK_ERROR_KIND, _SEV_CRITICAL)] = error_events
    if loop_events:
        drift_bucket[(_ADK_LOOP_KIND, _SEV_WARNING)] = loop_events
    if transfer_events:
        drift_bucket[(_ADK_TRANSFER_KIND, _SEV_INFO)] = transfer_events

    drift_counts = tuple(
        DriftCount(kind=k, severity=s, count=n)  # type: ignore[arg-type]
        for (k, s), n in sorted(drift_bucket.items())
    )

    warnings: list[str] = []
    if malformed:
        warnings.append(
            f"adk_events dialect: skipped {malformed} malformed line(s) in "
            f"{events_jsonl_path.name}"
        )

    return DialectSignals(
        drift_counts=drift_counts,
        plan_revisions=0,
        task_started=task_started,
        task_failed=task_failed,
        llm_call_count=llm_call_count,
        token_count=token_count,
        agent_text_chars=agent_text_chars,
        run_id=run_id,
        adk_session_id=adk_session_id,
        agent_turns=tuple(agent_turns),
        user_turns=tuple(user_turns),
        malformed_line_count=malformed,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# transcript dialect (the floor)
# ---------------------------------------------------------------------------


def _transcript_lines(objs: list[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    """Yield ``(role, content)`` for each usable transcript line.

    Accepts ``{"role": "user"|"assistant", "content": "…"}`` and the
    ``*_message`` / ``text`` aliases the adk dialect tolerates, so a
    transcript exported in either shape reduces cleanly.
    """
    for obj in objs:
        role = _message_role(obj)
        if role:
            yield role, _message_text(obj)


def reduce_transcript(events_jsonl_path: Path, entry: BoardEntry) -> DialectSignals:
    """Reduce a bare transcript JSONL — the floor tier (§4).

    NO telemetry: no drift, no task counts, no tokens, no plan revisions.
    The drift term is therefore structurally ``0.0`` (the explicit
    zero-drift stance, §4.1). The transcript is reconstructed into
    ``agent_turns`` / ``user_turns`` so the zicato-derived multi-turn
    FEATURE signals (memory-failure / context-loss) still work, and
    ``output:chars`` is summed from the agent-side text. Identity ids are
    empty (a transcript has none), so the reducer falls back to a synthetic
    ``run_id``.
    """
    objs, malformed = _iter_json_objects(events_jsonl_path)

    agent_turns: list[str] = []
    user_turns: list[str] = []
    agent_text_chars = 0
    for role, content in _transcript_lines(objs):
        if role == "agent":
            agent_turns.append(content)
            agent_text_chars += len(content)
        else:
            user_turns.append(content)

    warnings: list[str] = []
    if malformed:
        warnings.append(
            f"transcript dialect: skipped {malformed} malformed line(s) in "
            f"{events_jsonl_path.name}"
        )

    return DialectSignals(
        drift_counts=(),
        plan_revisions=0,
        task_started=0,
        task_failed=0,
        llm_call_count=0,
        token_count=0,
        agent_text_chars=agent_text_chars,
        run_id="",
        adk_session_id="",
        agent_turns=tuple(agent_turns),
        user_turns=tuple(user_turns),
        malformed_line_count=malformed,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Config-validation: capability-mismatch warnings (warn-or-refuse, §4.2)
# ---------------------------------------------------------------------------


def dialect_capability_warnings(weights: ScoringWeights) -> tuple[str, ...]:
    """Warn (recommend-only) about drift knobs inert under the pinned dialect.

    The "warn" half of the warn-or-refuse story (TELEMETRY-DIALECTS.md
    §4.2; the "refuse" half — an unknown dialect NAME — is rejected
    fail-fast in :meth:`ScoringWeights.__post_init__`). A contract that
    tunes drift under a dialect that cannot PRODUCE drift is not an error —
    the drift term is just structurally zero — but the tuning is a silent
    no-op, so we surface it. Pure and side-effect-free; the reducer logs
    the result.

    * ``transcript`` produces NO drift at all, so every drift-shaping knob
      (``drift_weight`` / ``plan_revision_weight`` / ``per_kind_weights`` /
      ``per_judge_weights`` / ``drift_kind_aggregation`` / ``drift_reducer``)
      is inert.
    * ``adk_events`` produces drift but carries NO process-judge
      judgements, so ``per_judge_weights`` (the ``custom:<judge_name>``
      lever) is inert.
    * ``goldfive`` can produce everything ⇒ never warns.
    """
    dialect = weights.telemetry_dialect
    if dialect == DIALECT_GOLDFIVE:
        return ()

    defaults = ScoringWeights()
    out: list[str] = []

    if dialect == DIALECT_TRANSCRIPT:
        if weights.drift_weight != defaults.drift_weight:
            out.append(
                "drift_weight is inert under the 'transcript' dialect: it "
                "produces no drift, so the drift term is structurally 0."
            )
        if weights.plan_revision_weight != defaults.plan_revision_weight:
            out.append(
                "plan_revision_weight is inert under the 'transcript' dialect: "
                "no plan-revision telemetry is produced."
            )
        if dict(weights.per_kind_weights):
            out.append(
                "per_kind_weights is inert under the 'transcript' dialect: no "
                "drift kinds are produced."
            )
        if dict(weights.drift_kind_aggregation):
            out.append(
                "drift_kind_aggregation is inert under the 'transcript' "
                "dialect: no drift kinds are produced."
            )
        if weights.drift_reducer:
            out.append(
                "drift_reducer is inert under the 'transcript' dialect: the "
                "drift term is structurally 0 with no counts to reduce."
            )

    if dialect in (DIALECT_TRANSCRIPT, DIALECT_ADK_EVENTS) and dict(weights.per_judge_weights):
        out.append(
            f"per_judge_weights is inert under the {dialect!r} dialect: an "
            "event log / transcript carries no process-judge judgements, so "
            "no custom:<judge_name> drift is produced."
        )

    return tuple(out)
