"""Post-run reducer: turn a run's goldfive ``events.jsonl`` into a :class:`LossProfile`.

The reducer is the only zicato component that walks raw goldfive events.
Every downstream consumer (pattern detectors, tournament scoring,
journal rendering) reads :class:`LossProfile` instead. That decoupling
is deliberate: event schemas evolve upstream, and we want a single
narrow seam between "what goldfive emits" and "what zicato scores on".

Two reading modes are supported:

1. **Strict mode** — when goldfive is importable, the reducer calls
   :func:`goldfive.sinks.persistence.replay_from_jsonl` which returns
   typed proto messages. Reading these uses the proto reflection API,
   so new fields on existing events are tolerated without code change.
2. **Direct mode** — when goldfive is not importable, or its strict
   parser refuses the file, the reducer reads the JSONL itself.

Either way the lines become records through
:mod:`zicato.telemetry.event_log`, so the payload case and its field
names are spelled the one way the dispatch below keys on, and a file in
the camelCase wire form reduces to the same numbers as the same run in
the snake_case one.

The reducer is a pure function: same JSONL + same inputs → same
:class:`LossProfile`. It does no I/O beyond reading the JSONL and (via
:func:`write_loss_profile`) writing the output.

Multi-turn heuristics
---------------------
For multi-turn entries (``multi_turn_scripted`` / ``multi_turn_emulated``)
the reducer additionally derives two zicato-specific signals from the
agent transcript reconstructed from goldfive events:

* :attr:`LossProfile.memory_failure_count` — count of agent turns
  whose body shares a >= 40-char substring with an earlier agent turn
  (one count per matching ``(i, j)`` pair, after whitespace
  normalisation and lowercasing). Captures "the agent repeated itself
  verbatim across turns" as a cheap proxy for forgetting that it
  already said this.
* :attr:`LossProfile.context_loss_count` — count of agent questions
  (turns ending with ``?``) whose surface form near-duplicates an
  earlier user statement (cosine over character trigrams, threshold
  0.7). Captures "the agent is asking the user for information the
  user already provided".

Both heuristics are best-effort and stay heuristic by design: a
trillion-parameter NL classifier would do better, but the reducer is a
deterministic post-run pass that runs once per run and must not pull
in heavy NLP deps. Single-turn entries leave both fields ``None``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core import (
    DIALECT_ADK_EVENTS,
    DIALECT_GOLDFIVE,
    DIALECT_TRANSCRIPT,
    BoardEntry,
    DriftCount,
    ExpectationKind,
    ExpectationResult,
    JudgeError,
    JudgeLoss,
    LossProfile,
    MetricCount,
    ScoringWeights,
    normalize_wire_drift_kind,
    normalize_wire_severity,
)
from zicato.scoring import DriftContext, builtin_drift_loss, resolve_drift_loss
from zicato.telemetry.dialects import (
    DialectReducer,
    DialectSignals,
    reduce_adk_events,
    reduce_transcript,
)
from zicato.telemetry.event_log import EventRecord, parse_event, read_event_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event walking — wire-form helpers
# ---------------------------------------------------------------------------


# Proto enum integer -> wire-canonical kind string. We mirror
# ``GOLDFIVE_DRIFT_KINDS`` ordering via the proto's enum numbering. This
# is only used when reading a proto-parsed event (where ``kind`` is an
# int). When we fall back to JSON-dict reading, ``MessageToJson`` already
# renders the kind as its uppercase enum name (e.g. ``"DRIFT_KIND_OFF_TOPIC"``)
# which we lowercase + strip prefix on the fly.

_DRIFT_KIND_INT_TO_STR: dict[int, str] = {
    1: "tool_error",
    2: "agent_refusal",
    3: "new_work_discovered",
    4: "plan_divergence",
    5: "user_steer",
    6: "user_cancel",
    7: "task_failed_recoverable",
    8: "task_failed_fatal",
    9: "context_pressure",
    10: "blocked",
    11: "wrong_agent",
    12: "agent_transfer",
    13: "model_refusal",
    14: "stopped_early",
    15: "too_many_steps",
    16: "goal_unreachable",
    17: "task_timeout",
    18: "repeated_failure",
    19: "unexpected_output",
    20: "schema_violation",
    21: "hallucination_suspected",
    22: "safety_concern",
    23: "resource_exhausted",
    24: "ambiguous_intent",
    25: "custom",
    26: "looping_tool_call",
    27: "looping_reasoning",
    29: "off_topic",
    30: "intent_divergence",
    31: "uncertain_progress",
    32: "self_reported_stuck",
    33: "reasoning_cluster_tightening",
    34: "confabulation_risk",
    35: "runaway_delegation",
    36: "refine_validation_failed",
    37: "human_intervention_required",
    38: "goal_drift",
    39: "llm_call_timeout",
    40: "justified_deviation",
    41: "capability_mismatch",
}

_DRIFT_SEVERITY_INT_TO_STR: dict[int, str] = {
    1: "info",
    2: "warning",
    3: "critical",
}


# Wire-form drift-kind / severity normalisation lives in
# ``zicato.core.drift_kinds`` (the single source of truth shared with the
# index ingest path, so the analytical index agrees with the loss
# profile). These module-local names are retained as the reducer's stable
# call surface. The reducer always passes ``str(...)``; the shared impl's
# ``Any`` guard is a no-op on that path, so behaviour is unchanged.
_normalize_drift_kind_str = normalize_wire_drift_kind
_normalize_severity_str = normalize_wire_severity


def _load_events(events_jsonl_path: Path) -> tuple[EventRecord, ...]:
    """Read an events JSONL into event records.

    We prefer goldfive's :func:`replay_from_jsonl` for strict
    proto-message parsing (it gives us typed enum ints and tolerates
    unknown fields), and fall back to reading the file directly when
    goldfive is not importable or its parser refuses the file. Either
    way the records come out of :mod:`zicato.telemetry.event_log`, so
    the payload case and its field names are spelled the one way the
    dispatch below keys on.

    The conversion from proto message → dict uses ``MessageToDict`` with
    ``use_integers_for_enums=False``, so the uppercase enum names that
    ``_normalize_*`` already handle survive.
    """
    try:
        from goldfive.sinks.persistence import replay_from_jsonl
        from google.protobuf.json_format import MessageToDict
    except ModuleNotFoundError:
        return read_event_log(events_jsonl_path).records

    try:
        events = replay_from_jsonl(events_jsonl_path)
    except Exception:  # noqa: BLE001 — goldfive's strict parser is brittle
        # Strict proto-parse failed, typically because goldfive's sink
        # emitted a mix of camelCase and snake_case event shapes within
        # one JSONL file. Read the file directly so the reducer still
        # produces a loss profile.
        return read_event_log(events_jsonl_path).records

    # MessageToDict renamed ``including_default_value_fields`` to
    # ``always_print_fields_with_no_presence`` in newer protobuf releases.
    # We default the value to False either way, which is the historical
    # behaviour, so just leaving the kwarg out is the version-portable
    # choice.
    return tuple(parse_event(MessageToDict(evt, use_integers_for_enums=False)) for evt in events)


# ---------------------------------------------------------------------------
# Drift loss
# ---------------------------------------------------------------------------


def _continuous_score(expectation_result: ExpectationResult) -> float:
    """Coerce an :class:`ExpectationResult` into a clamped ``[0, 1]`` score.

    The single guard the scalar trusts. A rogue scorer must never poison
    the aggregate, so the rules are:

    * a result that already carries a continuous ``score`` is clamped to
      ``[0.0, 1.0]``; a non-finite value (``NaN`` / ``±inf``) is treated
      as an outright MISS (``0.0``) rather than propagating into the mean;
    * otherwise the binary ``passed`` bit maps to ``1.0`` / ``0.0``.

    The bool path is exactly ``float(passed)``, so a board whose entries
    are all bool produces a per-entry score sequence identical to its
    binary pass/fail sequence — which is what makes ``mean_score`` collapse
    to the binary ``pass_rate`` byte-for-byte.
    """
    raw = expectation_result.score
    if raw is None:
        return 1.0 if expectation_result.passed else 0.0
    value = float(raw)
    if not math.isfinite(value):
        # A rogue scorer returned NaN / inf: count it as a miss rather
        # than letting it poison the mean.
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# The bare wire-canonical drift-kind string a custom judge emits. All
# custom judges share this single ``DriftKind`` value — the per-judge
# identity lives on the paired ``JudgementEmitted.judge_name`` rather than on
# the drift kind. The reducer re-attributes a ``custom``-kind drift to
# its authoring judge and stores the result under a namespaced kind of
# the form ``custom:<judge_name>`` (see ``_judge_attributed_kind``).
_CUSTOM_DRIFT_KIND: str = "custom"

# Separator between the ``custom`` namespace and the ``judge_name`` in
# the attributed drift kind. A drift kind ``"custom:slide_quality"``
# means "a CUSTOM-kind drift authored by the judge named
# ``slide_quality``". A plain ``"custom"`` (no separator) is a
# custom-kind drift the reducer could not pair with a judgement — it
# scores at the default judge weight, same as an unconfigured judge.
_JUDGE_KIND_SEP: str = ":"


def _judge_attributed_kind(judge_name: str) -> str:
    """Build the namespaced drift kind for a custom-judge-authored drift.

    ``judge_name`` is the stable per-judge identity carried on the
    paired :class:`JudgementEmitted`. The reducer folds it into the
    :class:`DriftCount.kind` string as ``custom:<judge_name>`` so two
    distinct custom judges occupy distinct drift buckets and
    :func:`compute_drift_loss` can weight them independently via
    :attr:`ScoringWeights.per_judge_weights`.

    An empty / whitespace-only ``judge_name`` yields the bare
    ``"custom"`` kind — an unattributed custom drift, weighted at the
    default.
    """
    name = judge_name.strip()
    if not name:
        return _CUSTOM_DRIFT_KIND
    return f"{_CUSTOM_DRIFT_KIND}{_JUDGE_KIND_SEP}{name}"


def split_judge_attributed_kind(kind: str) -> tuple[bool, str]:
    """Inverse of :func:`_judge_attributed_kind`.

    Returns ``(is_custom, judge_name)``:

    * For ``"custom:<judge_name>"`` → ``(True, "<judge_name>")``.
    * For a bare ``"custom"`` → ``(True, "")`` (an unattributed
      custom drift).
    * For any other kind → ``(False, "")``.

    Exposed (not underscore-private) because :func:`compute_drift_loss`
    is not the only consumer that needs to recover the judge identity
    from a :class:`DriftCount.kind` — analysis / journal-rendering
    callers reading a persisted :class:`LossProfile` do too.
    """
    if kind == _CUSTOM_DRIFT_KIND:
        return True, ""
    prefix = _CUSTOM_DRIFT_KIND + _JUDGE_KIND_SEP
    if kind.startswith(prefix):
        return True, kind[len(prefix) :]
    return False, ""


def compute_per_judge_loss(
    drift_counts: tuple[DriftCount, ...],
    weights: ScoringWeights,
) -> tuple[JudgeLoss, ...]:
    """Group ``drift_counts`` into per-judge loss attributions.

    Walks the run's drift counts, picks out every ``custom`` /
    ``custom:<judge_name>`` entry, sums the severity-weighted counts
    per ``judge_name``, multiplies by the judge's
    :attr:`ScoringWeights.per_judge_weights` value (falling back to
    :attr:`ScoringWeights.default_judge_weight` for unconfigured
    judges), and returns one :class:`JudgeLoss` per attributed judge.

    This split is how custom judges reach the scalar: each entry becomes a
    ``judge:<name>`` metric of the ``judge:`` channel
    (:meth:`zicato.core.LossProfile.unified_metrics`), and
    :func:`compute_drift_loss` excludes judge-attributed kinds from the
    ``drift:`` channel so the same event is never charged twice.

    A run with no custom-kind drift returns the empty tuple. Drifts
    whose attribution is empty (the unattributed bare ``custom`` kind)
    are accumulated under the ``""`` judge_name, a catch-all bucket that
    weighs at :attr:`default_judge_weight`.

    The returned tuple is sorted by ``judge_name`` so the order is
    deterministic across runs of the same reducer; that makes diffs
    against the persisted ``loss.json`` stable.
    """
    sev_w = weights.severity_weights
    raw_by_judge: dict[str, float] = {}
    for c in drift_counts:
        is_custom, judge_name = split_judge_attributed_kind(c.kind)
        if not is_custom:
            continue
        sev_mult = sev_w.get(c.severity, 0.0)
        raw_by_judge[judge_name] = raw_by_judge.get(judge_name, 0.0) + sev_mult * c.count

    out: list[JudgeLoss] = []
    for judge_name in sorted(raw_by_judge.keys()):
        raw_loss = raw_by_judge[judge_name]
        # A judge listed in per_judge_weights wins; an unlisted judge (or the
        # bare unattributed bucket "") falls back to default_judge_weight.
        weight = weights.per_judge_weights.get(judge_name, weights.default_judge_weight)
        weighted_loss = raw_loss * weight
        out.append(
            JudgeLoss(
                judge_name=judge_name,
                raw_loss=float(raw_loss),
                weight=float(weight),
                weighted_loss=float(weighted_loss),
            )
        )
    return tuple(out)


def compute_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    weights: ScoringWeights,
    *,
    task_failure_ratio: float = 0.0,
    runtime_ms: int = 0,
) -> float:
    """Compute the ``drift:`` channel's per-run term.

    The formula is::

        loss = fsum(
            severity_weights[c.severity] * per_kind_weights(c.kind) * c.count
            for c in drift_counts if not judge-attributed
        )
        + weights.plan_revision_weight * plan_revisions

    where a first-class drift kind resolves through
    ``per_kind_weights.get(kind, 1.0)``. Custom-judge kinds (``custom`` /
    ``custom:<judge_name>``) are EXCLUDED: they are scored in the ``judge:``
    channel off :func:`compute_per_judge_loss`, and charging them here as
    well would double-count.

    Both terms are non-negative on legal inputs, so the return value is
    non-negative; we clamp to zero defensively in case weights are
    set unusually.

    ``task_failure_ratio`` and ``runtime_ms`` are not part of this formula —
    they are the ``failure:`` and ``runtime:`` channels — but they are
    accepted so a drift PLUGIN, which sees the whole
    :class:`~zicato.scoring.api.DriftContext`, can read the outcome of the
    run it is scoring. They default to the neutral "no failure, no elapsed
    time" values for callers that only want the drift term.

    Seam-1 dispatch
    ---------------
    The formula itself lives in
    :func:`zicato.scoring.builtins.builtin_drift_loss` (importable from
    BOTH the orchestrator and the killable worker), and this function
    routes through :func:`zicato.scoring.dispatch.resolve_drift_loss` —
    the single seam declarative transforms and dotted-spec plugins plug
    into.
    """
    loss, _provenance = resolve_drift_loss(
        DriftContext(
            drift_counts=drift_counts,
            plan_revisions=plan_revisions,
            task_failure_ratio=task_failure_ratio,
            runtime_ms=runtime_ms,
            weights=weights,
            builtin_loss=builtin_drift_loss(
                drift_counts=drift_counts,
                plan_revisions=plan_revisions,
                weights=weights,
            ),
        )
    )
    return loss


# ---------------------------------------------------------------------------
# Multi-turn heuristics
# ---------------------------------------------------------------------------


# Minimum length of a shared substring before it counts as a memory
# failure. 40 chars filters out boilerplate ("Sure, I can help with
# that.") which two unrelated turns might share by accident; a 40-char
# repeated chunk is almost always semantic content the agent literally
# re-said.
_MEMORY_FAILURE_MIN_SUBSTRING_LEN: int = 40

# Cosine threshold for the context-loss heuristic. A trigram cosine of
# 0.7 between an agent question and an earlier user statement is high
# enough to imply "the agent is asking what the user already told it"
# while staying below the false-positive floor of generic phrases.
_CONTEXT_LOSS_TRIGRAM_THRESHOLD: float = 0.7


def _normalise_for_substring(text: str) -> str:
    """Whitespace-collapse + lowercase a turn body for substring matching."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def _has_shared_substring(a: str, b: str, min_len: int) -> bool:
    """Return ``True`` iff ``a`` and ``b`` share a substring of length >= ``min_len``.

    Implemented with a rolling set of length-``min_len`` windows over
    ``a`` and a sliding compare over ``b``. O(len(a) + len(b)) memory,
    O((len(a) - min_len) + (len(b) - min_len)) time — cheap enough for
    transcripts and easy to reason about. We avoid the
    ``difflib.SequenceMatcher.find_longest_match`` path because for our
    boolean question ("any shared chunk >= N?") it does more work than
    we need.
    """
    if len(a) < min_len or len(b) < min_len:
        return False
    # Index every length-min_len window of a; scan b for any hit.
    windows = set()
    for i in range(0, len(a) - min_len + 1):
        windows.add(a[i : i + min_len])
    for j in range(0, len(b) - min_len + 1):
        if b[j : j + min_len] in windows:
            return True
    return False


