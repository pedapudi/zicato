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
2. **Fallback JSON mode** — when goldfive is not importable (e.g.
   running the reducer over a fixture in a stripped-down test
   environment), the reducer reads the JSONL lines as plain JSON dicts
   produced by ``MessageToJson(sort_keys=True)``. The shape of those
   dicts is the proto's wire form: ``{"payload_key": {...}, "runId":
   "...", "sequence": N}`` etc. We walk the same payload keys either
   way.

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
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    LossProfile,
    MetricCount,
    ScoringWeights,
)

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


def _normalize_drift_kind_str(raw: str) -> str | None:
    """Normalise a wire-form drift-kind string to the lowercase canonical form.

    Handles three shapes:
      * Bare lowercase already (``"off_topic"``) — returned as-is.
      * Uppercase enum name (``"DRIFT_KIND_OFF_TOPIC"``) — stripped + lowered.
      * Unknown / ``"DRIFT_KIND_UNSPECIFIED"`` / empty — returned as ``None``.
    """
    if not raw:
        return None
    if raw.startswith("DRIFT_KIND_"):
        suffix = raw[len("DRIFT_KIND_") :].lower()
        if suffix in ("unspecified", ""):
            return None
        return suffix
    # Treat as already-canonical lowercase. The aggregation step counts
    # whatever string we hand back; the kind set on zicato side is
    # validated separately.
    return raw.lower()


def _normalize_severity_str(raw: str) -> str | None:
    """Map a wire-form severity string to ``"info"`` / ``"warning"`` / ``"critical"``."""
    if not raw:
        return None
    if raw.startswith("DRIFT_SEVERITY_"):
        suffix = raw[len("DRIFT_SEVERITY_") :].lower()
        if suffix == "unspecified":
            return None
        if suffix in ("info", "warning", "critical"):
            return suffix
        return None
    lo = raw.lower()
    if lo in ("info", "warning", "critical"):
        return lo
    return None


