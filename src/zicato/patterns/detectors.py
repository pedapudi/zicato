"""Cross-run pattern detectors over LossProfile windows.

Each detector is a pure function ``DetectorInput -> list[Pattern]``. The
input bundles three things: every per-run
:class:`zicato.core.LossProfile` over the window the caller cares about,
the :class:`zicato.core.BoardEntry` catalog, so detectors can slice by tag
or kind, and a map from entry id to the goldfive events JSONL path. The
last is for detectors that need the raw event stream: a LossProfile is a
digest, while raw events carry the agent and task identifiers a detector
like :func:`detect_hot_tasks` needs.

Design constraints:

* Pure. No I/O outside reading the events JSONL paths the caller
  supplied. No mutation of the inputs.
* Empty input → empty output. Every detector tolerates zero losses
  (the very first generation has no history) and tolerates entries
  with no events file (older runs whose JSONL was rotated away).
* Deterministic ids. :attr:`zicato.core.Pattern.id` is a sha1 of the
  concatenated (kind, summary, affected-ids-tuple) so the same
  finding produces the same id across runs — the proposer can dedupe
  patterns it has already addressed.
* Goldfive-optional. Detectors that need raw events import goldfive
  lazily inside the function and return ``[]`` silently when goldfive
  is not importable. Tests for those detectors use
  ``pytest.importorskip("goldfive")``.
* Empty ``affected_mutation_ids``. The proposer is responsible for
  resolving a pattern to specific mutation points; the detector layer
  only surfaces the loss signal.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import BoardEntry, LossProfile, Pattern

# ---------------------------------------------------------------------------
# Detector input + alias
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectorInput:
    """Bundle of cross-run data every detector reads.

    Fields
    ------
    losses:
        One :class:`LossProfile` per ``(entry, generation)`` over the
        window the caller wants the detectors to consider. The detectors
        do NOT scope the window themselves — the caller has already
        decided what "history" means (typically the most recent N
        generations within the open epoch).
    entries:
        Map from :attr:`BoardEntry.id` to the :class:`BoardEntry`. Used
        by detectors that slice on board-side metadata (entry kind,
        tags, max_turns).
    events_paths:
        Map from :attr:`BoardEntry.id` to an absolute path of a goldfive
        events JSONL. Detectors that need raw events (hot-tasks,
        hot-agents) replay these files. Missing entries are tolerated:
        the detector simply skips the entry. Entries with multiple runs
        SHOULD point at the most recent events file (the caller picks).
    """

    losses: list[LossProfile]
    entries: dict[str, BoardEntry]
    events_paths: dict[str, Path]


#: The detector callable shape. Take a ``DetectorInput`` and return a
#: list of :class:`Pattern` instances.
DetectorFn = Callable[[DetectorInput], list[Pattern]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pattern_id(kind: str, summary: str, affected_ids: Iterable[str]) -> str:
    """Deterministic short hash for a :class:`Pattern`.

    The id is sha1 of ``kind|summary|sorted(affected_ids)`` truncated
    to 16 hex chars — long enough that collisions across a few thousand
    patterns are negligible, short enough that it renders cleanly in
    journal output.
    """

    sorted_ids = ",".join(sorted(affected_ids))
    payload = f"{kind}|{summary}|{sorted_ids}".encode()
    return hashlib.sha1(payload).hexdigest()[:16]


def _safe_mean(values: list[float] | list[int]) -> float:
    """Mean of *values*, or ``0.0`` when empty.

    Detectors call this in places where an empty window is meaningful
    and a raised exception would not be — the caller has already
    decided the window is worth analysing.
    """

    if not values:
        return 0.0
    return float(statistics.fmean(values))


def _safe_median(values: list[float] | list[int]) -> float:
    """Median of *values*, or ``0.0`` when empty. See :func:`_safe_mean`."""

    if not values:
        return 0.0
    return float(statistics.median(values))


def _replay_events(path: Path) -> list[Any] | None:
    """Replay a goldfive events JSONL, or return ``None`` if unavailable.

    Returns ``None`` when goldfive is not importable OR the file does
    not exist OR the replay raised. Detectors that need events treat a
    ``None`` as "skip this entry" so a single corrupted file does not
    erase the rest of the detector's output.
    """

    try:
        from goldfive.sinks import replay_from_jsonl
    except Exception:
        return None
    if not Path(path).exists():
        return None
    try:
        events: list[Any] = replay_from_jsonl(path)
        return events
    except Exception:
        return None


def _payload_name(event: Any) -> str | None:
    """Return the goldfive ``Event.payload`` oneof case name, or ``None``.

    The proto stub exposes the populated oneof case via ``WhichOneof``;
    we use the case name (e.g. ``"task_started"``) rather than the
    payload field directly so the detector code does not need to know
    the proto's field numbering.
    """

    try:
        result: str | None = event.WhichOneof("payload")
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def detect_metric_frequency(
    inp: DetectorInput,
    namespace: str = "drift:",
    *,
    pattern_kind: str | None = None,
    min_frequency: float = 0.20,
) -> list[Pattern]:
    """One Pattern per namespaced metric that fires in >= ``min_frequency`` of runs.

    Generalises :func:`detect_drift_kind_frequency` to any
    :class:`MetricCount` namespace. ``namespace`` is matched against the
    ``MetricCount.name`` prefix (so ``"drift:"`` matches every drift
    kind, ``"cost:"`` matches every cost metric, etc.). The empty
    string matches every namespace.

    A metric "fires" in a run iff at least one
    :class:`zicato.core.MetricCount` entry under :meth:`LossProfile.unified_metrics`
    has the prefix and a positive count. Severity is the max-severity
    bucket observed for the metric in the window (or ``"info"`` when
    the namespace doesn't carry severity).

    Emits Pattern.kind = ``pattern_kind`` (defaults to
    ``"{namespace}metric_frequency"`` with the trailing colon stripped;
    e.g. ``"drift:" -> "drift_metric_frequency"``). Detail keys mirror
    the drift-only surface for back-compat: ``metric_name``,
    ``frequency``, ``run_count``, ``hits``, ``max_severity``,
    ``affected_entry_ids``.

    Parameters
    ----------
    inp:
        The detector input bundle.
    namespace:
        Prefix string the metric name must start with. Use ``"drift:"``
        for drift-only detection (the back-compat path), ``"cost:"`` for
        cost metrics, etc. Empty string matches every metric.
    pattern_kind:
        Override for :attr:`Pattern.kind`. When ``None`` the kind is
        derived from the namespace.
    min_frequency:
        Minimum per-window frequency for a metric to be surfaced.
        Default 0.20 matches the historical drift-detector threshold.
    """

    losses = inp.losses
    if not losses:
        return []

    total_runs = len(losses)
    if pattern_kind is None:
        ns = namespace.rstrip(":")
        pattern_kind = f"{ns}_metric_frequency" if ns else "metric_frequency"

    # metric_name -> set of run_ids that fired the metric
    metric_hits: dict[str, set[str]] = {}
    # metric_name -> ranked max severity observed
    severity_rank = {"info": 0, "warning": 1, "critical": 2}
    metric_max_sev: dict[str, str] = {}
    # metric_name -> set of affected entry ids
    metric_entries: dict[str, set[str]] = {}

    for loss in losses:
        for mc in loss.unified_metrics():
            if mc.count <= 0:
                continue
            if namespace and not mc.name.startswith(namespace):
                continue
            metric_hits.setdefault(mc.name, set()).add(loss.run_id)
            metric_entries.setdefault(mc.name, set()).add(loss.entry_id)
            sev = mc.severity or "info"
            current = metric_max_sev.get(mc.name)
            if current is None or severity_rank.get(sev, -1) > severity_rank.get(current, -1):
                metric_max_sev[mc.name] = sev

    patterns: list[Pattern] = []
    for metric_name, run_ids in sorted(metric_hits.items()):
        frequency = len(run_ids) / total_runs
        if frequency < min_frequency:
            continue
        affected = sorted(metric_entries.get(metric_name, set()))
        pct = round(frequency * 100, 1)
        # Strip namespace prefix for the human-readable display when
        # the caller asked for a single namespace; full name otherwise.
        if namespace and metric_name.startswith(namespace):
            display = metric_name[len(namespace) :]
        else:
            display = metric_name
        label = "drift kind" if namespace == "drift:" else "metric"
        summary = f"{label} {display!r} fires in {pct}% of runs across {len(affected)} entries"
        max_sev = metric_max_sev.get(metric_name, "info")
        pattern_severity: str = max_sev if max_sev in ("info", "warning", "critical") else "info"
        # For drift namespace the back-compat detail key is
        # ``drift_kind``; for everything else we use ``metric_name`` so
        # the proposer sees a more general label.
        detail: dict[str, str] = {
            "metric_name": metric_name,
            "frequency": f"{frequency:.3f}",
            "run_count": str(total_runs),
            "hits": str(len(run_ids)),
            "max_severity": max_sev,
            "affected_entry_ids": ",".join(affected),
        }
        if namespace == "drift:":
            detail["drift_kind"] = display
        patterns.append(
            Pattern(
                id=_pattern_id(pattern_kind, summary, affected),
                kind=pattern_kind,
                summary=summary,
                detail=detail,
                affected_mutation_ids=(),
                severity=pattern_severity,  # type: ignore[arg-type]
            )
        )
    return patterns


def detect_drift_kind_frequency(inp: DetectorInput) -> list[Pattern]:
    """One Pattern per drift kind that fires in >=20% of runs.

    Back-compat wrapper over :func:`detect_metric_frequency` with
    ``namespace="drift:"`` and ``pattern_kind="drift_kind_frequency"``.
    The detail dict includes ``drift_kind`` for old consumers and
    ``metric_name`` for new ones; the latter carries the fully
    namespaced name (e.g. ``"drift:off_topic"``).

    "Fires" means at least one :class:`zicato.core.DriftCount` with
    ``count > 0`` for that kind appears in the run's
    :attr:`LossProfile.drift_counts`. Severity is summarised as the
    max-severity bucket observed for that kind in the window.
    """

    return detect_metric_frequency(inp, namespace="drift:", pattern_kind="drift_kind_frequency")


def detect_cost_outliers(inp: DetectorInput) -> list[Pattern]:
    """One Pattern per ``cost:*`` metric that fires in >=20% of runs.

    Surfaces token/call-volume hotspots so the proposer can target
    cost-side objectives directly. Emits
    :attr:`Pattern.kind` = ``"cost_metric_frequency"``.
    """

    return detect_metric_frequency(inp, namespace="cost:", pattern_kind="cost_metric_frequency")


def detect_rubric_score_movement(inp: DetectorInput) -> list[Pattern]:
    """One Pattern per ``rubric:*`` metric that fires in >=20% of runs.

    For rubric scores, "fires" is interpreted as "has a non-zero score
    recorded in the run". This lets the proposer notice which rubric
    dimensions the harness is actually scoring on. Emits
    :attr:`Pattern.kind` = ``"rubric_metric_frequency"``.
    """

    return detect_metric_frequency(inp, namespace="rubric:", pattern_kind="rubric_metric_frequency")


def detect_hot_tasks(inp: DetectorInput) -> list[Pattern]:
    """Tasks that fail or block at more than 2x the median per-task rate.

    Reads goldfive events directly via ``replay_from_jsonl`` to extract
    task lifecycle counts. For each entry whose events file is
    available, count ``TaskStarted`` per ``task_id`` (denominator) and
    ``TaskFailed + TaskBlocked`` per ``task_id`` (numerator). Compute
    the failure-or-block rate per task; flag the tasks whose rate is
    >= max(2 * median_rate, 0.5) — the 0.5 floor avoids over-firing
    when the entire window's median rate is near zero.

    Returns ``[]`` silently when goldfive is not importable.
    """

    # Group tasks by (entry_id, task_id) so the same task across multiple
    # runs of the same entry aggregates; tasks in different entries are
    # kept distinct.
    started: Counter[tuple[str, str]] = Counter()
    failed_or_blocked: Counter[tuple[str, str]] = Counter()

    saw_any_events = False
    for entry_id, path in inp.events_paths.items():
        events = _replay_events(path)
        if events is None:
            continue
        saw_any_events = True
        for ev in events:
            case = _payload_name(ev)
            if case == "task_started":
                tid = ev.task_started.task_id
                if tid:
                    started[(entry_id, tid)] += 1
            elif case == "task_failed":
                tid = ev.task_failed.task_id
                if tid:
                    failed_or_blocked[(entry_id, tid)] += 1
            elif case == "task_blocked":
                tid = ev.task_blocked.task_id
                if tid:
                    failed_or_blocked[(entry_id, tid)] += 1

    if not saw_any_events or not started:
        return []

    rates: dict[tuple[str, str], float] = {}
    for key, start_count in started.items():
        if start_count <= 0:
            continue
        rates[key] = failed_or_blocked.get(key, 0) / start_count

    if not rates:
        return []

    median_rate = _safe_median(list(rates.values()))
    threshold = max(2.0 * median_rate, 0.5)

    patterns: list[Pattern] = []
    for (entry_id, task_id), rate in sorted(rates.items()):
        if rate < threshold:
            continue
        summary = (
            f"task {task_id!r} in entry {entry_id!r} fails or blocks "
            f"{rate:.0%} of starts (>= {threshold:.0%} threshold)"
        )
        sev: str = "warning" if rate >= max(0.75, threshold) else "info"
        patterns.append(
            Pattern(
                id=_pattern_id("hot_task", summary, (entry_id, task_id)),
                kind="hot_task",
                summary=summary,
                detail={
                    "entry_id": entry_id,
                    "task_id": task_id,
                    "fail_or_block_rate": f"{rate:.3f}",
                    "starts": str(started[(entry_id, task_id)]),
                    "fail_or_block_count": str(failed_or_blocked.get((entry_id, task_id), 0)),
                    "median_rate": f"{median_rate:.3f}",
                    "threshold": f"{threshold:.3f}",
                },
                affected_mutation_ids=(),
                severity=sev,  # type: ignore[arg-type]
            )
        )
    return patterns


def detect_hot_agents(inp: DetectorInput) -> list[Pattern]:
    """Agents that produce disproportionate drift counts.

    For each entry whose events file is readable, build per-agent drift
    counts by joining ``AgentInvocationStarted/Completed`` (agent_name
    + invocation_id) onto ``DriftDetected`` (current_agent_id). An
    agent's "drift count" is the number of ``DriftDetected`` events
    whose ``current_agent_id`` matches its name. Flag agents whose
    count is >= max(2 * mean_drifts_per_agent, 3) — the 3-event floor
    avoids over-firing on a quiet window.

    Returns ``[]`` silently when goldfive is not importable.
    """

    # Aggregate across all entries: a hot agent in one entry is still a
    # signal worth surfacing, but we key the pattern by (entry_id,
    # agent_name) so the proposer can target the specific entry.
    drifts_per_agent: Counter[tuple[str, str]] = Counter()
    saw_any_events = False

    for entry_id, path in inp.events_paths.items():
        events = _replay_events(path)
        if events is None:
            continue
        saw_any_events = True
        # Collect agent names from invocation events (so an agent shows
        # up even when it produced no drift — we still want the
        # denominator).
        agents_seen: set[str] = set()
        for ev in events:
            case = _payload_name(ev)
            if case == "agent_invocation_started":
                name = ev.agent_invocation_started.agent_name
                if name:
                    agents_seen.add(name)
            elif case == "agent_invocation_completed":
                name = ev.agent_invocation_completed.agent_name
                if name:
                    agents_seen.add(name)
            elif case == "drift_detected":
                agent = ev.drift_detected.current_agent_id
                if agent:
                    drifts_per_agent[(entry_id, agent)] += 1
                    agents_seen.add(agent)
        # Ensure every observed agent has a key (count 0 is fine — it
        # contributes to the mean).
        for name in agents_seen:
            drifts_per_agent.setdefault((entry_id, name), 0)

    if not saw_any_events or not drifts_per_agent:
        return []

    counts = [float(c) for c in drifts_per_agent.values()]
    mean_drifts = _safe_mean(counts)
    threshold = max(2.0 * mean_drifts, 3.0)

    patterns: list[Pattern] = []
    for (entry_id, agent_name), count in sorted(drifts_per_agent.items()):
        if count < threshold:
            continue
        summary = (
            f"agent {agent_name!r} in entry {entry_id!r} produced "
            f"{count} drift events (>= {threshold:.1f} threshold)"
        )
        sev: str = "warning" if count >= max(2.0 * threshold, 6.0) else "info"
        patterns.append(
            Pattern(
                id=_pattern_id("hot_agent", summary, (entry_id, agent_name)),
                kind="hot_agent",
                summary=summary,
                detail={
                    "entry_id": entry_id,
                    "agent_name": agent_name,
                    "drift_count": str(count),
                    "mean_drifts_per_agent": f"{mean_drifts:.3f}",
                    "threshold": f"{threshold:.3f}",
                },
                affected_mutation_ids=(),
                severity=sev,  # type: ignore[arg-type]
            )
        )
    return patterns


def detect_plan_revision_instability(inp: DetectorInput) -> list[Pattern]:
    """Plan-revision-count outliers (>= 2x mean) suggest steerer flapping.

    Emits one Pattern when at least one run's plan-revision count is
    >= max(2 * mean, mean + 2). The +2 floor handles low-mean windows
    where 2x of a small number is itself small — a single
    plan_revisions=5 against a window mean of 1.0 is interesting, but
    a 0.6 -> 1.2 nudge is not.
    """

    losses = inp.losses
    if not losses:
        return []

    counts = [loss.plan_revisions for loss in losses]
    mean_count = _safe_mean(counts)
    threshold = max(2.0 * mean_count, mean_count + 2.0)

    flapping: list[LossProfile] = [loss for loss in losses if loss.plan_revisions >= threshold]
    if not flapping:
        return []

    affected_entry_ids = sorted({loss.entry_id for loss in flapping})
    summary = (
        f"plan-revision instability: {len(flapping)} run(s) with "
        f">= {threshold:.1f} revisions (mean {mean_count:.2f})"
    )
    sev: str = (
        "warning" if any(loss.plan_revisions >= mean_count + 4 for loss in flapping) else "info"
    )
    return [
        Pattern(
            id=_pattern_id(
                "plan_revision_instability",
                summary,
                affected_entry_ids,
            ),
            kind="plan_revision_instability",
            summary=summary,
            detail={
                "mean_revisions": f"{mean_count:.3f}",
                "threshold": f"{threshold:.3f}",
                "outlier_run_count": str(len(flapping)),
                "outlier_run_ids": ",".join(loss.run_id for loss in flapping),
                "affected_entry_ids": ",".join(affected_entry_ids),
                "max_revisions": str(max(loss.plan_revisions for loss in flapping)),
            },
            affected_mutation_ids=(),
            severity=sev,  # type: ignore[arg-type]
        )
    ]


def _multi_turn_failure_pattern(
    inp: DetectorInput,
    *,
    field_name: str,
    pattern_kind: str,
    summary_label: str,
) -> list[Pattern]:
    """Shared body for :func:`detect_multi_turn_memory_failure` and
    :func:`detect_multi_turn_context_loss`.

    Scans :class:`LossProfile` instances whose ``field_name`` attribute
    is not ``None`` (i.e. multi-turn runs) and emits a Pattern when the
    fraction of runs with a positive count is >= 30%. The two surfaced
    detectors are structurally identical; only the underlying field
    differs, so this helper keeps them in lockstep.
    """

    losses = inp.losses
    if not losses:
        return []

    # Group by entry_id so a hot entry doesn't get diluted by every
    # other entry's calm runs.
    by_entry: dict[str, list[int]] = {}
    for loss in losses:
        value = getattr(loss, field_name)
        if value is None:
            continue
        by_entry.setdefault(loss.entry_id, []).append(int(value))

    patterns: list[Pattern] = []
    for entry_id, counts in sorted(by_entry.items()):
        if not counts:
            continue
        positives = sum(1 for c in counts if c > 0)
        rate = positives / len(counts)
        if rate < 0.30:
            continue
        summary = (
            f"{summary_label} in entry {entry_id!r}: "
            f"{positives}/{len(counts)} runs ({rate:.0%}) reported >0"
        )
        sev: str = "warning" if rate >= 0.60 else "info"
        patterns.append(
            Pattern(
                id=_pattern_id(pattern_kind, summary, (entry_id,)),
                kind=pattern_kind,
                summary=summary,
                detail={
                    "entry_id": entry_id,
                    "run_count": str(len(counts)),
                    "positive_run_count": str(positives),
                    "rate": f"{rate:.3f}",
                    "max_count": str(max(counts)),
                    "total_count": str(sum(counts)),
                },
                affected_mutation_ids=(),
                severity=sev,  # type: ignore[arg-type]
            )
        )
    return patterns


def detect_multi_turn_memory_failure(inp: DetectorInput) -> list[Pattern]:
    """Multi-turn entries where memory_failure_count > 0 in >=30% of runs."""

    return _multi_turn_failure_pattern(
        inp,
        field_name="memory_failure_count",
        pattern_kind="multi_turn_memory_failure",
        summary_label="multi-turn memory failure",
    )


def detect_multi_turn_context_loss(inp: DetectorInput) -> list[Pattern]:
    """Multi-turn entries where context_loss_count > 0 in >=30% of runs."""

    return _multi_turn_failure_pattern(
        inp,
        field_name="context_loss_count",
        pattern_kind="multi_turn_context_loss",
        summary_label="multi-turn context loss",
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


#: The canonical detector list, in deterministic application order. The
#: order matters only for the journal — :func:`detect_patterns` dedupes
#: by id so the same finding from two detectors collapses to the first
#: one's row.
ALL_DETECTORS: tuple[DetectorFn, ...] = (
    detect_drift_kind_frequency,
    detect_cost_outliers,
    detect_rubric_score_movement,
    detect_hot_tasks,
    detect_hot_agents,
    detect_plan_revision_instability,
    detect_multi_turn_memory_failure,
    detect_multi_turn_context_loss,
)


def detect_patterns(
    inp: DetectorInput, detectors: tuple[DetectorFn, ...] = ALL_DETECTORS
) -> list[Pattern]:
    """Run every detector, concatenate Pattern lists, dedupe by id.

    Detectors are invoked in the order given by *detectors*. When two
    detectors emit a Pattern with the same :attr:`Pattern.id` the first
    one wins; later duplicates are dropped. The output preserves
    insertion order so the journal renders detectors in
    declared-precedence order.
    """

    seen: set[str] = set()
    out: list[Pattern] = []
    for detector in detectors:
        for pattern in detector(inp):
            if pattern.id in seen:
                continue
            seen.add(pattern.id)
            out.append(pattern)
    return out


__all__ = [
    "ALL_DETECTORS",
    "DetectorFn",
    "DetectorInput",
    "detect_cost_outliers",
    "detect_drift_kind_frequency",
    "detect_hot_agents",
    "detect_hot_tasks",
    "detect_metric_frequency",
    "detect_multi_turn_context_loss",
    "detect_multi_turn_memory_failure",
    "detect_patterns",
    "detect_plan_revision_instability",
    "detect_rubric_score_movement",
]
