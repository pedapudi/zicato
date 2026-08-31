"""Redacted, train-slice-only run-corpus queries for a tool-using proposer.

The proposer's other channels are *pre-rendered*: the orchestrator samples
and bands the patterns, the failure-mode profile, and the process
exemplars before the proposer ever sees them. This module makes a small,
provably-clean part of the same corpus **queryable on demand** — same
privacy envelope, asked rather than pushed.

The privacy envelope is the deliverable
---------------------------------------
A view that leaks board identity into the proposer destroys the
overfitting guarantee the tournament exists to provide, and it does so
*silently*: nothing errors, nothing warns, and the champion quietly
memorizes the board. So this surface is built default-deny, and the
design invariant is PROPOSER.md §2.5 — **feed the MARGINAL, never the
JOINT**. The proposer may learn an aggregate property of the *harness's*
behaviour; it must never be able to reconstruct any board entry.

Concretely:

* **Train slice only, derived not trusted.** No caller passes a slice in.
  Every tool re-derives the partition itself from ``workspace_root`` +
  ``epoch_id`` through the SAME canonical
  :func:`~zicato.board.split.split_board` /
  :func:`~zicato.board.split.rotation_seed` pair the orchestrator uses, so
  a caller cannot widen it. **Fail closed**: if the slice cannot be
  derived — no board, no ``scoring.json``, an unparseable either — every
  tool returns :data:`_UNAVAILABLE` and NO data. It never falls back to
  the whole board.
* **Two independent gates.** Gate 1: only train-slice entries' event files
  are ever opened. Gate 2: :func:`drop_out_of_slice` re-filters the
  collected results by entry id afterwards, so a future view that arrives
  pre-filtered by someone else still cannot smuggle a holdout row through.
* **No entry ids, no task text, no model output — ever.** Entry ids are
  used to LOCATE files and are never emitted; the aggregates are counts
  over the slice rather than rows. Only a narrow allowlist of closed-vocabulary
  event fields is read at all (:data:`_READ_POLICY`); every other field is
  dropped and its strings join the identity corpus. The handful of
  open-vocabulary labels that do survive (agent names) are passed through
  :func:`~zicato.analyzer.redaction.scrub_identity` and then
  :func:`~zicato.analyzer.redaction.truncate_free_text` — the same
  identity-scrub and free-text-truncation primitives the process-exemplar
  channel uses, in that order.
* **Banded rather than exact.** Every number is a rate coarsened through the
  existing :func:`~zicato.proposer.prompts.band_rate` vocabulary
  (``none`` / ``~20%`` / ``~all``), and results are ordered by BAND then
  name — so neither the value nor the ordering hands back a fine-grained,
  climbable response surface (OVERFITTING.md §11.4).
* **Per-entry incidence rather than per-event counts.** Each entry contributes at
  most once to each rate ("in what fraction of train entries did X happen
  at least once"), so one chatty run cannot dominate a figure and no
  per-entry magnitude is recoverable.
* **Stable within a reign.** The champion's event files do not change
  between rounds, so re-asking returns byte-identical answers until the
  champion changes — there is no round-over-round signal to hill-climb,
  the same argument PROCESS-EXEMPLARS.md §2 makes for its refresh
  semantics.

Every tool here is best-effort by contract: a missing file, a malformed
line, an unknown payload — all tolerated, never raised. A proposer round
must never abort because a diagnostic read failed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.analyzer.redaction import (
    MIN_SCRUB_LEN,
    iter_string_leaves,
    scrub_identity,
    truncate_free_text,
)
from zicato.core import normalize_wire_drift_kind, normalize_wire_severity
from zicato.proposer.tool_context import ProposerToolContext, _active_context

#: The single fail-closed status string. A tool that cannot derive the
#: train slice returns this and NO data — never the whole board.
_UNAVAILABLE = "train slice unavailable"

#: Cap on the number of distinct open-vocabulary labels (agent names) any
#: one tool emits — a runaway-context guard mirroring the other proposer
#: tools' limits. Truncation is annotated in the payload.
_LABEL_LIMIT = 40

#: Goldfive event-envelope keys, so the payload oneof can be picked out as
#: "the single non-envelope dict-valued key" (the JSONL-only, proto-stub-
#: free discipline :mod:`zicato.analyzer.process_exemplars` follows).
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"event_id", "run_id", "sequence", "emitted_at", "session_id"}
)

#: Payload fields that are identity TOKENS — collected into the identity-token
#: corpus and scrubbed out of any emitted label at any length.
_TOKEN_FIELDS: frozenset[str] = frozenset(
    {
        "task_id",
        "current_task_id",
        "invocation_id",
        "parent_invocation_id",
        "drift_id",
        "plan_id",
        "run_id",
        "session_id",
    }
)

#: The READ allowlist — NARROWER by design than the process-exemplar
#: channel's payload allowlist, because this surface is queryable on demand rather
#: than capped at a couple of windows per round. Every field named here is
#: either a closed vocabulary (drift kind, severity, steering outcome,
#: intervention level, judge classification) or a harness-side agent label;
#: NO free-text field is admitted at all. Everything else — every payload
#: case not listed, and every field of a listed case not named — is dropped
#: and its string leaves join the identity corpus. That default-deny is
#: what keeps ``run_started.goal_summary`` (the task prompt) and every
#: completion summary (model output) structurally unreachable.
_READ_POLICY: Mapping[str, tuple[str, ...]] = {
    "drift_detected": ("kind", "severity", "current_agent_id"),
    "plan_revised": ("drift_kind", "severity", "dry_run"),
    "task_failed": ("recoverable",),
    "task_blocked": (),
    "task_cancelled": (),
    "agent_invocation_started": ("agent_name",),
    "agent_invocation_completed": ("agent_name",),
    "steering_decision_made": (
        "outcome",
        "chosen_severity",
        "chosen_intervention_level",
        "agent_name",
    ),
    "reasoning_judge_invoked": ("on_task", "severity", "classification"),
}

#: Fields whose values are drift-kind / severity ENUM wire forms, normalized
#: to the canonical lowercase strings so this surface speaks the same
#: vocabulary as the pattern and exemplar blocks.
_DRIFT_KIND_FIELDS: frozenset[str] = frozenset({"kind", "drift_kind"})
_SEVERITY_FIELDS: frozenset[str] = frozenset({"severity", "chosen_severity"})

#: The process-failure payload cases whose per-entry incidence
#: :func:`train_slice_process_profile` reports. Case NAMES are goldfive's
#: closed payload-oneof vocabulary and carry no content of their own.
_PROCESS_CASES: tuple[str, ...] = (
    "task_failed",
    "task_blocked",
    "task_cancelled",
    "plan_revised",
)


# ---------------------------------------------------------------------------
# Event reading (JSONL-only, best-effort — the aggregator's discipline)
# ---------------------------------------------------------------------------


def _to_snake(name: str) -> str:
    """camelCase → snake_case; an already-snake_case string is unchanged."""
    return re.sub(r"(?<!^)(?<!_)([A-Z])", r"_\1", name).lower()


def _snake_keys(d: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow copy of ``d`` with top-level keys snake-cased."""
    return {_to_snake(k): v for k, v in d.items()}


