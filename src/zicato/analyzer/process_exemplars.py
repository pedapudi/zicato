"""Process exemplars: drift-anchored, redacted event windows for the proposer.

The design + normative redaction contract live in
``docs/design/PROCESS-EXEMPLARS.md``; this module is its mechanical
enforcement. The proposer's existing channels tell it *that* a failure
shape recurs (the detector patterns), *what* the wrong answers look like in
aggregate (the outcome-marginal profile), and *what was already tried* (the
experiment memory) — none shows *how* a failure unfolds: the plan step that
wandered, the tool call that looped. That sequence lives in each run's
goldfive ``events.jsonl``. A **process exemplar** is a small window of
events around one anchor event chosen for one detected pattern, passed
through a mechanical redaction layer (no LLM redactor, ever) so the
proposer may learn HOW failures unfold but never WHICH board entries fail.

The invariants, mirroring :mod:`zicato.analyzer.outcome_marginals`:

* **Train slice only.** The caller (the orchestrator) passes the SAME
  train-entry-id partition it uses for the patterns / loss summary /
  outcome marginals. This module reads only the ``events.jsonl`` files of
  those entries under the given champion generation; it never reads the
  board, so it cannot widen the slice it is given.
* **Anchored on released information.** An exemplar exists only for a
  pattern the detectors already surfaced; the anchor adds *mechanism* to a
  failure shape the proposer was already told about (PROCESS-EXEMPLARS.md
  §4).
* **Deterministic + byte-stable.** No RNG, no wall clock: extraction is a
  pure function of (pattern set, the champion's train-slice event files),
  so the rendered block is byte-identical round over round while those are
  unchanged — re-presenting it leaks nothing new. The leakage budget is
  ≤ ``cap`` windows per (champion, pattern-set) state.
* **Default-deny redaction.** Rules R1–R4 of PROCESS-EXEMPLARS.md §3, each
  implemented by a named function below with its own test:
  R1 :data:`_FIELD_POLICY` (payload allowlist; unknown cases render as a
  bare case marker), R2 :class:`_WindowAnonymizer` (window-local task-id
  tokens; entry ids never emitted; relative offsets, never absolute
  sequence numbers), R3 :func:`~zicato.analyzer.redaction.truncate_free_text`
  (head/tail elision at a fixed cap), R4
  :func:`~zicato.analyzer.redaction.scrub_identity` over
  :func:`_identity_corpus` (every DROPPED string value + every identity
  token is scrubbed out of every KEPT free-text value — a drift detail
  that quotes the task prompt loses the quote mechanically).

R3 and R4 are pure string transforms with a SECOND consumer (the
proposer's redacted query surface, :mod:`zicato.proposer.redacted_query`),
so they live in :mod:`zicato.analyzer.redaction` — one implementation, one
set of constants, byte-identical redaction on both paths. This module
keeps R1 and R2, which are bound to the exemplar window's own structure.

Like the decision-telemetry aggregator, this module is JSONL-only and
proto-stub-free: it reads the files goldfive's persistence sink wrote
(camelCase wire form) or the reducer's snake_case re-emission, normalizing
keys on read. Missing files, malformed lines, and unknown payloads are all
tolerated — extraction is best-effort by contract and must never abort a
round.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.analyzer.redaction import (
    FREE_TEXT_LIMIT_CHARS,
    MIN_SCRUB_LEN,
    iter_string_leaves,
    scrub_identity,
    truncate_free_text,
)
from zicato.core import normalize_wire_drift_kind, normalize_wire_severity

# ---------------------------------------------------------------------------
# Tunables (PROCESS-EXEMPLARS.md §2–§3 — the doc is normative; change both)
# ---------------------------------------------------------------------------

#: Events kept on EACH side of the anchor (§2): a plan step, an agent/tool
#: invocation, the drift finding, and the steering response typically fit
#: inside ±3.
_WINDOW_RADIUS = 3

#: Default (and conventional) exemplar cap per extraction — matches the
#: outcome-marginal channel's entry cap. The contract knob
#: (``ProposerQualityConfig.process_exemplars``) supplies the live value.
DEFAULT_EXEMPLAR_CAP = 2

#: The R3 / R4 tunables live with their implementations in
#: :mod:`zicato.analyzer.redaction` (``FREE_TEXT_*`` / ``ELISION`` /
#: ``MIN_SCRUB_LEN`` / ``WITHHELD``) so both consumers share one set of
#: values. PROCESS-EXEMPLARS.md §2–§3 remains normative for all of them.
#: The two historical private spellings below are kept as aliases so the
#: rule-by-rule R3 tests (and any caller that reached for them) keep
#: resolving from this module after the move.
_FREE_TEXT_LIMIT_CHARS = FREE_TEXT_LIMIT_CHARS
_truncate_free_text = truncate_free_text


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExemplarEvent:
    """One redacted event of an exemplar window.

    ``offset`` is the position relative to the anchor (``0``), never the
    absolute sequence number (R2 — absolute positions could fingerprint an
    entry). ``case`` is the goldfive payload oneof case name (a closed
    vocabulary). ``fields`` are the already-redacted ``(name, value)``
    pairs the policy admitted, in policy order — everything else was
    dropped before this object was built.
    """

    offset: int
    case: str
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessExemplar:
    """One drift-anchored, fully-redacted event window.

    ``pattern_id`` / ``pattern_kind`` tie the exemplar back to the detector
    pattern it illustrates (both already rendered in the pattern block).
    ``anchor_label`` is a short human-readable description of what the
    anchor is (e.g. ``"drift kind 'looping_tool_call'"``) — built from
    closed-vocabulary / harness-side identity only, never board identity.
    """

    pattern_id: str
    pattern_kind: str
    anchor_label: str
    events: tuple[ExemplarEvent, ...] = ()


# ---------------------------------------------------------------------------
# Event reading (JSONL-only, proto-stub-free — the aggregator's discipline)
# ---------------------------------------------------------------------------

_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"event_id", "run_id", "sequence", "emitted_at", "session_id"}
)


def _to_snake(name: str) -> str:
    """camelCase → snake_case; an already-snake_case string is unchanged."""
    return re.sub(r"(?<!^)(?<!_)([A-Z])", r"_\1", name).lower()


def _snake_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow copy of ``d`` with top-level keys snake-cased."""
    return {_to_snake(k): v for k, v in d.items()}


