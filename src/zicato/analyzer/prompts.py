"""System + user prompt templates for the decision-telemetry analyzer.

The analyzer prompt has two halves:

* :data:`INSIGHT_SYSTEM_PROMPT` — operator-tone scaffolding describing
  what the LLM is looking at and the actionable-pattern output shape.
* :data:`INSIGHT_USER_TEMPLATE` — per-epoch payload: ladder, dispatch,
  policy, retry, and per-detector aggregates rendered as markdown.

Rendering helpers turn a :class:`zicato.analyzer.aggregator.DecisionEventSummary`
into the markdown blocks the user template splices in.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from zicato.analyzer.aggregator import DecisionEventSummary

INSIGHT_SYSTEM_PROMPT = """\
You are analyzing decision telemetry from an automated agent steering
system. You receive structured counts of:
- Intervention ladder transitions (from level -> to level + reasons)
- Detector dispatch orderings
- Policy outcomes (which steerer policies fired and what they decided)
- Refine attempt budgets (how often the planner had to retry)
- Per-detector verdicts (drift fired / no-drift / suppressed)

Your job: identify ACTIONABLE patterns the operator should know about.
For each pattern:
- Be specific (cite the counts).
- Suggest WHAT to change (a prompt? a threshold?) and WHERE.
- When you reference a mutation target, you MUST use a mutation id
  drawn VERBATIM from the "Available mutation targets" list in the
  payload below. Do NOT invent, guess, or paraphrase an id. If no
  listed id fits the change you are suggesting, describe the change in
  plain prose and say explicitly that no enumerated mutation target
  covers it — never fabricate an id.

Produce a markdown document with sections:
## Headline observations
## Suspected over-intervention
## Suspected under-intervention
## Suggested next mutations

Keep each section to 3-6 bullets. Be concrete. Cite numbers.
"""


INSIGHT_USER_TEMPLATE = """\
## Decision telemetry for epoch {epoch_id}

### Ladder transitions
{ladder_transitions_md}

### Dispatch orderings
{dispatch_orderings_md}

### Policy outcomes
{policy_outcomes_md}

### Retry budgets
{retry_budgets_md}

### Per-detector verdicts
{steering_decisions_md}

### Available mutation targets
{mutation_targets_md}

