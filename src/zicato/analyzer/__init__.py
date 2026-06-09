"""Decision-telemetry analyzer and the epoch analysis report.

The analyzer has two outputs, both regenerated as the evolve loop runs:

* **Per-round insights** — the analyzer reads goldfive's
  decision-telemetry events from the ``events.jsonl`` files an epoch has
  accumulated and produces an LLM-generated insight summary, persisted
  under ``epochs/{epoch}/insights/round_{N}.md`` and read back by the
  proposer the next round. This closes a feedback loop between
  goldfive's silent-decision telemetry and the proposer's next move.

* **The epoch analysis report** — a comprehensive, academic-paper-style
  narrative of the whole improvement campaign, regenerated after every
  generation and persisted as ``epochs/{epoch}/analysis.md`` plus a
  rendered ``analysis.html``. Its data-bearing sections are templated
  exactly from the structured workspace; its prose sections are written
  by one bounded auxiliary-LLM call.

Public surface:

* :class:`DecisionEventSummary` — frozen dataclass holding the
  aggregated counts the analyzer ships to the LLM.
* :func:`aggregate_decision_events` — JSONL replay + count aggregation.
* :func:`analyze_epoch_telemetry` — per-round insights entry point.
* :func:`load_latest_insights` — concatenate every insights file in
  chronological order for embedding in the proposer prompt.
* :func:`generate_epoch_report` — regenerate the comprehensive epoch
  analysis report (``analysis.md`` + ``analysis.html``).
* :class:`EpochReportData` / :func:`gather_epoch_report_data` — the
  deterministic structured view the report is templated from.
"""

from __future__ import annotations

from zicato.analyzer.aggregator import (
    DecisionEventSummary,
    aggregate_decision_events,
)
from zicato.analyzer.insights import analyze_epoch_telemetry, load_latest_insights
from zicato.analyzer.outcome_marginals import (
    OutcomeMarginalSummary,
    aggregate_outcome_marginals,
    run_operator_summarizer,
    sanitize_operator_marginals,
)
from zicato.analyzer.report import (
    generate_epoch_report,
    restamp_persisted_report,
)
from zicato.analyzer.report_data import EpochReportData, gather_epoch_report_data

__all__ = [
    "DecisionEventSummary",
    "aggregate_decision_events",
    "analyze_epoch_telemetry",
    "load_latest_insights",
    "OutcomeMarginalSummary",
    "aggregate_outcome_marginals",
    "run_operator_summarizer",
    "sanitize_operator_marginals",
    "generate_epoch_report",
    "restamp_persisted_report",
    "EpochReportData",
    "gather_epoch_report_data",
]