def _load_events(path: Path) -> list[dict[str, Any]]:
    """Read one ``events.jsonl`` into envelope-normalized event dicts.

    Plain-JSON only (goldfive's persistence sink wrote JSON lines via
    ``MessageToJson``); malformed lines and non-dict values are skipped, a
    missing/unreadable file yields ``[]`` — extraction is best-effort.
    """
    out: list[dict[str, Any]] = []
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
                    out.append(_snake_keys(obj))
    except OSError:
        return []
    return out


def _payload(event: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the ``(case, payload_dict)`` of one event dict (or ``(None, {})``).

    The payload is the single non-envelope dict-valued top-level key —
    goldfive's ``Event.payload`` oneof in wire form. Payload field keys are
    snake-cased so both wire shapes (camelCase sink output, snake_case
    proto re-emission) dispatch identically.
    """
    for k, v in event.items():
        if k in _ENVELOPE_KEYS:
            continue
        if isinstance(v, dict):
            return k, _snake_keys(v)
    return None, {}


# ---------------------------------------------------------------------------
# R1 — the payload/field allowlist (default-deny)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CasePolicy:
    """Field dispositions for one payload case (PROCESS-EXEMPLARS.md §3 R1).

    ``keep`` fields render verbatim (closed vocabulary / structural /
    harness-side identity); ``truncate`` fields are free process text —
    scrubbed (R4) then truncated (R3); ``anonymize`` fields are per-window
    identity tokens (R2); ``plan_structure`` admits the ``plan`` sub-message
    as counts only (``N tasks, M edges`` — no titles, no descriptions).
    Every field NOT named here is dropped, and its string content joins the
    identity corpus (R4).
    """

    keep: tuple[str, ...] = ()
    truncate: tuple[str, ...] = ()
    anonymize: tuple[str, ...] = ()
    plan_structure: bool = False


#: The normative field policy — a 1:1 transcription of the table in
#: PROCESS-EXEMPLARS.md §3. Payload cases not listed here render as a bare
#: case marker (offset + case name, no fields): the window's SHAPE survives,
#: its content does not. That default-deny covers:
#: ``run_started`` (``goal_summary`` IS the task prompt), ``run_completed``
#: / ``task_completed`` / ``task_progress`` (summaries/details ARE model
#: output or task content), and every LLM-call bookend.
_FIELD_POLICY: Mapping[str, _CasePolicy] = {
    "drift_detected": _CasePolicy(
        keep=("kind", "severity", "lifecycle", "authored_by", "current_agent_id"),
        truncate=("detail",),
        anonymize=("current_task_id",),
    ),
    "plan_revised": _CasePolicy(
        keep=("drift_kind", "severity", "revision_index", "target_agent_id", "dry_run"),
        truncate=("reason",),
        plan_structure=True,
    ),
    "plan_submitted": _CasePolicy(plan_structure=True),
    "task_started": _CasePolicy(anonymize=("task_id",)),
    "task_failed": _CasePolicy(
        keep=("recoverable",),
        truncate=("reason",),
        anonymize=("task_id",),
    ),
    "task_blocked": _CasePolicy(
        truncate=("blocker", "needed"),
        anonymize=("task_id",),
    ),
    "task_cancelled": _CasePolicy(
        truncate=("reason",),
        anonymize=("task_id",),
    ),
    "agent_invocation_started": _CasePolicy(
        keep=("agent_name",),
        anonymize=("task_id",),
    ),
    "agent_invocation_completed": _CasePolicy(
        keep=("agent_name",),
        anonymize=("task_id",),
    ),
    "steering_decision_made": _CasePolicy(
        keep=(
            "detector_name",
            "outcome",
            "considered_severity",
            "chosen_severity",
            "considered_intervention_level",
            "chosen_intervention_level",
            "agent_name",
        ),
        truncate=("reason",),
        anonymize=("task_id",),
    ),
    "reasoning_judge_invoked": _CasePolicy(
        keep=("on_task", "severity", "classification", "subject_agent_id"),
        truncate=("reason",),
        anonymize=("task_id",),
    ),
    "judgement_emitted": _CasePolicy(
        keep=("judge_name", "verdict_kind", "drift_kind", "severity", "metric_name"),
    ),
}

#: Fields whose values are drift-kind / severity ENUM wire forms
#: (``DRIFT_KIND_OFF_TOPIC`` / ``DRIFT_SEVERITY_WARNING``) — normalized to
#: the canonical lowercase kind strings at render so the exemplar block
#: speaks the same vocabulary as the pattern block.
_DRIFT_KIND_FIELDS: frozenset[str] = frozenset({"kind", "drift_kind"})
_SEVERITY_FIELDS: frozenset[str] = frozenset({"severity", "prev_severity"})


def _plan_structure(payload: Mapping[str, Any]) -> str | None:
    """Render a ``plan`` sub-message as counts only: ``"N tasks, M edges"``.

    Structural counts carry the process signal ("the plan grew to 7 tasks")
    without a byte of task titles / descriptions / assignee content — all
    of which are dropped (and their strings scrubbed, via the corpus).
    Returns ``None`` when the payload carries no readable plan.
    """
    plan = payload.get("plan")
    if not isinstance(plan, Mapping):
        return None
    tasks = plan.get("tasks")
    edges = plan.get("edges")
    n_tasks = len(tasks) if isinstance(tasks, list) else 0
    n_edges = len(edges) if isinstance(edges, list) else 0
    return f"{n_tasks} tasks, {n_edges} edges"


# ---------------------------------------------------------------------------
# R2 — window-local identity anonymization
# ---------------------------------------------------------------------------


@dataclass
class _WindowAnonymizer:
    """Maps raw task/invocation ids to window-local ``task-N`` tokens.

    Window-local by design (PROCESS-EXEMPLARS.md §3 R2): the same raw id
    maps to the same token *within one window* (so "the same task keeps
    failing" stays visible) but the mapping is rebuilt per window, so
    nothing correlates across windows, rounds, or back to the board.
    """

    _tokens: dict[str, str] = field(default_factory=dict)

    def token(self, raw: str) -> str:
        got = self._tokens.get(raw)
        if got is None:
            got = f"task-{len(self._tokens) + 1}"
            self._tokens[raw] = got
        return got


# ---------------------------------------------------------------------------
# R4 — the identity corpus (the scrub itself lives in analyzer.redaction)
# ---------------------------------------------------------------------------


#: Payload fields that are identity TOKENS (scrubbed at any length, on word
#: boundaries) rather than free text.
_TOKEN_FIELDS: frozenset[str] = frozenset(
    {"task_id", "current_task_id", "invocation_id", "parent_invocation_id", "drift_id"}
)


def _identity_corpus(
    events: Sequence[dict[str, Any]], entry_id: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Build the R4 identity corpus for one run: ``(texts, tokens)``.

    ``texts`` are the string values of every DROPPED field across the WHOLE
    file (not just the window — the task prompt lives in
    ``run_started.goal_summary`` wherever that event sits), scrubbed by
    substring when at least :data:`~zicato.analyzer.redaction.MIN_SCRUB_LEN`
    chars long. ``tokens`` are identity ids — the entry id, envelope
    run/session/event ids, and every raw
    task / invocation id — scrubbed at any length on word boundaries.
    Kept / truncated field values are NOT in the corpus (they are the text
    being protected rather than the identity being removed).
    """
    texts: set[str] = set()
    tokens: set[str] = {entry_id} if entry_id else set()

    for event in events:
        for env_key in ("run_id", "session_id", "event_id"):
            raw = event.get(env_key)
            if isinstance(raw, str) and raw:
                tokens.add(raw)
        case, payload = _payload(event)
        if case is None:
            continue
        policy = _FIELD_POLICY.get(case, _CasePolicy())
        admitted = set(policy.keep) | set(policy.truncate) | set(policy.anonymize)
        for fname, value in payload.items():
            if isinstance(value, str) and value and fname in _TOKEN_FIELDS:
                tokens.add(value)
            if fname in admitted:
                continue
            for leaf in iter_string_leaves(value):
                if len(leaf) >= MIN_SCRUB_LEN:
                    texts.add(leaf)
    return frozenset(texts), frozenset(tokens)


# ---------------------------------------------------------------------------
# Redaction of one window
# ---------------------------------------------------------------------------


def _redact_event(
    event: dict[str, Any],
    offset: int,
    anonymizer: _WindowAnonymizer,
    corpus_texts: frozenset[str],
    tokens: frozenset[str],
) -> ExemplarEvent:
    """Apply R1–R4 to one raw event dict → one :class:`ExemplarEvent`.

    Field order is the policy's declaration order (keep, then plan
    structure, then truncate, then anonymize) so the output is
    deterministic and diff-stable.
    """
    case, payload = _payload(event)
    if case is None:
        return ExemplarEvent(offset=offset, case="(unknown)")
    policy = _FIELD_POLICY.get(case)
    if policy is None:
        # R1 default-deny: unlisted cases render as a bare case marker.
        return ExemplarEvent(offset=offset, case=case)

    fields: list[tuple[str, str]] = []
    for fname in policy.keep:
        raw = payload.get(fname)
        if raw is None or raw == "":
            continue
        if fname in _DRIFT_KIND_FIELDS:
            value = normalize_wire_drift_kind(str(raw)) or str(raw)
        elif fname in _SEVERITY_FIELDS:
            value = normalize_wire_severity(str(raw)) or str(raw)
        elif isinstance(raw, bool):
            value = "true" if raw else "false"
        else:
            value = str(raw)
        fields.append((fname, value))
    if policy.plan_structure:
        structure = _plan_structure(payload)
        if structure is not None:
            fields.append(("plan", structure))
    for fname in policy.truncate:
        raw = payload.get(fname)
        if not isinstance(raw, str) or not raw:
            continue
        scrubbed = scrub_identity(raw, corpus_texts, tokens)
        fields.append((fname, truncate_free_text(scrubbed)))
    for fname in policy.anonymize:
        raw = payload.get(fname)
        if not isinstance(raw, str) or not raw:
            continue
        fields.append((fname, anonymizer.token(raw)))
    return ExemplarEvent(offset=offset, case=case, fields=tuple(fields))


def _redact_window(
    events: Sequence[dict[str, Any]],
    anchor_index: int,
    entry_id: str,
) -> tuple[ExemplarEvent, ...]:
    """Extract + redact the ±:data:`_WINDOW_RADIUS` window around an anchor."""
    corpus_texts, tokens = _identity_corpus(events, entry_id)
    anonymizer = _WindowAnonymizer()
    lo = max(0, anchor_index - _WINDOW_RADIUS)
    hi = min(len(events), anchor_index + _WINDOW_RADIUS + 1)
    return tuple(
        _redact_event(events[i], i - anchor_index, anonymizer, corpus_texts, tokens)
        for i in range(lo, hi)
    )


# ---------------------------------------------------------------------------
# Anchor resolution (PROCESS-EXEMPLARS.md §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AnchorSpec:
    """How to find one pattern's anchor event.

    ``label`` is the exemplar's human-readable anchor description (built
    from closed-vocabulary / harness identity only). ``entry_ids`` narrows
    the scan to specific entries when the pattern names them (always
    intersected with the train slice by the caller); ``None`` scans every
    train entry in sorted order.
    """

    label: str
    matches: Any  # Callable[[str, Mapping[str, Any]], bool]
    entry_ids: tuple[str, ...] | None = None


def _split_ids(joined: str) -> tuple[str, ...]:
    """Split a detector's comma-joined id list, dropping empties."""
    return tuple(piece.strip() for piece in joined.split(",") if piece.strip())


def _anchor_spec(pattern: Any) -> _AnchorSpec | None:
    """Resolve the anchor matcher for one detector pattern, or ``None``.

    The table from PROCESS-EXEMPLARS.md §2. Patterns without a localized
    event footprint (cost / rubric frequencies, the multi-turn transcript
    heuristics) return ``None`` and are skipped.
    """
    kind = str(getattr(pattern, "kind", ""))
    detail: Mapping[str, str] = getattr(pattern, "detail", {}) or {}

    if kind == "drift_kind_frequency" or (
        kind.endswith("metric_frequency")
        and str(detail.get("metric_name", "")).startswith("drift:")
    ):
        drift_kind = (
            str(detail.get("drift_kind", "")) or str(detail.get("metric_name", ""))[len("drift:") :]
        )
        if not drift_kind:
            return None
        affected = _split_ids(str(detail.get("affected_entry_ids", "")))

        def _match_kind(case: str, payload: Mapping[str, Any]) -> bool:
            if case != "drift_detected":
                return False
            return normalize_wire_drift_kind(str(payload.get("kind", ""))) == drift_kind

        return _AnchorSpec(
            label=f"drift kind {drift_kind!r}",
            matches=_match_kind,
            entry_ids=affected or None,
        )

    if kind == "hot_agent":
        agent = str(detail.get("agent_name", ""))
        entry = str(detail.get("entry_id", ""))
        if not agent:
            return None

        def _match_agent(case: str, payload: Mapping[str, Any]) -> bool:
            return case == "drift_detected" and str(payload.get("current_agent_id", "")) == agent

        return _AnchorSpec(
            label=f"drift by agent {agent!r}",
            matches=_match_agent,
            entry_ids=(entry,) if entry else None,
        )

    if kind == "hot_task":
        task = str(detail.get("task_id", ""))
        entry = str(detail.get("entry_id", ""))
        if not task:
            return None

        def _match_task(case: str, payload: Mapping[str, Any]) -> bool:
            return (
                case in ("task_failed", "task_blocked") and str(payload.get("task_id", "")) == task
            )

        # The raw task id is used to FIND the anchor only; the rendered
        # window anonymizes it (R2) like every other task id.
        return _AnchorSpec(
            label="a repeatedly failing task",
            matches=_match_task,
            entry_ids=(entry,) if entry else None,
        )

    if kind == "plan_revision_instability":
        affected = _split_ids(str(detail.get("affected_entry_ids", "")))

        def _match_revision(case: str, payload: Mapping[str, Any]) -> bool:
            return case == "plan_revised"

        return _AnchorSpec(
            label="a plan revision",
            matches=_match_revision,
            entry_ids=affected or None,
        )

    return None


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------


def extract_process_exemplars(
    workspace_root: Path,
    epoch_id: str,
    patterns: Sequence[Any],
    cap: int = DEFAULT_EXEMPLAR_CAP,
    *,
    parent_generation_id: str,
    train_entry_ids: Collection[str],
) -> tuple[ProcessExemplar, ...]:
    """Extract ≤ ``cap`` redacted exemplar windows for the detected patterns.

    Parameters
    ----------
    workspace_root / epoch_id / parent_generation_id:
        Locate the CURRENT CHAMPION's per-entry ``events.jsonl`` files (the
        same generation whose losses fed the detectors).
    patterns:
        The round's detected patterns, in detector order. At most one
        exemplar is minted per pattern (PROCESS-EXEMPLARS.md §2); patterns
        without a localized event footprint contribute nothing.
    cap:
        Hard ceiling on exemplars returned (default
        :data:`DEFAULT_EXEMPLAR_CAP`). ``<= 0`` returns ``()``.
    train_entry_ids:
        The TRAIN-slice entry ids — the same ``split_board`` /
        ``rotation_seed`` partition the caller used for the patterns and
        outcome marginals. Only these entries' event files are ever read;
        a pattern that names entries outside the slice is narrowed to the
        intersection (and skipped when that is empty), so the holdout's
        events can never be windowed.

    Fully deterministic (sorted entry order, first match wins, no RNG, no
    wall clock): the same inputs yield byte-identical exemplars, so the
    rendered block is stable across rounds until the pattern set or the
    champion changes — the §2 refresh semantics.
    """
    if cap <= 0 or not patterns:
        return ()
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415
    from zicato.tournament.unit_cache import any_unit_transcript  # noqa: PLC0415

    train_ids = sorted(str(e) for e in train_entry_ids)
    if not train_ids:
        return ()
    train_id_set = set(train_ids)

    events_cache: dict[str, list[dict[str, Any]]] = {}

    def _events_for(entry_id: str) -> list[dict[str, Any]]:
        cached = events_cache.get(entry_id)
        if cached is None:
            cached = _load_events(
                any_unit_transcript(
                    events_jsonl_path(workspace_root, epoch_id, parent_generation_id, entry_id)
                )
            )
            events_cache[entry_id] = cached
        return cached

    out: list[ProcessExemplar] = []
    seen_anchors: set[tuple[str, int]] = set()
    for pattern in patterns:
        if len(out) >= cap:
            break
        spec = _anchor_spec(pattern)
        if spec is None:
            continue
        if spec.entry_ids is not None:
            scan_ids = sorted(e for e in spec.entry_ids if e in train_id_set)
        else:
            scan_ids = train_ids
        window: tuple[ExemplarEvent, ...] | None = None
        for entry_id in scan_ids:
            events = _events_for(entry_id)
            for i, event in enumerate(events):
                case, payload = _payload(event)
                if case is None or not spec.matches(case, payload):
                    continue
                if (entry_id, i) in seen_anchors:
                    continue
                seen_anchors.add((entry_id, i))
                window = _redact_window(events, i, entry_id)
                break
            if window is not None:
                break
        if window is None:
            continue
        out.append(
            ProcessExemplar(
                pattern_id=str(getattr(pattern, "id", "")),
                pattern_kind=str(getattr(pattern, "kind", "")),
                anchor_label=spec.label,
                events=window,
            )
        )
    return tuple(out)


__all__ = [
    "DEFAULT_EXEMPLAR_CAP",
    "ExemplarEvent",
    "ProcessExemplar",
    "extract_process_exemplars",
]