Total decision events observed: {total_events}
"""


def _none_block(message: str) -> str:
    """Single-line "no data" marker used when an aggregate is empty.

    The LLM gets an explicit signal rather than a blank section so it
    doesn't hallucinate counts that aren't there.
    """

    return f"_(none observed)_  \n{message}".strip() if False else "_(none observed)_"


def _render_ladder_transitions(summary: DecisionEventSummary) -> str:
    if not summary.ladder_transitions and not summary.ladder_reasons:
        return _none_block("")
    lines: list[str] = []
    if summary.ladder_transitions:
        lines.append("Transitions (from -> to):")
        for key, count in sorted(
            summary.ladder_transitions.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"- `{key}` x {count}")
    if summary.ladder_reasons:
        lines.append("")
        lines.append("Reasons cited:")
        for reason, count in sorted(summary.ladder_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- `{reason}` x {count}")
    return "\n".join(lines)


def _render_dispatch_orderings(summary: DecisionEventSummary) -> str:
    if not summary.dispatch_orders:
        return _none_block("")
    # Multiple sessions usually share an identical ordering; count
    # frequency so the markdown stays compact.
    counter: Counter[tuple[str, ...]] = Counter(summary.dispatch_orders)
    lines: list[str] = []
    for order, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
        rendered = " -> ".join(order) if order else "(empty)"
        lines.append(f"- `{rendered}` x {count}")
    return "\n".join(lines)


def _render_policy_outcomes(summary: DecisionEventSummary) -> str:
    if not summary.policy_outcomes:
        return _none_block("")
    lines: list[str] = []
    for policy_name, outcomes in sorted(summary.policy_outcomes.items()):
        total = sum(outcomes.values())
        lines.append(f"- **{policy_name}** ({total} total)")
        for outcome, count in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = (count / total * 100) if total else 0.0
            lines.append(f"  - `{outcome}` x {count} ({pct:.1f}%)")
    return "\n".join(lines)


def _render_retry_budgets(summary: DecisionEventSummary) -> str:
    if not summary.retry_attempts:
        return _none_block("")
    lines: list[str] = []
    for operation, attempts in sorted(summary.retry_attempts.items()):
        if not attempts:
            continue
        total = len(attempts)
        max_attempt = max(attempts)
        # Number of times the budget was fully spent (final attempt
        # >= 2 by convention; the proto guarantees 1-indexed attempts
        # and the steerer's default budget is 2). We surface the
        # raw distribution rather than collapsing because the LLM is
        # better positioned to interpret thresholds than we are.
        dist = Counter(attempts)
        dist_str = ", ".join(f"attempt={a}: {c}" for a, c in sorted(dist.items()))
        lines.append(
            f"- **{operation}** ({total} attempts, max attempt #{max_attempt}; {dist_str})"
        )
    return "\n".join(lines)


def _render_steering_decisions(summary: DecisionEventSummary) -> str:
    if not summary.steering_decisions:
        return _none_block("")
    lines: list[str] = []
    for detector_name, outcomes in sorted(summary.steering_decisions.items()):
        total = sum(outcomes.values())
        lines.append(f"- **{detector_name}** ({total} verdicts)")
        for outcome, count in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = (count / total * 100) if total else 0.0
            lines.append(f"  - `{outcome}` x {count} ({pct:.1f}%)")
    return "\n".join(lines)


def _render_mutation_targets(mutation_ids: Sequence[str] | None) -> str:
    """Render the enumerated mutation-target ids as a markdown bullet list.

    This block GROUNDS the insight: the system prompt instructs the LLM
    to reference only ids that appear here, so it cannot hallucinate a
    mutation target that is absent from the agent's real mutation surface.
    When the caller passes no ids (the analyzer was not handed the
    enumerated surface), a "none provided" marker is
    rendered and the system prompt's fallback rule applies — the LLM
    must describe the change in prose rather than invent an id.
    """
    cleaned = [str(mid).strip() for mid in (mutation_ids or []) if str(mid).strip()]
    if not cleaned:
        return _none_block("no enumerated mutation surface was provided")
    # De-duplicate while preserving first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for mid in cleaned:
        if mid not in seen:
            seen.add(mid)
            ordered.append(mid)
    lines = ["Reference ONLY these ids when suggesting a mutation (verbatim — do not invent ids):"]
    lines.extend(f"- `{mid}`" for mid in ordered)
    return "\n".join(lines)


def render_insight_user_prompt(
    summary: DecisionEventSummary,
    epoch_id: str,
    mutation_ids: Sequence[str] | None = None,
) -> str:
    """Assemble the full user prompt body from a summary + epoch id.

    Sections render their own "none observed" notice when the
    aggregate is empty so the LLM sees explicit zeros rather than a
    blank section it might hallucinate against.

    ``mutation_ids`` is the agent's real enumerated mutation surface
    (the :attr:`zicato.core.types.MutationPoint.id` values). It is
    rendered into the prompt so the LLM's "Suggested next mutations"
    section is grounded in ids that actually exist — the system prompt
    forbids referencing any id absent from this list. When ``None`` or
    empty, the prompt still renders (with a "none provided" marker) so
    the analyzer degrades gracefully for callers that cannot supply the
    surface.
    """

    return INSIGHT_USER_TEMPLATE.format(
        epoch_id=epoch_id,
        ladder_transitions_md=_render_ladder_transitions(summary),
        dispatch_orderings_md=_render_dispatch_orderings(summary),
        policy_outcomes_md=_render_policy_outcomes(summary),
        retry_budgets_md=_render_retry_budgets(summary),
        steering_decisions_md=_render_steering_decisions(summary),
        mutation_targets_md=_render_mutation_targets(mutation_ids),
        total_events=summary.total_events_seen,
    )


__all__ = [
    "INSIGHT_SYSTEM_PROMPT",
    "INSIGHT_USER_TEMPLATE",
    "render_insight_user_prompt",
]