def _trigrams(text: str) -> Counter[str]:
    """Character-trigram bag of ``text`` after a light normalisation.

    We lowercase and collapse whitespace before chunking, so phrasing
    differences in spacing / capitalisation do not artificially deflate
    the cosine. The trigram window slides over every 3-char span.
    """
    norm = re.sub(r"\s+", " ", text.strip()).lower()
    if len(norm) < 3:
        return Counter()
    return Counter(norm[i : i + 3] for i in range(len(norm) - 2))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity between two trigram bags. Returns 0.0 on empty input."""
    if not a or not b:
        return 0.0
    # Iterate the smaller bag for the dot product.
    if len(b) < len(a):
        a, b = b, a
    dot = 0
    for tri, ca in a.items():
        dot += ca * b.get(tri, 0)
    if dot == 0:
        return 0.0
    norm_a = math.fsum(v * v for v in a.values()) ** 0.5
    norm_b = math.fsum(v * v for v in b.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _memory_failure_count(agent_turns: list[str]) -> int:
    """Number of agent turn pairs (i, j) with a shared >= 40-char substring.

    Pairs are walked with ``i < j``; one count per matching pair (so a
    turn that duplicates two earlier turns contributes 2). We dedupe by
    "one count per matching turn-pair", not "one count per duplicated
    turn", because the latter would compress a 3-way repeat into 1 and
    a 4-way repeat into 1 — losing useful magnitude on agents that
    spam the same paragraph.
    """
    count = 0
    norm = [_normalise_for_substring(t) for t in agent_turns]
    for j in range(1, len(norm)):
        for i in range(0, j):
            if _has_shared_substring(norm[i], norm[j], _MEMORY_FAILURE_MIN_SUBSTRING_LEN):
                count += 1
    return count


def _context_loss_count(agent_turns: list[str], user_turns: list[str]) -> int:
    """Count agent questions that near-duplicate an earlier user statement.

    An agent turn counts if:
      * its stripped body ends with ``?`` (it's a question), AND
      * the cosine of its character trigrams against ANY earlier user
        statement's trigrams meets or exceeds the threshold.

    "Earlier" is positional in the transcript walk; we assume
    ``agent_turns`` and ``user_turns`` arrive in conversation order
    (caller's responsibility). When we don't have positional
    alignment between the two lists we approximate "earlier" as "any
    user statement we've seen so far in walking the conversation".

    A user statement contributes only if it is not itself a question
    (asking a question doesn't constitute the user "telling" something).
    """
    user_grams = [
        (_trigrams(u), u) for u in user_turns if u.strip() and not u.strip().endswith("?")
    ]
    count = 0
    for turn in agent_turns:
        body = turn.strip()
        if not body.endswith("?"):
            continue
        agent_g = _trigrams(turn)
        if not agent_g:
            continue
        for user_g, _ in user_grams:
            if not user_g:
                continue
            if _cosine(agent_g, user_g) >= _CONTEXT_LOSS_TRIGRAM_THRESHOLD:
                count += 1
                break
    return count


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


def _extract_drift_buckets(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull ``(kind, severity)`` strings from a ``DriftDetected`` payload dict."""
    raw_kind = payload.get("kind", "")
    raw_sev = payload.get("severity", "")
    # Proto-parsed events with use_integers_for_enums=False give us the
    # uppercase enum name; the JSON-fallback path produces the same; if
    # someone hand-wrote integers we accept those too.
    if isinstance(raw_kind, int):
        kind = _DRIFT_KIND_INT_TO_STR.get(raw_kind)
    else:
        kind = _normalize_drift_kind_str(str(raw_kind))
    if isinstance(raw_sev, int):
        sev = _DRIFT_SEVERITY_INT_TO_STR.get(raw_sev)
    else:
        sev = _normalize_severity_str(str(raw_sev))
    return kind, sev


def _judgement_judge_name(payload: dict[str, Any]) -> str | None:
    """Recover the authoring ``judge_name`` from a ``JudgementEmitted`` payload.

    Returns the ``judge_name`` only when the judgement carries a
    **drift-flavoured** verdict (``verdict_kind == "drift"``) — those
    are the judgements paired one-for-one with a ``DriftDetected``
    that the reducer needs to attribute. Rubric / boolean / numeric
    judgements do not mint a ``DriftDetected`` and are irrelevant to
    drift attribution, so we return ``None`` for them.

    Returns ``None`` (rather than ``""``) when there is nothing
    usable — no ``judge_name``, or a non-drift verdict — so the caller
    can distinguish "no pairing candidate" from "a drift judgement
    whose ``judge_name`` happens to be empty" (the latter still pairs,
    just at the default weight).

    Field names arrive in one spelling whichever wire shape the line used,
    because the reader normalises them before the reducer sees them.
    """
    verdict_kind = str(payload.get("verdict_kind", "") or "")
    if verdict_kind != "drift":
        return None
    return str(payload.get("judge_name", "") or "")


def _agent_and_user_turns_from_events(
    events: tuple[EventRecord, ...],
) -> tuple[list[str], list[str]]:
    """Best-effort transcript reconstruction from goldfive events.

    Goldfive's event stream does not carry user-facing assistant /
    user messages as first-class payloads (those live in the harness
    transcript rather than in the event wire). However, several payloads do
    carry short text fragments — agent invocation summaries,
    completion summaries, conversation-end reasons — that the
    multi-turn heuristics can operate on without depending on a richer
    transcript adapter.

    We extract:

      * agent text: ``AgentInvocationCompleted.summary``,
        ``TaskCompleted.summary``, and ``RunCompleted.outcome_summary``
        — these are the closest proxies to "what the agent said" that
        live in the event stream itself.
      * user text: ``RunStarted.goal_summary`` — the closest proxy to
        "what the user asked for". Also useful for the context-loss
        heuristic.

    When operators want fuller transcript-grounded heuristics, they
    should pre-populate the events file with synthesised payloads
    (e.g. user-message-as-RunStarted on each turn) or call the
    heuristic helpers directly with a richer transcript. The reducer
    stays usable on bare goldfive output by best-effort fallback.
    """
    agent_turns: list[str] = []
    user_turns: list[str] = []
    for evt in events:
        key, payload = evt.case, evt.payload
        if key == "agent_invocation_completed":
            s = payload.get("summary", "")
            if s:
                agent_turns.append(s)
        elif key == "task_completed":
            s = payload.get("summary", "")
            if s:
                agent_turns.append(s)
        elif key == "run_completed":
            s = payload.get("outcome_summary", "")
            if s:
                agent_turns.append(s)
        elif key == "run_started":
            s = payload.get("goal_summary", "")
            if s:
                user_turns.append(s)
    return agent_turns, user_turns


def _goldfive_signals(events_jsonl_path: Path, entry: BoardEntry) -> DialectSignals:
    """The ``goldfive`` dialect producer — the historical event walk (Seam 0).

    Reads the drift-instrumented ``events.jsonl`` (via goldfive's replay
    helper when available, plain JSON otherwise) and aggregates the raw
    signals into a :class:`~zicato.telemetry.dialects.DialectSignals`. This
    is the DEFAULT dialect and the most powerful: only this producer can
    carry in-process drift instruments, custom process-judge drift
    (``custom:<judge_name>``), and the collusion-guarded emulator lane
    (TELEMETRY-DIALECTS.md §2).

    The transcript reconstruction is computed unconditionally here, and the
    result is used only for non-``single_turn`` entries.
    """
    events: tuple[EventRecord, ...] = ()
    if events_jsonl_path.exists():
        events = _load_events(events_jsonl_path)

    drift_bucket: dict[tuple[str, str], int] = {}
    plan_revisions = 0
    task_started = 0
    task_failed = 0
    llm_call_count = 0
    token_count = 0
    agent_text_chars = 0
    run_id = ""
    adk_session_id = ""
    # Custom-judge drift attribution. The steerer emits a drift-flavoured
    # ``JudgementEmitted`` IMMEDIATELY before the paired ``DriftDetected``,
    # and custom judges fire in a sequential loop, so each (judgement,
    # drift) pair is contiguous on the wire. ``pending_judge_name`` holds
    # the ``judge_name`` of the most recent unconsumed drift-flavoured
    # judgement; the next ``DriftDetected`` of any kind consumes it.
    pending_judge_name: str | None = None
    for evt in events:
        if not run_id:
            run_id = evt.run_id
        if not adk_session_id:
            adk_session_id = evt.session_id
        key, payload = evt.case, evt.payload
        if key == "judgement_emitted":
            jn = _judgement_judge_name(payload)
            if jn is not None:
                pending_judge_name = jn
        elif key == "drift_detected":
            kind, sev = _extract_drift_buckets(payload)
            paired_judge = pending_judge_name
            pending_judge_name = None
            if kind is None or sev is None:
                continue
            if kind == _CUSTOM_DRIFT_KIND:
                kind = _judge_attributed_kind(paired_judge or "")
            drift_bucket[(kind, sev)] = drift_bucket.get((kind, sev), 0) + 1
        elif key == "plan_revised":
            plan_revisions += 1
        elif key == "task_started":
            task_started += 1
        elif key == "task_failed":
            task_failed += 1
        elif key == "goldfive_llm_call_end":
            llm_call_count += 1
            # Token counts are not on the canonical goldfive proto but MAY be
            # attached as extension fields by callers that wrap their LLM SDK
            # with token-accounting middleware. Read opportunistically.
            for tk in ("input_tokens", "output_tokens", "tokens", "total_tokens"):
                v = payload.get(tk)
                if isinstance(v, int | float):
                    token_count += int(v)
        elif key == "agent_invocation_completed":
            s = payload.get("summary", "")
            if isinstance(s, str):
                agent_text_chars += len(s)
        elif key == "task_completed":
            s = payload.get("summary", "")
            if isinstance(s, str):
                agent_text_chars += len(s)

    drift_counts = tuple(
        DriftCount(kind=k, severity=s, count=n)  # type: ignore[arg-type]
        for (k, s), n in sorted(drift_bucket.items())
    )

    agent_turns, user_turns = _agent_and_user_turns_from_events(events)

    return DialectSignals(
        drift_counts=drift_counts,
        plan_revisions=plan_revisions,
        task_started=task_started,
        task_failed=task_failed,
        llm_call_count=llm_call_count,
        token_count=token_count,
        agent_text_chars=agent_text_chars,
        run_id=run_id,
        adk_session_id=adk_session_id,
        agent_turns=tuple(agent_turns),
        user_turns=tuple(user_turns),
    )


#: The dialect registry: name → producer. The ``goldfive`` slot is the
#: historical walk (byte-identical default); the others are the alternative
#: producers in :mod:`zicato.telemetry.dialects`. Keyed on the closed
#: ``KNOWN_TELEMETRY_DIALECTS`` set that ``ScoringWeights.__post_init__``
#: validates against, so an unknown name never reaches dispatch.
_DIALECT_PRODUCERS: dict[str, DialectReducer] = {
    DIALECT_GOLDFIVE: _goldfive_signals,
    DIALECT_ADK_EVENTS: reduce_adk_events,
    DIALECT_TRANSCRIPT: reduce_transcript,
}


def _resolve_dialect_producer(dialect: str) -> DialectReducer:
    """Look up the producer for ``dialect`` (fail-open to ``goldfive``).

    The contract loader validates the name against
    :data:`~zicato.core.KNOWN_TELEMETRY_DIALECTS`, so an unknown name here
    would be a corrupt args file — we fall open to the default producer
    rather than crash the worker mid-reduction.
    """
    return _DIALECT_PRODUCERS.get(dialect, _goldfive_signals)


def dialect_producer(dialect: str) -> DialectReducer:
    """Public accessor for a dialect's ``DialectSignals`` producer.

    The stable seam the trajectory importer (TRAJECTORY-BOOTSTRAP.md §3.1)
    reduces a foreign trace through: it sniffs the file's dialect, resolves the
    producer here, and calls it with a synthetic placeholder ``BoardEntry``
    (§2.1 — no producer reads ``entry``). Fail-open to ``goldfive`` for an
    unknown name, mirroring :func:`_resolve_dialect_producer`.
    """
    return _resolve_dialect_producer(dialect)


def reduce_loss(
    events_jsonl_path: Path,
    entry: BoardEntry,
    generation_id: str,
    epoch_id: str,
    expectation_result: ExpectationResult | None,
    runtime_ms: int,
    wall_clock_budget_exceeded: bool,
    weights: ScoringWeights,
    final_output: str | None = None,
    run_not_completed: bool = False,
) -> LossProfile:
    """Build a :class:`LossProfile` for one run.

    Reads ``events_jsonl_path`` (via goldfive's replay helper when
    available, plain JSON otherwise), counts the signals the contract
    pins, and computes the weighted drift-loss scalar via
    :func:`compute_drift_loss`. For multi-turn entries, additionally
    runs the memory-failure / context-loss heuristics over the
    best-effort transcript reconstruction.

    Not-completed penalty
    ---------------------
    A run that did **not** complete successfully is scored worst-case,
    never zero. This covers every non-success terminal state:

    * ``wall_clock_budget_exceeded`` — the run hit its wall-clock
      budget and was force-aborted.
    * ``run_not_completed`` — the run terminated abnormally for any
      other reason: the harness raised an exception (a crash), the
      emulator's answer-leak heuristic aborted it, the scripted /
      emulated driver was unavailable, the adapter rejected the entry
      kind, or the worker process was killed.

    Either flag records the SAME two facts, exactly once even when both
    are set: the reducer floors ``task_failure_ratio`` to ``1.0`` (so the
    ``failure:tasks`` term contributes its maximum) and sets
    ``not_completed``, which charges ``not_completed_weight`` in the same
    channel. This is deliberate: without it a run that crashes instantly
    leaves an empty events file, the reducer counts zero drift, and the
    run earns a loss of ``0.0`` — the BEST possible score. A challenger
    generation could then win a tournament simply by failing fast. The
    floor + the fixed charge make any non-completing run unambiguously
    worst-case, consistent with how a watchdog-killed run is already
    scored by the runner's :func:`_aborted_loss_profile`.

    The ``run_id`` field on the returned profile is derived from the
    first event's ``run_id`` envelope field; when the events file is
    empty (a run that aborted before the first emit), we fall back to
    a synthetic id of the form ``f"{generation_id}:{entry.id}"``.

    Generalised metric surface
    --------------------------
    Alongside ``drift_counts``, the reducer populates
    :attr:`LossProfile.metric_counts` with namespaced :class:`MetricCount`
    entries — every drift entry under the ``"drift:"`` namespace plus
    per-namespace derivations:

    * ``"cost:llm_calls"`` — count of ``goldfive_llm_call_end`` events
      observed. Used as the canonical cost proxy when the events file
      does not carry token counts.
    * ``"cost:tokens_spent"`` — populated when ``goldfive_llm_call_end``
      payloads carry ``input_tokens`` / ``output_tokens`` extension
      fields; silently zero (and the metric is suppressed) when absent.
    * ``"output:chars"`` — length of ``final_output`` when supplied;
      falls back to summed lengths of agent-side text payloads.
    * ``"schema:failures"`` — count of drift events with kind
      ``schema_violation``. Mirrored as a first-class scalar for
      analysis-side convenience.

    The first-class scalar fields :attr:`LossProfile.tokens_spent`,
    :attr:`LossProfile.output_chars`, and
    :attr:`LossProfile.schema_failures` are populated to agree with the
    corresponding MetricCount entries — single source of truth.

    Parameters
    ----------
    final_output:
        Optional final user-facing output from the run. When supplied,
        ``output_chars`` is taken as its length; otherwise the reducer
        approximates from summed agent text payloads. Back-compat: the
        parameter is optional so existing callers don't need to change.
    run_not_completed:
        ``True`` when the run terminated abnormally for a reason other
        than the wall-clock budget — a harness crash, an emulator
        answer-leak abort, an unavailable driver, or a killed worker.
        Triggers the same not-completed penalty as
        ``wall_clock_budget_exceeded`` (see "Not-completed penalty"
        above). Optional, defaulting to ``False``, which is also what a
        profile omitting the key reads back as.
    """
    # --- 1. Produce raw signals via the pinned telemetry dialect (Seam 0) ---
    #
    # The dialect (TELEMETRY-DIALECTS.md) is the pluggable PRODUCER: it turns
    # the run's raw telemetry into the LossProfile inputs. ``goldfive`` (the
    # default) is the historical drift-instrument walk, byte-identical;
    # ``adk_events`` reduces a generic agent event log; ``transcript`` is the
    # predicate/judge-only floor. Everything below this line is
    # DIALECT-AGNOSTIC — it scores the raw signals identically regardless of
    # which producer emitted them. The dialect rides ``weights`` (an
    # evaluation-contract property) so it reaches BOTH the orchestrator and
    # this reducer (in the killable worker) with no new call-site plumbing.
    producer = _resolve_dialect_producer(weights.telemetry_dialect)
    signals = producer(events_jsonl_path, entry)
    # Surface per-run reduction warnings (malformed lines). Advisory only.
    #
    # NOTE: the dialect CAPABILITY warnings (drift knobs inert under a
    # drift-incapable dialect — the "warn" half of TELEMETRY-DIALECTS.md
    # §4.2) are NOT emitted here. They are a pure function of the contract's
    # weights rather than of this run, so emitting them per board-unit — inside the
    # killable worker, once per entry × replicate × generation — was pure
    # duplication and invisible. They are now surfaced ONCE per invocation at
    # the contract-load preflight (LOGGING.md §6,
    # ``evolve.loop.emit_dialect_capability_warnings``).
    for warning in signals.warnings:
        log.warning("telemetry reduction [%s]: %s", weights.telemetry_dialect, warning)

    drift_counts = signals.drift_counts
    plan_revisions = signals.plan_revisions
    task_started = signals.task_started
    task_failed = signals.task_failed
    llm_call_count = signals.llm_call_count
    token_count = signals.token_count
    agent_text_chars = signals.agent_text_chars
    run_id = signals.run_id
    adk_session_id = signals.adk_session_id

    # Schema-failure scalar derived from the drift counts. Folds the
    # `schema_violation` kind into a first-class metric so analysis
    # sites that care about schema health don't have to re-walk drift.
    schema_failures = sum(dc.count for dc in drift_counts if dc.kind == "schema_violation")

    # Output-chars: prefer the caller's explicit final_output length
    # (single source of truth for the user-facing surface); otherwise
    # fall back to summed agent text.
    if final_output is not None:
        output_chars = len(final_output)
    else:
        output_chars = agent_text_chars

    if task_started > 0:
        task_failure_ratio = task_failed / task_started
    else:
        # No tasks observed → cannot derive a meaningful ratio. We
        # report 0.0 rather than NaN so the scalar stays well-defined.
        task_failure_ratio = 0.0
    # Clamp into [0.0, 1.0] (a recoverable failure that later retries
    # could otherwise push the numerator above the denominator in
    # pathological event orderings).
    task_failure_ratio = max(0.0, min(1.0, task_failure_ratio))

    # A run that did not complete successfully is scored worst-case.
    # Floor ``task_failure_ratio`` to its maximum so the ``failure:tasks``
    # term contributes fully even when the run crashed before emitting a
    # single ``task_failed`` event (the common case — an instant harness
    # exception leaves an empty events file). The fixed not-completed
    # magnitude rides the same channel, off the ``not_completed`` flag
    # recorded on the profile. Both ``wall_clock_budget_exceeded`` and
    # ``run_not_completed`` are non-success terminal states; either one
    # triggers the floor.
    not_completed = bool(wall_clock_budget_exceeded or run_not_completed)
    if not_completed:
        task_failure_ratio = 1.0

    # --- 2. Multi-turn heuristics ---

    turns_completed: int | None = None
    memory_failure_count: int | None = None
    context_loss_count: int | None = None
    if entry.kind != "single_turn":
        agent_turns = list(signals.agent_turns)
        user_turns = list(signals.user_turns)
        # ``turns_completed`` is a positional count of agent-side turns
        # observed on the wire; for the scripted / emulated kinds this
        # is what operators want surfaced in the journal.
        turns_completed = len(agent_turns)
        memory_failure_count = _memory_failure_count(agent_turns)
        context_loss_count = _context_loss_count(agent_turns, user_turns)

    # --- 3. Compute drift loss (Seam 1) ---
    #
    # Route through the scoring dispatcher so the provenance marker is
    # captured for ``loss.json``. The drift channel is drift EVENTS only;
    # the run's outcome is carried on the profile (``task_failure_ratio`` /
    # ``not_completed``) and scored in the ``failure:`` channel.
    drift_loss, scoring_provenance = resolve_drift_loss(
        DriftContext(
            drift_counts=drift_counts,
            plan_revisions=plan_revisions,
            task_failure_ratio=task_failure_ratio,
            runtime_ms=runtime_ms,
            weights=weights,
            builtin_loss=builtin_drift_loss(
                drift_counts=drift_counts,
                plan_revisions=plan_revisions,
                weights=weights,
            ),
        )
    )

    # --- 4. Pass/fail + continuous-score derivation ---

    pass_fail: bool | None = None
    score: float | None = None
    metrics: dict[str, float] | None = None
    if expectation_result is not None:
        pass_fail = expectation_result.passed
        # Record the per-entry continuous score (bool -> 1.0/0.0, a float
        # clamped to [0,1], NaN/inf treated as a miss). The bool case is
        # exactly float(passed), so an all-bool board yields a score
        # sequence identical to its pass/fail sequence — mean_score then
        # equals pass_rate byte-for-byte (see tournament.scoring).
        score = _continuous_score(expectation_result)
        # Carry the scorer's optional decomposition (precision/recall/...)
        # straight through; coerce to float and drop non-finite entries so
        # loss.json never serialises a NaN.
        if expectation_result.metrics:
            cleaned: dict[str, float] = {}
            for key, raw_val in expectation_result.metrics.items():
                val = float(raw_val)
                if math.isfinite(val):
                    cleaned[str(key)] = val
            metrics = cleaned or None

    if not run_id:
        run_id = f"{generation_id}:{entry.id}"

    # Build the generalised metric_counts superset: drift entries lifted
    # under the "drift:" namespace, plus per-namespace cost / output /
    # schema metrics. Always emit cost:llm_calls (even at zero) so
    # downstream analysis can rely on the key being present; the other
    # entries are emitted only when non-zero to keep the JSON compact.
    metric_counts_list: list[MetricCount] = [
        MetricCount.from_drift_count(dc) for dc in drift_counts
    ]
    metric_counts_list.append(
        MetricCount(name="cost:llm_calls", severity="", count=float(llm_call_count))
    )
    if token_count:
        metric_counts_list.append(
            MetricCount(name="cost:tokens_spent", severity="", count=float(token_count))
        )
    if output_chars:
        metric_counts_list.append(
            MetricCount(name="output:chars", severity="", count=float(output_chars))
        )
    if schema_failures:
        metric_counts_list.append(
            MetricCount(name="schema:failures", severity="", count=float(schema_failures))
        )

    per_judge_loss = compute_per_judge_loss(drift_counts, weights)

    return LossProfile(
        run_id=run_id,
        entry_id=entry.id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=drift_counts,
        plan_revisions=plan_revisions,
        task_failure_ratio=task_failure_ratio,
        runtime_ms=runtime_ms,
        wall_clock_budget_exceeded=wall_clock_budget_exceeded,
        not_completed=not_completed,
        expectation_result=expectation_result,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        turns_completed=turns_completed,
        memory_failure_count=memory_failure_count,
        context_loss_count=context_loss_count,
        metric_counts=tuple(metric_counts_list),
        tokens_spent=token_count,
        output_chars=output_chars,
        schema_failures=schema_failures,
        adk_session_id=adk_session_id,
        per_judge_loss=per_judge_loss,
        score=score,
        metrics=metrics,
        scoring_provenance=scoring_provenance,
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _profile_to_dict(profile: LossProfile) -> dict[str, Any]:
    """Render :class:`LossProfile` into a JSON-serialisable dict.

    We use :func:`dataclasses.asdict` for the body, which recursively
    unpacks the nested :class:`DriftCount` tuple and
    :class:`ExpectationResult`. Tuples are converted to lists by
    ``asdict``, which is the correct JSON-side shape; the inverse
    reader re-tuples them on the way back.
    """
    return asdict(profile)


def write_loss_profile(profile: LossProfile, target_path: Path) -> None:
    """Serialise ``profile`` to ``target_path`` as JSON.

    The path is created together with its parent directories so the
    caller can hand in a path under a generation directory that has not
    been pre-created. Writes are atomic-by-convention only: we write
    the bytes and close. A crash between write and close would leave a
    truncated file; the workspace assumes the reducer is run once per
    completed run and so a partial file is treated as "rerun the
    reducer".
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _profile_to_dict(profile)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, indent=2)
        f.write("\n")


def read_loss_profile(path: Path) -> LossProfile:
    """Read a :class:`LossProfile` previously written by :func:`write_loss_profile`.

    Inverse of :func:`write_loss_profile`; the decode itself lives in
    :func:`loss_profile_from_dict`, which the archived copies of a
    displaced profile (``loss.archive.jsonl``, issue #122) share.
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return loss_profile_from_dict(d)


def loss_profile_from_dict(d: dict[str, Any]) -> LossProfile:
    """Rebuild a :class:`LossProfile` from its persisted JSON object.

    Re-tuples ``drift_counts`` (which JSON renders as a list) and
    re-constructs the nested :class:`DriftCount` and
    :class:`ExpectationResult` dataclasses.

    Back-compat: profiles written before the generalised metric surface
    omit ``metric_counts`` / ``tokens_spent`` / ``output_chars`` /
    ``schema_failures``. The reader treats them as the dataclass
    defaults (empty tuple / 0) so old JSON loads cleanly. New consumers
    that want the merged view should call
    :meth:`LossProfile.unified_metrics`. Same for ``judge_errors``: a
    profile written before per-judge error provenance existed — and every
    profile of a run whose judges all returned — carries no such key and
    loads as the empty tuple.
    """
    drift_counts = tuple(
        DriftCount(
            kind=c["kind"],
            severity=c["severity"],
            count=int(c["count"]),
        )
        for c in d.get("drift_counts", ())
    )
    metric_counts = tuple(
        MetricCount(
            name=str(m.get("name", "")),
            severity=m.get("severity", ""),
            count=float(m.get("count", 0.0)),
        )
        for m in d.get("metric_counts", ())
        if isinstance(m, dict) and m.get("name")
    )
    per_judge_loss = tuple(
        JudgeLoss(
            judge_name=str(j.get("judge_name", "")),
            raw_loss=float(j.get("raw_loss", 0.0) or 0.0),
            weight=float(j.get("weight", 0.0) or 0.0),
            weighted_loss=float(j.get("weighted_loss", 0.0) or 0.0),
        )
        for j in d.get("per_judge_loss", ())
        if isinstance(j, dict)
    )
    judge_errors = tuple(
        JudgeError(
            judge_name=str(j.get("judge_name", "")),
            invocations=int(j.get("invocations", 0) or 0),
            errors=int(j.get("errors", 0) or 0),
            last_error_type=str(j.get("last_error_type", "") or ""),
        )
        for j in d.get("judge_errors", ())
        if isinstance(j, dict)
    )
    exp = d.get("expectation_result")
    expectation_result: ExpectationResult | None
    if exp is None:
        expectation_result = None
    else:
        exp_score = exp.get("score")
        exp_metrics_raw = exp.get("metrics")
        exp_metrics = (
            {str(k): float(v) for k, v in exp_metrics_raw.items()}
            if isinstance(exp_metrics_raw, dict)
            else None
        )
        expectation_result = ExpectationResult(
            # ``kind`` is declared :class:`ExpectationKind`; coerce so a
            # profile read back off disk carries the same runtime type as the
            # one the matcher produced in-process (issue #132). An invalid
            # token raises ``ValueError``, which is what every caller of this
            # reader already catches alongside the ``KeyError`` the direct
            # indexing on this same line has always been able to raise.
            #
            # "Every caller catches it" is NOT "every caller degrades
            # harmlessly". The sharp one is
            # ``unit_cache._resolve_cached_unit``: it catches and returns
            # ``None``, i.e. a cache MISS, so an undecodable token re-runs the
            # unit instead of reusing a profile whose kind is meaningless.
            # That is the right trade — but on a round whose token or
            # wall-clock budget is already spent the unit is not re-run:
            # ``scheduling._skip_unit_side`` synthesises a
            # ``wall_clock_budget_exceeded`` profile and overwrites the real
            # measurement with it. Unreachable today (the enum's five members
            # have never changed and only ``write_loss_profile`` writes the
            # field), and the guard against making it reachable is the
            # forward-compat note on :class:`ExpectationKind` itself.
            kind=ExpectationKind(exp["kind"]),
            passed=bool(exp["passed"]),
            detail=exp.get("detail", ""),
            score=float(exp_score) if exp_score is not None else None,
            metrics=exp_metrics,
        )
    score_raw = d.get("score")
    metrics_raw = d.get("metrics")
    metrics = (
        {str(k): float(v) for k, v in metrics_raw.items()}
        if isinstance(metrics_raw, dict)
        else None
    )
    return LossProfile(
        run_id=d["run_id"],
        entry_id=d["entry_id"],
        generation_id=d["generation_id"],
        epoch_id=d["epoch_id"],
        drift_counts=drift_counts,
        plan_revisions=int(d["plan_revisions"]),
        task_failure_ratio=float(d["task_failure_ratio"]),
        runtime_ms=int(d["runtime_ms"]),
        wall_clock_budget_exceeded=bool(d["wall_clock_budget_exceeded"]),
        not_completed=bool(d.get("not_completed", False)),
        expectation_result=expectation_result,
        drift_loss=float(d["drift_loss"]),
        pass_fail=d.get("pass_fail"),
        turns_completed=d.get("turns_completed"),
        memory_failure_count=d.get("memory_failure_count"),
        context_loss_count=d.get("context_loss_count"),
        metric_counts=metric_counts,
        tokens_spent=int(d.get("tokens_spent", 0) or 0),
        output_chars=int(d.get("output_chars", 0) or 0),
        schema_failures=int(d.get("schema_failures", 0) or 0),
        adk_session_id=str(d.get("adk_session_id", "") or ""),
        match_id=str(d.get("match_id", "") or ""),
        per_judge_loss=per_judge_loss,
        judge_errors=judge_errors,
        cached=bool(d.get("cached", False)),
        source_epoch=str(d.get("source_epoch", "") or ""),
        source_run=str(d.get("source_run", "") or ""),
        score=float(score_raw) if score_raw is not None else None,
        metrics=metrics,
        scoring_provenance=(
            str(d["scoring_provenance"]) if d.get("scoring_provenance") is not None else None
        ),
        abort_cause=(str(d["abort_cause"]) if d.get("abort_cause") is not None else None),
        not_completed_reason=(
            str(d["not_completed_reason"]) if d.get("not_completed_reason") is not None else None
        ),
        started_at=(str(d["started_at"]) if d.get("started_at") is not None else None),
        ended_at=(str(d["ended_at"]) if d.get("ended_at") is not None else None),
    )


__all__ = [
    "reduce_loss",
    "compute_drift_loss",
    "compute_per_judge_loss",
    "split_judge_attributed_kind",
    "read_loss_profile",
    "loss_profile_from_dict",
    "write_loss_profile",
    "dialect_producer",
]
