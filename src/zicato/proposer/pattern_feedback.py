"""Numeric detector evidence allowed in restricted proposal and critique requests."""

from __future__ import annotations

import math
from dataclasses import dataclass

from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS, DriftSeverity
from zicato.core.patterns import Pattern

# Each kind declares its summary, count fields, and real-valued fields. Diagnostic
# text, including fields added by extensions, cannot become feedback implicitly.
_FREQUENCY_FIELDS = ("run_count", "hits"), ("frequency",)
_MULTI_TURN_FIELDS = (("run_count", "positive_run_count", "max_count", "total_count"), ("rate",))
_FIELDS = {
    "drift_kind_frequency": ("Recurring drift", *_FREQUENCY_FIELDS),
    "drift_metric_frequency": ("Recurring drift", *_FREQUENCY_FIELDS),
    "cost_metric_frequency": ("Recorded resource use", *_FREQUENCY_FIELDS),
    "rubric_metric_frequency": ("Recorded rubric scores", *_FREQUENCY_FIELDS),
    "metric_frequency": ("Recurring measured behavior", *_FREQUENCY_FIELDS),
    "hot_task": (
        "Frequent task failures or blocks",
        ("starts", "fail_or_block_count"),
        ("fail_or_block_rate", "median_rate", "threshold"),
    ),
    "hot_agent": (
        "Concentrated agent drift",
        ("drift_count",),
        ("mean_drifts_per_agent", "threshold"),
    ),
    "plan_revision_instability": (
        "Frequent plan revisions",
        ("outlier_run_count", "max_revisions"),
        ("mean_revisions", "threshold"),
    ),
    "multi_turn_memory_failure": ("Multi-turn memory failures", *_MULTI_TURN_FIELDS),
    "multi_turn_context_loss": ("Multi-turn context loss", *_MULTI_TURN_FIELDS),
    "unrecognized_pattern": ("Detector finding", (), ()),
}
_METRIC_NAMES = frozenset(f"drift:{kind}" for kind in GOLDFIVE_DRIFT_KINDS) | {
    "cost:tokens_spent",
}


@dataclass(frozen=True, slots=True)
class PatternFeedback:
    """Declared measurements without diagnostic ids, text, or task correspondence.

    This is a request-local projection; canonical operator records remain Patterns.
    Metric labels come from the built-in vocabulary. Mutation references identify
    editable code, so they remain available to direct a proposed change.
    """

    kind: str
    severity: DriftSeverity
    statistics: tuple[tuple[str, int | float], ...]
    metric_name: str
    affected_mutation_ids: tuple[str, ...]

    @classmethod
    def from_pattern(cls, pattern: Pattern) -> PatternFeedback:
        """Parse declared numeric fields and discard unknown or invalid values."""
        kind = pattern.kind if pattern.kind in _FIELDS else "unrecognized_pattern"
        _, counts, measurements = _FIELDS[kind]
        statistics: dict[str, int | float] = {}
        for keys, parse in ((counts, int), (measurements, float)):
            for key in keys:
                try:
                    value = parse(pattern.detail[key])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if value >= 0 and (isinstance(value, int) or math.isfinite(value)):
                    statistics[key] = value
        entries = {
            entry.strip()
            for entry in pattern.detail.get("affected_entry_ids", "").split(",")
            if entry.strip()
        }
        if entry := pattern.detail.get("entry_id"):
            entries.add(entry)
        if entries:
            statistics["entries_affected"] = len(entries)
        metric = pattern.detail.get("metric_name", "")
        if not metric and pattern.detail.get("drift_kind") in GOLDFIVE_DRIFT_KINDS:
            metric = "drift:" + pattern.detail["drift_kind"]
        return cls(
            kind=kind,
            severity=DriftSeverity(pattern.severity),
            statistics=tuple(sorted(statistics.items())),
            metric_name=metric if metric in _METRIC_NAMES else "",
            affected_mutation_ids=pattern.affected_mutation_ids,
        )

    def render(self) -> str:
        """Build the summary exclusively from permitted feedback fields."""
        label = _FIELDS[self.kind][0]
        if self.metric_name:
            label += f" ({self.metric_name})"
        measurements = "; ".join(
            f"{key}={value if isinstance(value, int) else format(value, 'g')}"
            for key, value in self.statistics
        )
        summary = f"{label}: {measurements}" if measurements else label
        affected = ", ".join(self.affected_mutation_ids) or "—"
        return (
            f"- kind={self.kind} severity={self.severity}\n"
            f"  summary: {summary}\n"
            f"  affected_mutation_ids: {affected}"
        )
