"""Main entry point for the decision-telemetry analyzer.

The analyzer's job for one epoch:

1. Walk the workspace's ``epochs/{epoch}/generations/{*}/runs/{*}/events.jsonl``
   tree and collect every events file the epoch has accumulated.
2. Aggregate the five decision-telemetry event types into a
   :class:`zicato.analyzer.aggregator.DecisionEventSummary`.
3. Render the system + user prompts.
4. Call the auxiliary LLM with a bounded per-call timeout
   (:func:`zicato.aux_timeout.aux_call_timeout_s`).
5. Persist the LLM's markdown response as
   ``.zicato/epochs/{epoch}/insights/round_{N}.md`` (or
   ``insights/latest.md`` when ``round_n is None``).

Every failure mode (no events at all, LLM timeout, LLM error) is
handled by writing a short markdown placeholder rather than raising —
the orchestrator calls this best-effort and a wedge here must not
abort the round.

A sibling :func:`load_latest_insights` helper reads every
``insights/*.md`` file in lexicographic order and concatenates the
contents so the proposer can splice them into its user prompt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from zicato.analyzer.aggregator import (
    DecisionEventSummary,
    aggregate_decision_events,
)
from zicato.analyzer.prompts import (
    INSIGHT_SYSTEM_PROMPT,
    render_insight_user_prompt,
)
from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.workspace import epoch_dir


def _collect_events_jsonl_paths(workspace_root: Path, epoch_id: str) -> list[Path]:
    """Walk the epoch's generation tree and return every ``events.jsonl`` path.

    The walk is filesystem-driven (rather than reading the board) so
    every generation's runs surface — including rejected ones, whose
    telemetry is still useful for analysis. Returns an empty list when
    the epoch directory does not exist or carries no events files yet
    (e.g. a freshly-created epoch with no completed rounds).
    """

    root = epoch_dir(workspace_root, epoch_id) / "generations"
    if not root.exists():
        return []
    out: list[Path] = []
    for path in sorted(root.glob("*/runs/*/events.jsonl")):
        if path.is_file():
            out.append(path)
    return out


def _insights_dir(workspace_root: Path, epoch_id: str) -> Path:
    return epoch_dir(workspace_root, epoch_id) / "insights"


def _insight_target(workspace_root: Path, epoch_id: str, round_n: int | None) -> Path:
    out_dir = _insights_dir(workspace_root, epoch_id)
    if round_n is None:
        return out_dir / "latest.md"
    # Zero-pad to width 4 so lexicographic ordering matches numeric
    # ordering up to 10k rounds — well beyond any plausible operator
    # workflow.
    return out_dir / f"round_{round_n:04d}.md"


def _empty_insight_body(epoch_id: str, summary: DecisionEventSummary) -> str:
    """Fallback insight body when no decision telemetry was observed.

    Returned ahead of any LLM call so the caller doesn't burn an
    aux-LLM budget on a prompt the model has nothing to say about.
    """

    return (
        f"# Decision telemetry insights — epoch {epoch_id}\n\n"
        f"No decision-telemetry events were observed in this epoch's "
        f"runs (total_events_seen={summary.total_events_seen}). This is "
        "expected for goldfive builds that pre-date the decision-"
        "telemetry events (tags 39-43) or for epochs whose runs have "
        "not yet executed against the new build.\n\n"
        "_No actionable patterns to surface._\n"
    )


def _error_insight_body(epoch_id: str, err: str) -> str:
    """Fallback insight body when the LLM call fails / times out.

    The orchestrator treats this as best-effort, so we still write a
    file (the proposer can ignore an empty actionable section). The
    error string surfaces in the insight so an operator inspecting the
    workspace can see what went wrong.
    """

    return (
        f"# Decision telemetry insights — epoch {epoch_id}\n\n"
        f"_(auxiliary LLM call failed: {err}; no insights generated for "
        "this round)_\n"
    )


async def analyze_epoch_telemetry(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]],
    model: str = "",
    round_n: int | None = None,
) -> Path:
    """Build the decision-event summary, call the LLM, persist the insight.

    Parameters
    ----------
    workspace_root:
        Absolute path to the ``.zicato/`` workspace root.
    epoch_id:
        The epoch whose accumulated telemetry should be analyzed.
    aux_call_llm:
        The auxiliary LLM callable (see
        :class:`zicato.core.types.RuntimeConfig`). Wrapped in
        :func:`asyncio.wait_for` against
        :func:`zicato.aux_timeout.aux_call_timeout_s`.
    model:
        Optional model identifier forwarded verbatim to *aux_call_llm*.
        Free-form; the analyzer does not switch on its value.
    round_n:
        Round number for the output filename. When ``None`` the insight
        is written to ``insights/latest.md`` instead of
        ``insights/round_{N}.md``.

    Returns
    -------
    Path
        Absolute path of the markdown file that was written. Always
        written — even when no telemetry was observed or the LLM call
        failed — so the orchestrator's best-effort caller has something
        deterministic to inspect.

    Notes
    -----
    This function does not raise on LLM failures or empty telemetry.
    Internal failures (path math, disk write) DO raise, which is the
    right behaviour for the orchestrator's ``try / except`` wrapper.
    """

    events_paths = _collect_events_jsonl_paths(workspace_root, epoch_id)
    summary = aggregate_decision_events(events_paths)

    target = _insight_target(workspace_root, epoch_id, round_n)
    target.parent.mkdir(parents=True, exist_ok=True)

    if summary.total_events_seen == 0:
        target.write_text(_empty_insight_body(epoch_id, summary), encoding="utf-8")
        return target

    user_prompt = render_insight_user_prompt(summary, epoch_id)
    try:
        response = await asyncio.wait_for(
            aux_call_llm(INSIGHT_SYSTEM_PROMPT, user_prompt, model),
            timeout=aux_call_timeout_s(),
        )
    except TimeoutError:
        target.write_text(
            _error_insight_body(
                epoch_id,
                f"timeout after {aux_call_timeout_s():.1f}s",
            ),
            encoding="utf-8",
        )
        return target
    except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
        target.write_text(
            _error_insight_body(
                epoch_id,
                f"{type(exc).__name__}: {exc}",
            ),
            encoding="utf-8",
        )
        return target

    # The LLM body is written verbatim. The system prompt already
    # constrains it to a markdown shape; we don't second-guess by
    # post-processing.
    body = response.strip() + "\n" if response else _empty_insight_body(epoch_id, summary)
    target.write_text(body, encoding="utf-8")
    return target


def load_latest_insights(workspace_root: Path, epoch_id: str) -> str:
    """Concatenate every ``insights/*.md`` file under the epoch in order.

    Reads ``round_{N}.md`` files in lexicographic order (which matches
    numeric ordering because of the zero-pad), followed by any
    ``latest.md`` written when an analyzer ran with ``round_n=None``.
    The concatenation joins files with a blank line so the
    proposer-side embedding renders cleanly.

    Returns the empty string when the insights directory does not
    exist or carries no readable markdown files. Empty string is the
    proposer-side sentinel for "no insights to embed".
    """

    insights_root = _insights_dir(workspace_root, epoch_id)
    if not insights_root.exists():
        return ""

    files = sorted(p for p in insights_root.glob("*.md") if p.is_file())
    if not files:
        return ""

    bodies: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            bodies.append(text)
    if not bodies:
        return ""
    return "\n\n".join(bodies) + "\n"


__all__ = [
    "analyze_epoch_telemetry",
    "load_latest_insights",
]