def _read_events(path: Path) -> list[dict[str, Any]]:
    """Read one ``events.jsonl`` into envelope-normalized event dicts.

    Malformed lines and non-dict values are skipped; a missing or
    unreadable file yields ``[]``. Reading is best-effort by contract —
    a diagnostic query must never abort a proposer round.
    """
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(_snake_keys(obj))
    except OSError:
        return []
    return out


def _case_and_payload(event: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the ``(case, payload)`` of one event dict (or ``(None, {})``).

    The payload is the single non-envelope dict-valued top-level key —
    goldfive's ``Event.payload`` oneof in wire form. Keys are snake-cased
    so both wire shapes dispatch identically.
    """
    for k, v in event.items():
        if k in _ENVELOPE_KEYS:
            continue
        if isinstance(v, dict):
            return k, _snake_keys(v)
    return None, {}


# ---------------------------------------------------------------------------
# The per-run identity corpus, against THIS module's allowlist
# ---------------------------------------------------------------------------


def _identity_corpus(
    events: Sequence[Mapping[str, Any]], entry_id: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Build the ``(texts, tokens)`` identity corpus for one run's events.

    Mirrors :func:`zicato.analyzer.process_exemplars._identity_corpus` but
    is computed against :data:`_READ_POLICY` — this module's (narrower)
    allowlist — so anything this module drops is scrubbed out of anything
    this module emits. ``texts`` are dropped string values at least
    :data:`~zicato.analyzer.redaction.MIN_SCRUB_LEN` chars long; ``tokens``
    are the entry id, the envelope run / session / event ids, and every raw
    task / invocation / plan id, scrubbed at any length on word boundaries.
    """
    texts: set[str] = set()
    tokens: set[str] = {entry_id} if entry_id else set()
    for event in events:
        for env_key in ("run_id", "session_id", "event_id"):
            raw = event.get(env_key)
            if isinstance(raw, str) and raw:
                tokens.add(raw)
        case, payload = _case_and_payload(event)
        if case is None:
            continue
        admitted = set(_READ_POLICY.get(case, ()))
        for fname, value in payload.items():
            if isinstance(value, str) and value and fname in _TOKEN_FIELDS:
                tokens.add(value)
            if fname in admitted:
                continue
            for leaf in iter_string_leaves(value):
                if len(leaf) >= MIN_SCRUB_LEN:
                    texts.add(leaf)
    return frozenset(texts), frozenset(tokens)


def _safe_label(raw: Any, texts: frozenset[str], tokens: frozenset[str]) -> str:
    """Redact one emitted label: scrub identity, THEN truncate.

    The order is load-bearing — see
    :func:`~zicato.analyzer.redaction.truncate_free_text`. Applied to EVERY
    string this module emits, closed-vocabulary or not: a closed vocabulary
    is an assumption about the producer, and default-deny does not rest on
    assumptions about producers.
    """
    return truncate_free_text(scrub_identity(str(raw), texts, tokens))


# ---------------------------------------------------------------------------
# The train slice — derived from the workspace, never accepted from a caller
# ---------------------------------------------------------------------------


def _derive_train_slice(ctx: ProposerToolContext) -> tuple[frozenset[str], str]:
    """Re-derive the round's TRAIN slice; ``(ids, "")`` or ``(∅, reason)``.

    Loads the epoch's frozen ``board.jsonl`` and ``scoring.json`` and runs
    the SAME canonical partition the orchestrator runs
    (:func:`~zicato.board.split.rotation_seed` threaded into
    :func:`~zicato.board.split.split_board`), so this surface cannot see a
    different — or a wider — slice than the one the round's patterns and
    loss summary were computed over. No slice is ever accepted as an
    argument.

    FAIL CLOSED: every failure path (no epoch id, no board, no scoring, an
    unparseable either, an empty train slice) returns an EMPTY id set plus
    a human-readable reason. There is no whole-board fallback: a
    silently-widened slice is the failure this module exists to prevent.
    """
    from zicato.board.jsonl import load_board  # noqa: PLC0415
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
    from zicato.core.workspace import board_path, scoring_path  # noqa: PLC0415
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    if not ctx.epoch_id:
        return frozenset(), "no epoch id is bound in this round's proposer context"

    bpath = board_path(ctx.workspace_root, ctx.epoch_id)
    if not bpath.is_file():
        return frozenset(), f"no board.jsonl for epoch {ctx.epoch_id!r}"
    try:
        board = load_board(bpath)
    except (OSError, ValueError) as exc:
        return frozenset(), f"board.jsonl for epoch {ctx.epoch_id!r} could not be read: {exc}"

    spath = scoring_path(ctx.workspace_root, ctx.epoch_id)
    if not spath.is_file():
        return frozenset(), f"no scoring.json for epoch {ctx.epoch_id!r}"
    try:
        weights = scoring_weights_from_dict(json.loads(spath.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return frozenset(), f"scoring.json for epoch {ctx.epoch_id!r} could not be read: {exc}"

    seed = rotation_seed(weights.overfitting, ctx.epoch_id)
    train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=seed)
    if not train_ids:
        return frozenset(), f"the train slice for epoch {ctx.epoch_id!r} is empty"
    return frozenset(train_ids), ""


def drop_out_of_slice(
    rows: Mapping[str, _EntryFacts], train_ids: frozenset[str]
) -> dict[str, _EntryFacts]:
    """GATE 2: drop every row whose entry id is not in the train slice.

    Defence in depth, and independent of gate 1 by design (which opens
    only train-slice event files). A single gate is one refactor away from
    being bypassed — a view that arrives "already filtered" is trusted
    exactly once and then quietly is not. This filter re-checks membership
    by entry id no matter where the rows came from, so a holdout row can
    only reach an output by passing BOTH gates.
    """
    return {entry_id: facts for entry_id, facts in rows.items() if entry_id in train_ids}


# ---------------------------------------------------------------------------
# Per-entry facts — closed-vocabulary SETS, never per-event magnitudes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EntryFacts:
    """What one train-slice run contributes, as membership sets.

    Sets rather than counts: each entry contributes at most once to each rate, so
    every reported figure is "the fraction of train entries in which this
    happened at least once" — a marginal. A per-event count would let one
    chatty run dominate a figure and would make a per-entry magnitude
    recoverable; neither is worth the resolution.
    """

    drift: frozenset[tuple[str, str]] = frozenset()
    agents_invoked: frozenset[str] = frozenset()
    agents_drifting: frozenset[str] = frozenset()
    agents_steered: frozenset[str] = frozenset()
    process_cases: frozenset[str] = frozenset()
    steering_outcomes: frozenset[str] = frozenset()
    intervention_levels: frozenset[str] = frozenset()
    judge_classifications: frozenset[str] = frozenset()
    unrecoverable_failure: bool = False


def _entry_facts(events: Sequence[Mapping[str, Any]], entry_id: str) -> _EntryFacts:
    """Distil one run's events into redacted, closed-vocabulary facts."""
    texts, tokens = _identity_corpus(events, entry_id)

    def label(raw: Any) -> str:
        return _safe_label(raw, texts, tokens)

    drift: set[tuple[str, str]] = set()
    agents_invoked: set[str] = set()
    agents_drifting: set[str] = set()
    agents_steered: set[str] = set()
    process_cases: set[str] = set()
    steering_outcomes: set[str] = set()
    intervention_levels: set[str] = set()
    judge_classifications: set[str] = set()
    unrecoverable = False

    for event in events:
        case, payload = _case_and_payload(event)
        if case is None or case not in _READ_POLICY:
            continue
        if case in _PROCESS_CASES:
            process_cases.add(case)
        if case == "drift_detected":
            kind = _norm(payload, "kind")
            severity = _norm(payload, "severity")
            if kind:
                drift.add((label(kind), label(severity or "(unspecified)")))
            agent = payload.get("current_agent_id")
            if isinstance(agent, str) and agent:
                agents_drifting.add(label(agent))
        elif case in ("agent_invocation_started", "agent_invocation_completed"):
            agent = payload.get("agent_name")
            if isinstance(agent, str) and agent:
                agents_invoked.add(label(agent))
        elif case == "steering_decision_made":
            agent = payload.get("agent_name")
            if isinstance(agent, str) and agent:
                agents_steered.add(label(agent))
            outcome = payload.get("outcome")
            if isinstance(outcome, str) and outcome:
                steering_outcomes.add(label(outcome))
            level = payload.get("chosen_intervention_level")
            if isinstance(level, str) and level:
                intervention_levels.add(label(level))
        elif case == "reasoning_judge_invoked":
            classification = payload.get("classification")
            if isinstance(classification, str) and classification:
                judge_classifications.add(label(classification))
        elif case == "task_failed" and payload.get("recoverable") is False:
            unrecoverable = True

    return _EntryFacts(
        drift=frozenset(drift),
        agents_invoked=frozenset(agents_invoked),
        agents_drifting=frozenset(agents_drifting),
        agents_steered=frozenset(agents_steered),
        process_cases=frozenset(process_cases),
        steering_outcomes=frozenset(steering_outcomes),
        intervention_levels=frozenset(intervention_levels),
        judge_classifications=frozenset(judge_classifications),
        unrecoverable_failure=unrecoverable,
    )


def _norm(payload: Mapping[str, Any], field: str) -> str:
    """Normalize a drift-kind / severity wire enum to its canonical string."""
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw:
        return ""
    if field in _DRIFT_KIND_FIELDS:
        return normalize_wire_drift_kind(raw) or raw
    if field in _SEVERITY_FIELDS:
        return normalize_wire_severity(raw) or raw
    return raw


# ---------------------------------------------------------------------------
# The shared collection + banding machinery
# ---------------------------------------------------------------------------


def _collect() -> tuple[dict[str, _EntryFacts], int, str]:
    """Return ``(facts_by_entry, train_slice_size, reason)``, both gates applied.

    GATE 1 opens only the event files of entries in the derived train
    slice; GATE 2 (:func:`drop_out_of_slice`) re-filters the collected
    mapping by entry id afterwards. A non-empty ``reason`` means the slice
    could not be derived — the caller must emit :data:`_UNAVAILABLE` and no
    data.
    """
    from zicato.core.workspace import events_jsonl_path  # noqa: PLC0415
    from zicato.tournament.unit_cache import any_unit_transcript  # noqa: PLC0415

    ctx = _active_context()
    train_ids, reason = _derive_train_slice(ctx)
    if reason:
        return {}, 0, reason
    if not ctx.generation_id:
        return {}, len(train_ids), "no champion generation id is bound in this round's context"

    collected: dict[str, _EntryFacts] = {}
    for entry_id in sorted(train_ids):  # GATE 1: train-slice files only.
        events = _read_events(
            any_unit_transcript(
                events_jsonl_path(ctx.workspace_root, ctx.epoch_id, ctx.generation_id, entry_id)
            )
        )
        if not events:
            continue
        collected[entry_id] = _entry_facts(events, entry_id)
    return drop_out_of_slice(collected, train_ids), len(train_ids), ""  # GATE 2


def _band(count: int, denominator: int) -> str:
    """Coarsen ``count / denominator`` through the existing band vocabulary.

    Reuses :func:`zicato.proposer.prompts.band_rate` (lazy import — the
    prompt renderer is the band vocabulary's home) rather than inventing a
    second banding scheme, so ``~20%`` means the same thing here as it does
    in the failure-mode profile.
    """
    from zicato.proposer.prompts import band_rate  # noqa: PLC0415

    if denominator <= 0:
        return "none"
    return band_rate(count / denominator)


def _band_order(label: str) -> float:
    """Sort weight of a band LABEL — derived from the label, never the rate.

    Ordering rows by their exact rate would hand back finer resolution than
    the band itself (the ranking within a band would encode the underlying
    numbers). Sorting by the label, then alphabetically, keeps the ordering
    exactly as coarse as the value it displays.
    """
    if label == "~all":
        return 1.0
    if label == "none":
        return 0.0
    try:
        return float(label.strip("~%")) / 100.0
    except ValueError:
        return 0.0


def _banded_rows(
    counts: Mapping[str, int], denominator: int, key_name: str
) -> list[dict[str, str]]:
    """Render ``{label: count}`` as band-sorted, band-valued rows."""
    rows = [
        {key_name: name, "entries_affected": _band(count, denominator)}
        for name, count in counts.items()
    ]
    rows.sort(key=lambda r: (-_band_order(r["entries_affected"]), r[key_name]))
    return rows[:_LABEL_LIMIT]


def _tally(facts: Mapping[str, _EntryFacts], attr: str) -> dict[str, int]:
    """Count, per member of a per-entry set attribute, how many entries carry it."""
    out: dict[str, int] = {}
    for entry in facts.values():
        for member in getattr(entry, attr):
            out[member] = out.get(member, 0) + 1
    return out


def _envelope(tool: str, facts: Mapping[str, _EntryFacts], slice_size: int) -> dict[str, Any]:
    """The header every tool payload carries — basis, scope, and honesty."""
    return {
        "tool": tool,
        "status": "ok",
        "basis": (
            "TRAIN SLICE ONLY, current champion generation. Every figure is the "
            "BANDED fraction of train-slice entries in which the thing happened at "
            "least once — an aggregate over the slice, never a per-entry value. No "
            "entry id, task text, model output, or holdout data is reachable from "
            "this surface."
        ),
        "train_slice_entries": slice_size,
        "entries_with_events": len(facts),
    }


def _unavailable(tool: str, reason: str) -> str:
    """The fail-closed payload: the status, the reason, and NO data."""
    return json.dumps({"tool": tool, "status": _UNAVAILABLE, "reason": reason}, indent=2)


def _empty_note(facts: Mapping[str, _EntryFacts]) -> dict[str, Any]:
    """The explicit "available but nothing recorded yet" annotation."""
    if facts:
        return {}
    return {"note": "(no train-slice event data recorded for the champion generation yet)"}


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def train_slice_drift_profile() -> str:
    """How often each drift kind fires across the champion's TRAIN slice.

    Returns JSON: for every drift kind observed, the BANDED fraction of
    train-slice entries in which that kind fired at least once, plus the
    banded incidence of each severity it fired at. Sorted by band, then
    alphabetically.

    Use it to target *which failure mode* to attack, and to check whether a
    mode the pattern block named is broad (most entries) or narrow (a few).

    REDACTION CONTRACT — this tool cannot tell you which board entries do
    anything. It reads only the train slice (the holdout is never opened),
    emits only aggregate counts over that slice, and never emits an entry
    id, task text, or model output; drift kinds and severities are a closed
    harness vocabulary. Figures are banded (``none`` / ``~20%`` / ``~all``),
    never exact, and they do not change between rounds while the champion
    is unchanged — so there is nothing here to hill-climb. If the train
    slice cannot be derived the tool returns ``status: "train slice
    unavailable"`` and no data.
    """
    facts, slice_size, reason = _collect()
    if reason:
        return _unavailable("train_slice_drift_profile", reason)

    kind_counts: dict[str, int] = {}
    severity_counts: dict[str, dict[str, int]] = {}
    for entry in facts.values():
        for kind in {k for k, _s in entry.drift}:
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        for kind, severity in entry.drift:
            bucket = severity_counts.setdefault(kind, {})
            bucket[severity] = bucket.get(severity, 0) + 1

    denominator = len(facts)
    kinds: list[dict[str, Any]] = []
    for row in _banded_rows(kind_counts, denominator, "drift_kind"):
        kind = row["drift_kind"]
        kinds.append(
            {
                **row,
                "severity_mix": _banded_rows(
                    severity_counts.get(kind, {}), denominator, "severity"
                ),
            }
        )
    payload: dict[str, Any] = {
        **_envelope("train_slice_drift_profile", facts, slice_size),
        "drift_kinds": kinds,
        **_empty_note(facts),
    }
    return json.dumps(payload, indent=2)


def train_slice_agent_profile() -> str:
    """Which agents run, drift, and get steered across the TRAIN slice.

    Returns JSON: one row per agent the harness invoked, with the BANDED
    fraction of train-slice entries in which that agent was invoked, was
    the agent a drift finding was attributed to, and was the subject of a
    steering decision. Sorted by band, then alphabetically.

    Use it to localise a failure to a role — "the researcher is invoked
    everywhere but the coordinator is the one being steered" — before
    choosing which prompt or config to mutate.

    REDACTION CONTRACT — agent names are HARNESS-side identity (they come
    from the snapshot you can already read with ``read_mutable_file``), not
    board identity, and each is still passed through the mechanical
    identity scrub before it is emitted, so an agent label that echoes a
    run / task / entry id is withheld. Train slice only; aggregate counts
    only; no entry id, task text, or model output is reachable. Figures are
    banded, never exact, and stable while the champion is unchanged. If the
    train slice cannot be derived the tool returns ``status: "train slice
    unavailable"`` and no data.
    """
    facts, slice_size, reason = _collect()
    if reason:
        return _unavailable("train_slice_agent_profile", reason)

    denominator = len(facts)
    invoked = _tally(facts, "agents_invoked")
    drifting = _tally(facts, "agents_drifting")
    steered = _tally(facts, "agents_steered")
    names = sorted(set(invoked) | set(drifting) | set(steered))
    rows = [
        {
            "agent": name,
            "entries_invoked": _band(invoked.get(name, 0), denominator),
            "entries_with_attributed_drift": _band(drifting.get(name, 0), denominator),
            "entries_with_steering_decision": _band(steered.get(name, 0), denominator),
        }
        for name in names
    ]
    rows.sort(key=lambda r: (-_band_order(r["entries_with_attributed_drift"]), r["agent"]))
    payload: dict[str, Any] = {
        **_envelope("train_slice_agent_profile", facts, slice_size),
        "agents": rows[:_LABEL_LIMIT],
        **_empty_note(facts),
    }
    if len(rows) > _LABEL_LIMIT:
        payload["truncated"] = f"showing the first {_LABEL_LIMIT} of {len(rows)} agents"
    return json.dumps(payload, indent=2)


def train_slice_process_profile() -> str:
    """How runs go wrong procedurally across the champion's TRAIN slice.

    Returns JSON: the BANDED fraction of train-slice entries that hit each
    process-failure event (task failed / blocked / cancelled, plan revised)
    and an unrecoverable task failure, plus the banded mix of steering
    outcomes, chosen intervention levels, and reasoning-judge
    classifications. Sorted by band, then alphabetically.

    Use it to tell a *mechanism* failure (plans thrash, tasks block, the
    steerer keeps escalating) from a *content* failure the drift profile
    already describes — the two want different edits.

    REDACTION CONTRACT — every value here is either a goldfive payload-case
    name or a closed enum vocabulary (steering outcome, intervention level,
    judge classification); each is still passed through the mechanical
    identity scrub before it is emitted. Train slice only; aggregate counts
    only; no entry id, task text, model output, or reason/detail free text
    is reachable — the free-text fields of these very events are dropped by
    the read allowlist, not truncated. Figures are banded, never exact, and
    stable while the champion is unchanged. If the train slice cannot be
    derived the tool returns ``status: "train slice unavailable"`` and no
    data.
    """
    facts, slice_size, reason = _collect()
    if reason:
        return _unavailable("train_slice_process_profile", reason)

    denominator = len(facts)
    case_counts = _tally(facts, "process_cases")
    unrecoverable = sum(1 for entry in facts.values() if entry.unrecoverable_failure)
    payload: dict[str, Any] = {
        **_envelope("train_slice_process_profile", facts, slice_size),
        "process_failures": _banded_rows(case_counts, denominator, "event"),
        "unrecoverable_task_failure": _band(unrecoverable, denominator),
        "steering_outcomes": _banded_rows(
            _tally(facts, "steering_outcomes"), denominator, "outcome"
        ),
        "intervention_levels": _banded_rows(
            _tally(facts, "intervention_levels"), denominator, "level"
        ),
        "reasoning_judge_classifications": _banded_rows(
            _tally(facts, "judge_classifications"), denominator, "classification"
        ),
        **_empty_note(facts),
    }
    return json.dumps(payload, indent=2)


#: The redacted run-corpus query tools, ready to splice into
#: ``zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS``. Kept as ONE tuple so
#: the adversarial identity-leak probe in
#: ``tests/test_proposer_redacted_query.py`` loops over it — a tool added
#: here is covered by the probe automatically and cannot skip it.
REDACTED_QUERY_TOOLS = (
    train_slice_drift_profile,
    train_slice_agent_profile,
    train_slice_process_profile,
)


__all__ = [
    "REDACTED_QUERY_TOOLS",
    "drop_out_of_slice",
    "train_slice_agent_profile",
    "train_slice_drift_profile",
    "train_slice_process_profile",
]
