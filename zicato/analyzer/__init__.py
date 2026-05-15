"""Decision-telemetry analyzer.

The analyzer reads goldfive's decision-telemetry events from the
``events.jsonl`` files an epoch has accumulated and produces an
LLM-generated insight summary. The output is persisted under
``epochs/{epoch}/insights/`` and is read back by the proposer the next
round so the orchestrator's evolve loop closes a feedback loop between
goldfive's silent-decision telemetry and the proposer's next move.

Public surface:

* :class:`DecisionEventSummary` — frozen dataclass holding the
  aggregated counts the analyzer ships to the LLM.
* :func:`aggregate_decision_events` — JSONL replay + count aggregation.
* :func:`analyze_epoch_telemetry` — main entry point. Builds the
  summary, renders the analysis prompt, calls the auxiliary LLM with a
  bounded timeout, writes the resulting markdown to
  ``insights/round_{N}.md``.
* :func:`load_latest_insights` — concatenate every insights file in
  chronological order for embedding in the proposer prompt.
"""

from __future__ import annotations

from zicato.analyzer.aggregator import (
    DecisionEventSummary,
    aggregate_decision_events,
)
from zicato.analyzer.insights import analyze_epoch_telemetry, load_latest_insights

__all__ = [
    "DecisionEventSummary",
    "aggregate_decision_events",
    "analyze_epoch_telemetry",
    "load_latest_insights",
]
