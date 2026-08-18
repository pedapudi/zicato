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
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

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
from zicato.workspace import is_events_file

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.telemetry.meta_loop import MetaLoopEmitter


def _collect_events_jsonl_paths(workspace_root: Path, epoch_id: str) -> list[Path]:
    """Walk the epoch's generation tree and return every current events path.

    The walk is filesystem-driven (rather than reading the board) so
    every generation's runs surface — including rejected ones, whose
    telemetry is still useful for analysis. Returns an empty list when
    the epoch directory does not exist or carries no events files yet
    (e.g. a freshly-created epoch with no completed rounds).

    EVERY replicate of a unit is collected, not just replicate 0, and the
    insight prompt therefore aggregates across replicate bands: a unit run
    at ``replicates=3`` contributes three transcripts of the same board
    entry. That is deliberate for a whole-epoch drift summary — but it
    means a per-entry count read off this list counts draws, not units.
    Archived predecessors (``*.prev.jsonl``) are excluded.
    """

    root = epoch_dir(workspace_root, epoch_id) / "generations"
    if not root.exists():
        return []
    out: list[Path] = []
    for path in sorted(root.glob("*/runs/*/events*.jsonl")):
        if path.is_file() and is_events_file(path):
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
    mutation_ids: Sequence[str] | None = None,
    meta_loop_emitter: MetaLoopEmitter | None = None,
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
    mutation_ids:
        The agent's real enumerated mutation-surface ids (the
        :attr:`zicato.core.types.MutationPoint.id` values for the
        epoch's current generation). Threaded into the insight prompt
        so the LLM's "Suggested next mutations" section is grounded in
        ids that actually exist — without it, the LLM hallucinated
        mutation target ids absent from the agent's surface. When
        ``None`` the prompt still renders, with a "none provided"
        marker, and the system prompt forbids inventing an id.

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

    user_prompt = render_insight_user_prompt(summary, epoch_id, mutation_ids)
    # The decision-telemetry analyzer is the meta-loop's "process judge"
    # — it surveys the round's telemetry and emits a verdict (the
    # insight markdown). Bracket the LLM call with a paired
    # ``judge_invoked`` / ``judgment_emitted`` envelope so the dashboard
    # / harmonograf timeline shows the analyzer as a judge alongside
    # the proposer. Every emit is best-effort and isolated from a
    # misconfigured emitter.
    invocation_id: str | None = None
    judge_name = "decision_telemetry_analyzer"
    started_at = time.monotonic()
    if meta_loop_emitter is not None:
        try:
            invocation_id = await meta_loop_emitter.judge_invoked(
                judge_name=judge_name,
                kind="process",
            )
        except Exception:  # noqa: BLE001 — additive telemetry only
            invocation_id = None
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
        if meta_loop_emitter is not None and invocation_id is not None:
            try:
                await meta_loop_emitter.judgment_emitted(
                    invocation_id=invocation_id,
                    judge_name=judge_name,
                    verdict_kind="boolean",
                    score=None,
                    detail=f"timeout after {aux_call_timeout_s():.1f}s",
                    latency_s=time.monotonic() - started_at,
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                pass
        return target
    except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
        target.write_text(
            _error_insight_body(
                epoch_id,
                f"{type(exc).__name__}: {exc}",
            ),
            encoding="utf-8",
        )
        if meta_loop_emitter is not None and invocation_id is not None:
            try:
                await meta_loop_emitter.judgment_emitted(
                    invocation_id=invocation_id,
                    judge_name=judge_name,
                    verdict_kind="boolean",
                    score=None,
                    detail=f"{type(exc).__name__}: {exc}",
                    latency_s=time.monotonic() - started_at,
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                pass
        return target

    if meta_loop_emitter is not None and invocation_id is not None:
        try:
            await meta_loop_emitter.judgment_emitted(
                invocation_id=invocation_id,
                judge_name=judge_name,
                verdict_kind="rubric",
                # No structured score available from a markdown insight —
                # the dashboard treats ``None`` as "narrative judgement
                # only" and renders the detail field instead.
                score=None,
                detail=f"insight written ({len(response or '')} chars)",
                latency_s=time.monotonic() - started_at,
            )
        except Exception:  # noqa: BLE001 — additive telemetry only
            pass

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