def _load_events_as_dicts(events_jsonl_path: Path) -> list[dict[str, Any]]:
    """Read an events JSONL into a list of dicts.

    We prefer goldfive's :func:`replay_from_jsonl` for strict
    proto-message parsing (it gives us typed enum ints and tolerates
    unknown fields), but fall back to plain JSON line parsing when
    goldfive is not importable. Either way the rest of this module
    operates over a uniform ``list[dict]``.

    The conversion from proto message → dict uses ``MessageToDict`` so
    field naming matches the JSON-fallback path. We pass
    ``preserving_proto_field_name=True`` so snake_case payload keys
    (``drift_detected`` not ``driftDetected``) survive, and
    ``use_integers_for_enums=False`` so we get the uppercase enum names
    that ``_normalize_*`` already handle.
    """

    def _plain_json_fallback() -> list[dict[str, Any]]:
        """Read the JSONL with vanilla :mod:`json`, skipping malformed lines.

        Each readable line is treated as a ``MessageToJson`` dict already
        (the JSON form goldfive's persistence sink writes). Lines whose
        payload mixes the two serialisation conventions goldfive emits
        (camelCase ``"emittedAt": "ISO string"`` for some events,
        snake_case ``"emitted_at": {seconds, nanos}`` for others) still
        survive — the reducer's downstream consumers only inspect
        payload keys, not the envelope timestamps, so we tolerate either
        shape rather than aborting the whole replay on a strict-parse
        failure.
        """

        out: list[dict[str, Any]] = []
        with open(events_jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    try:
        from goldfive.sinks.persistence import replay_from_jsonl
        from google.protobuf.json_format import MessageToDict
    except ModuleNotFoundError:
        return _plain_json_fallback()

    try:
        events = replay_from_jsonl(events_jsonl_path)
    except Exception:  # noqa: BLE001 — goldfive's strict parser is brittle
        # Strict proto-parse failed (typically because goldfive's sink
        # emitted a mix of camelCase and snake_case event shapes within
        # one JSONL file). Fall back to plain JSON so the reducer still
        # produces a loss profile.
        return _plain_json_fallback()

    out: list[dict[str, Any]] = []
    for evt in events:
        # MessageToDict renamed ``including_default_value_fields`` to
        # ``always_print_fields_with_no_presence`` in newer protobuf
        # releases. We default the value to False either way, which is
        # the historical behaviour, so just leaving the kwarg out is
        # the version-portable choice.
        d = MessageToDict(
            evt,
            preserving_proto_field_name=True,
            use_integers_for_enums=False,
        )
        out.append(d)
    return out


def _payload(event: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return ``(payload_key, payload_dict)`` for one event dict.

    Goldfive's ``Event`` proto wraps its payload in a oneof. ``MessageToJson``
    serialises that as exactly one of the payload keys at the top level
    of the event dict. We scan the known payload keys, ignoring envelope
    keys (``event_id``, ``run_id``, ``sequence``, ``emitted_at``,
    ``session_id``).
    """
    envelope = {"event_id", "run_id", "sequence", "emitted_at", "session_id"}
    for k, v in event.items():
        if k in envelope:
            continue
        if isinstance(v, dict):
            return k, v
    return None, {}


# ---------------------------------------------------------------------------
# Drift loss
# ---------------------------------------------------------------------------


# Multiplier on ``task_failure_ratio`` inside :func:`compute_drift_loss`.
# Spec'd in the contract: "task_failure_ratio (multiplier 10.0 by
# default — pure failures matter)". Kept as a module-level constant so
# tests can introspect it and so the value lives somewhere greppable.
_TASK_FAILURE_RATIO_MULTIPLIER: float = 10.0


def compute_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    task_failure_ratio: float,
    runtime_ms: int,
    weights: ScoringWeights,
) -> float:
    """Compute the weighted-scalar drift-loss term.

    The formula is::

        loss = sum(
            severity_weights[c.severity] * per_kind_weights.get(c.kind, 1.0) * c.count
            for c in drift_counts
        )
        + weights.plan_revision_weight * plan_revisions
        + 10.0 * task_failure_ratio
        + weights.runtime_weight * (runtime_ms / 1000.0)

    All terms are non-negative on legal inputs, so the return value is
    non-negative; we clamp to zero defensively in case weights are
    set unusually.

    The 10.0 multiplier on ``task_failure_ratio`` is deliberately
    constant rather than configurable — the contract pinned it as
    "pure failures matter". If operators want to dampen failures
    relative to drift, they should up-weight drift via
    :attr:`ScoringWeights.severity_weights` or
    :attr:`ScoringWeights.per_kind_weights` instead.
    """
    sev_w = weights.severity_weights
    kind_w = weights.per_kind_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = kind_w.get(c.kind, 1.0)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * task_failure_ratio
    loss += weights.runtime_weight * (runtime_ms / 1000.0)
    return max(0.0, float(loss))


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
    transcripts and easy to reason about. We deliberately avoid the
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
    norm_a = sum(v * v for v in a.values()) ** 0.5
    norm_b = sum(v * v for v in b.values()) ** 0.5
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


def _agent_and_user_turns_from_events(
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Best-effort transcript reconstruction from goldfive events.

    Goldfive's event stream does not carry user-facing assistant /
    user messages as first-class payloads (those live in the harness
    transcript, not in the event wire). However, several payloads do
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
        key, payload = _payload(evt)
        if key is None:
            continue
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
) -> LossProfile:
    """Build a :class:`LossProfile` for one run.

    Reads ``events_jsonl_path`` (via goldfive's replay helper when
    available, plain JSON otherwise), counts the signals the contract
    pins, and computes the weighted drift-loss scalar via
    :func:`compute_drift_loss`. For multi-turn entries, additionally
    runs the memory-failure / context-loss heuristics over the
    best-effort transcript reconstruction.

    When ``wall_clock_budget_exceeded`` is true, the reducer adds a
    heavy fixed-magnitude term to the drift loss so a budget-exceeded
    run is unambiguously worst-case relative to a budget-respecting
    one. The magnitude is keyed off the configured ``severity_weights``
    so an epoch that has dialled severities down still sees a
    proportionally heavy penalty: we add
    ``5.0 * max(severity_weights.values(), default=1.0)``. This keeps
    the budget gate effective without burying the rest of the signal.

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
    """
    events: list[dict[str, Any]] = []
    if events_jsonl_path.exists():
        events = _load_events_as_dicts(events_jsonl_path)

    # --- 1. Walk events, aggregate raw counts ---

    drift_bucket: dict[tuple[str, str], int] = {}
    plan_revisions = 0
    task_started = 0
    task_failed = 0
    llm_call_count = 0
    token_count = 0
    agent_text_chars = 0
    run_id = ""
    for evt in events:
        if not run_id:
            run_id = str(evt.get("run_id", "") or evt.get("runId", "") or "")
        key, payload = _payload(evt)
        if key is None:
            continue
        if key == "drift_detected":
            kind, sev = _extract_drift_buckets(payload)
            if kind is None or sev is None:
                continue
            drift_bucket[(kind, sev)] = drift_bucket.get((kind, sev), 0) + 1
        elif key == "plan_revised":
            plan_revisions += 1
        elif key == "task_started":
            task_started += 1
        elif key == "task_failed":
            task_failed += 1
        elif key == "goldfive_llm_call_end":
            llm_call_count += 1
            # Token counts are not on the canonical goldfive proto but
            # MAY be attached as extension fields by callers that wrap
            # their LLM SDK with token-accounting middleware. Read
            # opportunistically; missing keys yield 0 and are tolerated.
            for tk in ("input_tokens", "output_tokens", "tokens", "total_tokens"):
                v = payload.get(tk)
                if isinstance(v, (int, float)):
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

    # Schema-failure scalar derived from the drift bucket. Folds the
    # `schema_violation` kind into a first-class metric so analysis
    # sites that care about schema health don't have to re-walk drift.
    schema_failures = sum(cnt for (k, _s), cnt in drift_bucket.items() if k == "schema_violation")

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

    # --- 2. Multi-turn heuristics ---

    turns_completed: int | None = None
    memory_failure_count: int | None = None
    context_loss_count: int | None = None
    if entry.kind != "single_turn":
        agent_turns, user_turns = _agent_and_user_turns_from_events(events)
        # ``turns_completed`` is a positional count of agent-side turns
        # observed on the wire; for the scripted / emulated kinds this
        # is what operators want surfaced in the journal.
        turns_completed = len(agent_turns)
        memory_failure_count = _memory_failure_count(agent_turns)
        context_loss_count = _context_loss_count(agent_turns, user_turns)

    # --- 3. Compute drift loss ---

    drift_loss = compute_drift_loss(
        drift_counts=drift_counts,
        plan_revisions=plan_revisions,
        task_failure_ratio=task_failure_ratio,
        runtime_ms=runtime_ms,
        weights=weights,
    )
    if wall_clock_budget_exceeded:
        # Heavy fixed-magnitude term keyed off severity_weights so the
        # penalty stays meaningfully large relative to the rest of the
        # scoring surface. See docstring for rationale.
        sev_vals = list(weights.severity_weights.values()) or [1.0]
        drift_loss += 5.0 * max(sev_vals)

    # --- 4. Pass/fail derivation ---

    pass_fail: bool | None = None
    if expectation_result is not None:
        pass_fail = expectation_result.passed

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

    Inverse of :func:`write_loss_profile`. Re-tuples ``drift_counts``
    (which JSON renders as a list) and re-constructs the nested
    :class:`DriftCount` and :class:`ExpectationResult` dataclasses.

    Back-compat: profiles written before the generalised metric surface
    omit ``metric_counts`` / ``tokens_spent`` / ``output_chars`` /
    ``schema_failures``. The reader treats them as the dataclass
    defaults (empty tuple / 0) so old JSON loads cleanly. New consumers
    that want the merged view should call
    :meth:`LossProfile.unified_metrics`.
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
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
            severity=m.get("severity", ""),  # type: ignore[arg-type]
            count=float(m.get("count", 0.0)),
        )
        for m in d.get("metric_counts", ())
        if isinstance(m, dict) and m.get("name")
    )
    exp = d.get("expectation_result")
    expectation_result: ExpectationResult | None
    if exp is None:
        expectation_result = None
    else:
        expectation_result = ExpectationResult(
            kind=exp["kind"],
            passed=bool(exp["passed"]),
            detail=exp.get("detail", ""),
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
    )


__all__ = [
    "reduce_loss",
    "compute_drift_loss",
    "read_loss_profile",
    "write_loss_profile",
]
