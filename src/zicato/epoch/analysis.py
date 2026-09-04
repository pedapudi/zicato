"""At-epoch-close analysis pass.

Generates ``analysis.md`` for an epoch by handing the auxiliary LLM:

* the running ``journal.md``,
* every ``experiment.json`` written under the epoch's ``generations/``,
* an optional patterns snapshot if one is available on disk,
* a pre-rendered "Tournament outcomes" section computed deterministically
  from the journal data (lineage graph, trajectory table, ASCII
  sparkline, drift-kind movement table).

The LLM is asked to produce a fixed-structure markdown document with the
sections enumerated in :data:`REQUIRED_SECTIONS`, referencing the
pre-rendered diagrams without re-emitting them. We do NOT parse or
re-validate the LLM result beyond writing it through; downstream tooling
that wants structure should read from ``experiment.json`` and
``journal.md`` directly.

The sibling ``analysis.html`` is not rendered here: :func:`write_html_companion`
hands the markdown to :func:`zicato.analyzer.report.render_report_html`, the one
renderer of the served document, so the HTML always matches the markdown beside
it.

The pass is **bounded**: we cap the journal slice and per-experiment detail we
inline into the prompt so the call is predictable. Operators who need a fuller
retrospective can re-run the pass with a larger budget by setting environment
knobs; the function takes a ``model`` argument so a caller can select one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.types import (
    Experiment,
    Generation,
)
from zicato.core.workspace import (
    analysis_path,
    epoch_dir,
    journal_path,
)
from zicato.epoch.journal import experiment_body, read_epoch_experiments
from zicato.epoch.lineage import load_lineage
from zicato.workspace import ScalarStep, WorkspaceLayout, cumulative_scalars, generation_ids

# A goldfive-compatible auxiliary call_llm.
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

REQUIRED_SECTIONS: tuple[str, ...] = (
    "Headline movements",
    "Hypotheses that held",
    "Hypotheses that didn't",
    "Surface still open at epoch close",
    "Recommended focus for next epoch",
)


# Soft caps so the prompt size stays predictable. These are intentionally
# generous — operators will overflow them only on multi-week epochs.
_MAX_JOURNAL_CHARS = 60_000
_MAX_PATTERNS_CHARS = 20_000
_MAX_EXPERIMENT_CHARS = 4_000
_MAX_EXPERIMENTS_INLINE = 50

# Maximum drift-kind rows in the movement table — keeps the section
# readable on a typical terminal.
_DRIFT_KIND_TABLE_LIMIT = 12

# Max characters of a rejection reason that we render inline on a
# mermaid edge label. Longer reasons get an ellipsis suffix.
#
# Gate reasons lead with the rule that fired and follow it with that
# rule's numbers, so 30 characters clipped every edge in the graph to the
# rule name alone — "insufficient improvement: los…" on every rejected
# branch, which is the one thing the arrow's own style already says
# (issue #129). 60 reaches the first measured quantity while still fitting
# a node label at normal zoom.
_MERMAID_EDGE_LABEL_LIMIT = 60


_SYSTEM_PROMPT = """\
You are an expert reviewer summarizing one epoch of an automated agent
optimization loop. You will receive:

  * the running narrative journal for the epoch,
  * a structured list of every experiment that ran (hypothesis + outcome),
  * optionally, a patterns snapshot summarising drift observations,
  * a pre-rendered "Tournament outcomes" section (mermaid lineage graph,
    trajectory table, ASCII sparkline, drift-kind movement table). These
    diagrams are computed from the journal data and are factually
    authoritative. Reference them in your narrative; do NOT reproduce or
    modify them.

Your job is to write the narrative half of `analysis.md` — a retrospective
the operator will read between epochs. Be specific. Cite generation ids
when relevant. Prefer concrete observations over generalities.

You MUST produce exactly these sections, in this order, as markdown
level-2 headings:

  ## Headline movements
  ## Hypotheses that held
  ## Hypotheses that didn't
  ## Surface still open at epoch close
  ## Recommended focus for next epoch

Do NOT emit a level-1 heading or the "## Tournament outcomes" section —
the caller prepends those. Do not invent sections that are not in the
list above; do not omit any of them. Each section may be a short
paragraph or bullet list — whichever fits the material.
"""


def _slice(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... [truncated for analysis pass]"


def _collect_patterns_snapshot(workspace_root: Path, epoch_id: str) -> str:
    """Aggregate ``patterns/round_*.json`` files into a single text blob.

    Returns the empty string when there is no patterns directory, which is
    the common case: nothing writes pattern files yet.
    """
    patterns_dir = epoch_dir(workspace_root, epoch_id) / "patterns"
    if not patterns_dir.exists():
        return ""
    parts: list[str] = []
    for path in sorted(patterns_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        parts.append(f"### {path.stem}\n{json.dumps(d, indent=2, sort_keys=True)}")
    return "\n\n".join(parts)


def _format_experiment(experiment: Experiment) -> str:
    """Compact one experiment down to its journal-relevant fields.

    The prompt slice is a projection, so it is built from the typed record
    through the record's own encoder rather than by re-opening the file —
    the same bytes the writer put on disk, minus the fields the narrative
    has no use for.
    """
    body = experiment_body(experiment)
    keep = {
        k: body[k]
        for k in (
            "id",
            "generation_id",
            "parent_generation_id",
            "proposed_at",
            "hypothesis",
            "outcome",
        )
    }
    return _slice(json.dumps(keep, indent=2, sort_keys=True), _MAX_EXPERIMENT_CHARS)


# ---------------------------------------------------------------------------
# Deterministic tournament-outcomes diagrams
# ---------------------------------------------------------------------------


#: Characters that terminate a flowchart token wherever they appear, so
#: mermaid rejects the whole ``graph`` block when one lands inside a label.
#: Node labels are emitted inside double quotes and survive these; EDGE
#: labels are emitted bare between pipes (``a -.->|text| b``) and do not:
#: one ``(`` fails the parse for the entire diagram rather than only its edge.
_MERMAID_LABEL_ENTITIES = {
    "&": "&amp;",
    '"': "&quot;",
    "<": "&lt;",
    ">": "&gt;",
    "(": "&#40;",
    ")": "&#41;",
    "[": "&#91;",
    "]": "&#93;",
    "{": "&#123;",
    "}": "&#125;",
    "|": "&#124;",
    "\n": " ",
    "\r": " ",
}


def _sanitize_label(text: str) -> str:
    """Strip mermaid-hostile characters from a node/edge label.

    Replaces each with an HTML entity so the rendered label preserves the
    original glyph while staying syntactically valid, the same way the
    quote and angle-bracket escapes do.

    The bracket family matters because gate reasons carry their measured
    numbers in parentheses (``"insufficient improvement: loss fell by only
    0.000200 (champion … -> challenger …)"``). An edge-label clip short
    enough to cut the parenthetical off would hide the hazard;
    :data:`_MERMAID_EDGE_LABEL_LIMIT` at 60 reaches it, and an unescaped
    ``(`` makes the whole lineage diagram unparseable.
    """
    return "".join(_MERMAID_LABEL_ENTITIES.get(ch, ch) for ch in text)


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis suffix when truncated."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _decision_marker(decision: str | None, is_baseline: bool) -> str:
    """Return the inline glyph for a tournament decision."""
    if is_baseline:
        return ""
    if decision == "promoted":
        return "✓"
    if decision == "rejected":
        return "✗"
    if decision == "deferred":
        return "~"
    return "?"


def _decision_word(decision: str | None, is_baseline: bool) -> str:
    """Return the upper-cased decision word used in the sparkline tail."""
    if is_baseline:
        return "baseline"
    if decision is None:
        return "pending"
    return decision.upper()


def _node_class(decision: str | None, is_baseline: bool) -> str:
    """Map a generation's decision into a mermaid class name."""
    if is_baseline:
        return "baseline"
    if decision == "promoted":
        return "promoted"
    if decision == "rejected":
        return "rejected"
    return "pending"


def _gen_by_id(generations: Sequence[Generation]) -> dict[str, Generation]:
    return {g.id: g for g in generations}


def _exp_by_child(experiments: Sequence[Experiment]) -> dict[str, Experiment]:
    """Index experiments by the (child) ``generation_id`` they describe."""
    return {e.generation_id: e for e in experiments}


def _scalar_trajectory(
    generations: Sequence[Generation],
    experiments: Sequence[Experiment],
    baseline: float = 0.0,
) -> dict[str, float]:
    """Cumulate per-generation scalar scores from a baseline.

    The on-disk outcome record carries the *delta* in scalar score from
    the parent generation; an absolute score is not persisted. We seed
    the baseline (``v0`` or the earliest generation) at ``baseline`` and
    add successive ``scalar_score_delta`` values to produce a deterministic
    per-generation scalar suitable for plotting.

    Generations without an outcome inherit the parent's scalar (or the
    baseline if the parent is unknown). The returned mapping keys on
    generation id.

    The cumulation itself is the shared canonical one
    (:func:`zicato.workspace.cumulative_scalars`); this function supplies the
    typed lineage's answers to which generation seeds the lineage and what
    delta each one recorded.
    """
    exp_idx = _exp_by_child(experiments)

    def _delta(gen: Generation) -> float:
        exp = exp_idx.get(gen.id)
        if exp is None or exp.outcome is None:
            return 0.0
        return exp.outcome.scalar_score_delta

    return dict(
        cumulative_scalars(
            (
                ScalarStep(
                    generation_id=g.id,
                    parent_generation_id=g.parent_id or "",
                    is_baseline=g.parent_id is None,
                    scalar_score_delta=_delta(g),
                )
                for g in generations
            ),
            baseline=baseline,
        )
    )


def render_mermaid_lineage(
    generations: list[Generation],
    experiments: list[Experiment],
) -> str:
    """Render the lineage as a mermaid ``graph LR`` block.

    Nodes are labelled with the generation id, a decision marker
    (``✓`` promoted, ``✗`` rejected, neutral for the baseline / pending),
    the scalar-score delta from the parent, and the cumulated scalar
    score. Edges are labelled by the tournament outcome's rejection
    reason when present (clipped to :data:`_MERMAID_EDGE_LABEL_LIMIT`
    characters) or by the decision word otherwise; promoted edges use
    ``==>`` (thick) and rejected edges use ``-.->`` (dashed).

    An empty ``generations`` list yields a placeholder block so the
    surrounding section still has a valid mermaid container.
    """
    if not generations:
        return '```mermaid\ngraph LR\n    empty["(no generations)"]\n```'

    exp_idx = _exp_by_child(experiments)
    scores = _scalar_trajectory(generations, experiments)

    lines: list[str] = ["```mermaid", "graph LR"]
    # Node declarations.
    node_classes: dict[str, str] = {}
    for g in generations:
        is_baseline = g.parent_id is None
        exp = exp_idx.get(g.id)
        decision = exp.outcome.tournament_decision if exp and exp.outcome else None
        marker = _decision_marker(decision, is_baseline)

        label_parts: list[str] = []
        head = g.id if not marker else f"{g.id} {marker}"
        label_parts.append(_sanitize_label(head))
        if is_baseline:
            label_parts.append("baseline")
        elif exp is not None and exp.outcome is not None:
            label_parts.append(_sanitize_label(f"Δ {exp.outcome.scalar_score_delta:+.3f}"))
        else:
            label_parts.append("pending")
        label_parts.append(_sanitize_label(f"scalar {scores[g.id]:+.3f}"))

        label = "<br/>".join(label_parts)
        lines.append(f'    {g.id}["{label}"]')
        node_classes[g.id] = _node_class(decision, is_baseline)

    # Edges.
    for g in generations:
        if g.parent_id is None:
            continue
        if g.parent_id not in node_classes:
            # Parent not in the rendered set; skip the edge rather than
            # emit a dangling reference that mermaid will reject.
            continue
        exp = exp_idx.get(g.id)
        decision = exp.outcome.tournament_decision if exp and exp.outcome else None
        if decision == "promoted":
            arrow = "==>"
            edge_label = "promoted"
        elif decision == "rejected":
            arrow = "-.->"
            reason = exp.outcome.rejection_reason if exp and exp.outcome else ""
            edge_label = reason or "rejected"
            edge_label = _truncate(edge_label, _MERMAID_EDGE_LABEL_LIMIT)
        elif decision == "deferred":
            arrow = "-->"
            edge_label = "deferred"
        else:
            arrow = "-->"
            edge_label = "pending"
        edge_label = _sanitize_label(edge_label)
        lines.append(f"    {g.parent_id} {arrow}|{edge_label}| {g.id}")

    # Style classes.
    lines.append("    classDef promoted fill:#d4edda,stroke:#155724")
    lines.append("    classDef rejected fill:#f8d7da,stroke:#721c24")
    lines.append("    classDef baseline fill:#e7e7e7,stroke:#444")
    lines.append("    classDef pending fill:#fff3cd,stroke:#856404")

    # Assign classes — group by class to keep the output compact.
    by_class: dict[str, list[str]] = {}
    for gid, cls in node_classes.items():
        by_class.setdefault(cls, []).append(gid)
    for cls in ("promoted", "rejected", "baseline", "pending"):
        members = by_class.get(cls)
        if members:
            lines.append(f"    class {','.join(members)} {cls}")

    lines.append("```")
    return "\n".join(lines)


def render_trajectory_table(
    generations: list[Generation],
    experiments: list[Experiment],
) -> str:
    """Render a markdown table summarising the scalar trajectory.

    Columns: generation id, cumulated scalar score, ``Δ`` from parent,
    tournament decision, and the proposer's one-line ``core_idea``.
    Generations that have not yet produced an outcome are marked
    "pending" in the decision column with an empty delta.
    """
    if not generations:
        return "_(no generations)_"

    exp_idx = _exp_by_child(experiments)
    scores = _scalar_trajectory(generations, experiments)

    lines: list[str] = []
    lines.append("| gen | score | Δ from parent | decision | core_idea |")
    lines.append("| --- | --- | --- | --- | --- |")
    for g in generations:
        is_baseline = g.parent_id is None
        exp = exp_idx.get(g.id)
        score = scores.get(g.id, 0.0)
        if is_baseline:
            delta_cell = "—"
            decision_cell = "baseline"
            core = ""
        else:
            if exp is None:
                delta_cell = ""
                decision_cell = "pending"
                core = ""
            elif exp.outcome is None:
                delta_cell = ""
                decision_cell = "pending"
                core = exp.hypothesis.core_idea.splitlines()[0].strip()
            else:
                delta_cell = f"{exp.outcome.scalar_score_delta:+.3f}"
                decision_cell = exp.outcome.tournament_decision
                core = exp.hypothesis.core_idea.splitlines()[0].strip()
        # Pipes and newlines break markdown tables — sanitize.
        core = core.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {g.id} | {score:+.3f} | {delta_cell} | {decision_cell} | {core} |")
    return "\n".join(lines)


def render_score_sparkline(
    generations: list[Generation],
    experiments: list[Experiment],
    width: int = 26,
) -> str:
    """Render an ASCII bar chart of the per-generation scalar trajectory.

    Each generation occupies one line; bars are ``width`` columns wide,
    filled with ``█`` and padded with ``░``. The numeric scalar, the
    signed delta (with an ``↑`` / ``↓`` arrow), and the tournament
    decision are appended to each line. The most-recent generation is
    annotated with ``← current``. The bar's range is normalised over
    ``[min(scores) * 0.9, max(scores) * 1.1]``; if all scores are equal
    every bar is half-filled (the range is degenerate).

    The whole block is wrapped in a fenced code block (no language) so
    the alignment is preserved.
    """
    if width <= 0:
        width = 1
    if not generations:
        return "```\n(no generations)\n```"

    exp_idx = _exp_by_child(experiments)
    scores = _scalar_trajectory(generations, experiments)

    values = [scores[g.id] for g in generations]
    lo = min(values) * 0.9 if min(values) != 0 else -0.1
    hi = max(values) * 1.1 if max(values) != 0 else 0.1
    if lo > 0:
        lo = min(values) * 0.9
    if hi < lo or hi == lo:
        # Degenerate range — widen by a small constant so we always
        # produce a meaningful bar.
        spread = max(abs(lo), abs(hi), 1.0) * 0.1
        lo -= spread
        hi += spread

    label_width = max(len(g.id) for g in generations)
    last_idx = len(generations) - 1

    body_lines: list[str] = []
    for i, g in enumerate(generations):
        is_baseline = g.parent_id is None
        exp = exp_idx.get(g.id)
        score = scores[g.id]
        ratio = (score - lo) / (hi - lo) if hi > lo else 0.5
        ratio = max(0.0, min(1.0, ratio))
        filled = int(round(ratio * width))
        bar = "█" * filled + "░" * (width - filled)

        if is_baseline:
            delta_str = "         "  # blank slot keeps columns aligned
        elif exp is None or exp.outcome is None:
            delta_str = "         "
        else:
            delta = exp.outcome.scalar_score_delta
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "·")
            delta_str = f"{arrow} {delta:+.3f}"

        decision = exp.outcome.tournament_decision if exp and exp.outcome else None
        word = _decision_word(decision, is_baseline)
        marker = _decision_marker(decision, is_baseline)
        if is_baseline:
            tail = f"   {word}"
        else:
            tail = f"   {marker} {word}".rstrip()
            if exp and exp.outcome and exp.outcome.rejection_reason:
                tail += f"  [{exp.outcome.rejection_reason}]"

        line = f"{g.id:<{label_width}}: {bar}  {score:+.3f}   {delta_str}{tail}"
        if i == last_idx:
            line = line.rstrip() + "   ← current"
        body_lines.append(line.rstrip())

    return "```\n" + "\n".join(body_lines) + "\n```"


def _promoted_chain(
    generations: Sequence[Generation],
    experiments: Sequence[Experiment],
) -> list[Generation]:
    """Return the baseline plus every promoted generation in lineage order.

    The lineage is linear by construction — ``v0`` is the seed and each
    subsequent promoted generation chains off the previous head. We
    discover the chain by walking ``Generation.parent_id`` rather than
    trusting the input ordering (which may interleave rejected branches).
    """
    by_id = _gen_by_id(generations)
    exp_idx = _exp_by_child(experiments)

    baselines = [g for g in generations if g.parent_id is None]
    if not baselines:
        return []
    head = baselines[0]
    chain: list[Generation] = [head]
    # Walk forward: each step picks the child generation whose experiment
    # was promoted off the current head.
    current_id = head.id
    visited: set[str] = {head.id}
    while True:
        next_gen: Generation | None = None
        for g in generations:
            if g.id in visited:
                continue
            if g.parent_id != current_id:
                continue
            exp = exp_idx.get(g.id)
            if exp is None or exp.outcome is None:
                continue
            if exp.outcome.tournament_decision != "promoted":
                continue
            next_gen = g
            break
        if next_gen is None:
            break
        chain.append(next_gen)
        visited.add(next_gen.id)
        current_id = next_gen.id
        # Defensive: bail if the chain has somehow grown to cover every
        # node already (cycle guard).
        if len(chain) > len(by_id):
            break
    return chain


def render_metric_movement_table(
    generations: list[Generation],
    experiments: list[Experiment],
    namespace_filter: str | None = None,
) -> str:
    """Render a per-metric rate table over the promoted lineage.

    Generalises :func:`render_drift_kind_movement_table` to any
    namespace. ``namespace_filter`` is a string prefix (e.g.
    ``"drift:"``, ``"cost:"``, ``"rubric:"``) that restricts the table
    to a single namespace; pass ``None`` to render every metric across
    every namespace.

    The table reports the parent (``from_value``) and child (``to_value``)
    values for each metric, stitched into a per-metric column sequence.
    Rows are sorted by ``abs(net_change)`` descending and capped at
    :data:`_DRIFT_KIND_TABLE_LIMIT`.

    For back-compat the column suffix is ``_rate`` and the row header
    is ``drift_kind`` when ``namespace_filter == "drift:"`` (matches
    the historical drift-only table verbatim); otherwise the columns
    use ``_value`` and the header is ``metric``. Each row's display
    name has the namespace prefix stripped when a single namespace
    filter is in effect.

    The hydration path reads both ``outcome.metric_movements`` (the
    generalised surface) AND ``outcome.drift_movements`` lifted under
    the ``"drift:"`` namespace; either populates the table so
    operators don't lose drift signal even when only the older field
    is filled in.

    Returns the empty string when no movements were recorded in the
    requested namespace.
    """
    chain = _promoted_chain(generations, experiments)
    if len(chain) < 2:
        return ""

    exp_idx = _exp_by_child(experiments)
    per_metric: dict[str, list[float | None]] = {}
    seen_any = False
    n_cols = len(chain)

    def _record(metric_name: str, step_idx: int, from_val: float, to_val: float) -> None:
        nonlocal seen_any
        if namespace_filter and not metric_name.startswith(namespace_filter):
            return
        seen_any = True
        seq = per_metric.setdefault(metric_name, [None] * n_cols)
        if seq[step_idx - 1] is None:
            seq[step_idx - 1] = from_val
        seq[step_idx] = to_val

    for step_idx, child in enumerate(chain[1:], start=1):
        exp = exp_idx.get(child.id)
        if exp is None or exp.outcome is None:
            continue
        for mv in exp.outcome.drift_movements:
            _record(f"drift:{mv.kind}", step_idx, mv.from_rate, mv.to_rate)
        for mm in exp.outcome.metric_movements:
            _record(mm.metric_name, step_idx, mm.from_value, mm.to_value)

    if not seen_any:
        return ""

    rows: list[tuple[str, list[float | None], float]] = []
    for name, seq in per_metric.items():
        first = next((v for v in seq if v is not None), None)
        last = next((v for v in reversed(seq) if v is not None), None)
        if first is None or last is None:
            net = 0.0
        else:
            net = last - first
        rows.append((name, seq, net))
    rows.sort(key=lambda r: (-abs(r[2]), r[0]))
    rows = rows[:_DRIFT_KIND_TABLE_LIMIT]

    # Column suffix: keep "_rate" for the drift namespace (back-compat)
    # so the existing snapshot expectations don't shift; "_value" for
    # any other namespace where "rate" would be a misnomer (cost,
    # latency, rubric, ...).
    col_suffix = "rate" if namespace_filter == "drift:" else "value"
    rate_cols = [f"{g.id}_{col_suffix}" for g in chain]
    if rate_cols:
        rate_cols[-1] = f"final_{col_suffix}"

    name_header = "drift_kind" if namespace_filter == "drift:" else "metric"
    header_cells = [name_header, "severity", *rate_cols, "net_change"]
    lines: list[str] = []
    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("| " + " | ".join("---" for _ in header_cells) + " |")

    def _fmt(v: float | None) -> str:
        if v is None:
            return ""
        return f"{v:.3f}"

    for name, seq, net in rows:
        if namespace_filter and name.startswith(namespace_filter):
            display = name[len(namespace_filter) :]
        else:
            display = name
        cells = [display, ""]
        cells.extend(_fmt(v) for v in seq)
        cells.append(f"{net:+.3f}")
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def render_drift_kind_movement_table(
    generations: list[Generation],
    experiments: list[Experiment],
) -> str:
    """Render a per-drift-kind rate table over the promoted lineage.

    Back-compat wrapper over :func:`render_metric_movement_table` with
    ``namespace_filter="drift:"``. Output header and row format match
    the pre-generalisation table verbatim (``drift_kind | severity |
    {gen}_rate | ... | final_rate | net_change``) so existing tests
    and consumers don't break.

    Returns the empty string when no drift movements were recorded.
    """
    return render_metric_movement_table(generations, experiments, namespace_filter="drift:")


# Figure markers naming a builder in :mod:`zicato.analyzer.report_figures`.
# They are invisible in the markdown and substituted for the inline SVG when
# the document is rendered to HTML (:func:`write_html_companion`), so each
# table in this section is accompanied by the chart drawn from the same data.
_FIGURE_LINEAGE = "<!-- FIGURE:lineage -->"
_FIGURE_SCORE_TRAJECTORY = "<!-- FIGURE:score-trajectory -->"
_FIGURE_DRIFT_MOVEMENTS = "<!-- FIGURE:drift-movements -->"


def render_tournament_outcomes_section(
    generations: list[Generation],
    experiments: list[Experiment],
) -> str:
    """Compose the full ``## Tournament outcomes`` markdown section.

    Stitches together the mermaid lineage graph, the trajectory table,
    the ASCII sparkline, and the per-metric movement tables, each
    followed by the marker for the figure that draws the same data. The
    drift table renders in its historical position with the historical
    heading (``### Drift-kind movements across the promoted lineage``);
    if any non-drift metric movements are present, a separate
    ``### Metric movements (non-drift namespaces)`` section is
    appended after it.

    Each sub-table is elided when its namespace has no recorded
    movements.
    """
    parts: list[str] = []
    parts.append("## Tournament outcomes")
    parts.append("")
    parts.append("### Lineage")
    parts.append("")
    parts.append(render_mermaid_lineage(generations, experiments))
    parts.append("")
    parts.append(_FIGURE_LINEAGE)
    parts.append("")
    parts.append("### Scalar trajectory")
    parts.append("")
    parts.append(render_trajectory_table(generations, experiments))
    parts.append("")
    parts.append(_FIGURE_SCORE_TRAJECTORY)
    parts.append("")
    parts.append("### Score sparkline")
    parts.append("")
    parts.append(render_score_sparkline(generations, experiments))
    drift_table = render_drift_kind_movement_table(generations, experiments)
    if drift_table:
        parts.append("")
        parts.append("### Drift-kind movements across the promoted lineage")
        parts.append("")
        parts.append(drift_table)
        parts.append("")
        parts.append(_FIGURE_DRIFT_MOVEMENTS)
    # Render a separate table for every non-drift metric movement so
    # the section surfaces cost / rubric / latency / schema /
    # output / ... signal without disturbing the drift table's shape.
    nondrift_table = _render_non_drift_metric_table(generations, experiments)
    if nondrift_table:
        parts.append("")
        parts.append("### Metric movements (non-drift namespaces)")
        parts.append("")
        parts.append(nondrift_table)
    parts.append("")
    return "\n".join(parts)


def _render_non_drift_metric_table(
    generations: list[Generation],
    experiments: list[Experiment],
) -> str:
    """Render the metric-movement table restricted to non-drift namespaces.

    Walks every outcome's :attr:`OutcomeRecord.metric_movements` and
    elides entries whose metric_name starts with ``"drift:"`` — those
    are already covered by the historical drift table. Returns the
    empty string when no non-drift metric movements were recorded.
    """
    chain = _promoted_chain(generations, experiments)
    if len(chain) < 2:
        return ""
    exp_idx = _exp_by_child(experiments)
    per_metric: dict[str, list[float | None]] = {}
    seen_any = False
    n_cols = len(chain)
    for step_idx, child in enumerate(chain[1:], start=1):
        exp = exp_idx.get(child.id)
        if exp is None or exp.outcome is None:
            continue
        for mm in exp.outcome.metric_movements:
            if mm.metric_name.startswith("drift:"):
                continue
            seen_any = True
            seq = per_metric.setdefault(mm.metric_name, [None] * n_cols)
            if seq[step_idx - 1] is None:
                seq[step_idx - 1] = mm.from_value
            seq[step_idx] = mm.to_value
    if not seen_any:
        return ""

    rows: list[tuple[str, list[float | None], float]] = []
    for name, seq in per_metric.items():
        first = next((v for v in seq if v is not None), None)
        last = next((v for v in reversed(seq) if v is not None), None)
        net = 0.0 if first is None or last is None else (last - first)
        rows.append((name, seq, net))
    rows.sort(key=lambda r: (-abs(r[2]), r[0]))
    rows = rows[:_DRIFT_KIND_TABLE_LIMIT]

    rate_cols = [f"{g.id}_value" for g in chain]
    if rate_cols:
        rate_cols[-1] = "final_value"
    header_cells = ["metric", "severity", *rate_cols, "net_change"]
    lines: list[str] = ["| " + " | ".join(header_cells) + " |"]
    lines.append("| " + " | ".join("---" for _ in header_cells) + " |")

    def _fmt(v: float | None) -> str:
        return "" if v is None else f"{v:.3f}"

    for name, seq, net in rows:
        cells = [name, ""]
        cells.extend(_fmt(v) for v in seq)
        cells.append(f"{net:+.3f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Typed view of the epoch's records
# ---------------------------------------------------------------------------


def _hydrate_generations(
    workspace_root: Path,
    epoch_id: str,
    fallback_ids: Sequence[str],
) -> list[Generation]:
    """Reconstruct :class:`Generation` instances for ``epoch_id``.

    Prefers the recorded ``lineage.json`` entry for accuracy; falls back
    to the experiment-id ordering when lineage has not been populated
    (the common case in unit tests that bypass the lifecycle helpers).
    """
    raw = load_lineage(workspace_root)
    for entry in raw.get("epochs", []) or []:
        if entry.get("id") != epoch_id:
            continue
        out: list[Generation] = []
        for g in entry.get("generations", []) or []:
            try:
                out.append(
                    Generation(
                        id=str(g["id"]),
                        epoch_id=epoch_id,
                        parent_id=g.get("parent_id"),
                        snapshot_root=Path("/"),
                        created_at=str(g.get("created_at", "")),
                        promoted=bool(g.get("promoted", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            return out
        break

    if not fallback_ids:
        return []
    fallback_out: list[Generation] = []
    prev: str | None = None
    # Ensure v0 (or the earliest id) is treated as the root.
    ordered = list(fallback_ids)
    for gid in ordered:
        fallback_out.append(
            Generation(
                id=gid,
                epoch_id=epoch_id,
                parent_id=prev,
                snapshot_root=Path("/"),
                created_at="",
                promoted=False,
            )
        )
        prev = gid
    return fallback_out


def _generations_for(
    workspace_root: Path,
    epoch_id: str,
    experiments: Sequence[Experiment],
) -> list[Generation]:
    """The :class:`Generation` list the renderers pair with ``experiments``.

    Returns a possibly-empty list. The renderer functions tolerate an empty
    input and emit graceful placeholders, so callers do not need to
    special-case the result.
    """
    # Lineage may not list v0 explicitly if the lifecycle helpers were
    # bypassed; derive a parent-chain fallback from the experiment ids
    # plus any parent ids they reference.
    parents = {e.parent_generation_id for e in experiments if e.parent_generation_id}
    children = {e.generation_id for e in experiments}
    fallback_ids = sorted(parents | children)
    return _hydrate_generations(workspace_root, epoch_id, fallback_ids)


# ---------------------------------------------------------------------------
# Metadata header
# ---------------------------------------------------------------------------


def _render_unreadable_note(reasons: Sequence[str]) -> str:
    """Render the reasons the epoch's unreadable records were left out.

    A generation whose ``experiment.json`` does not parse is missing from
    every table and diagram below, and a reader who is not told that reads
    the epoch as one generation shorter than it was. The note says which
    record and why, so the omission is visible in the document itself
    rather than only in a log line nobody kept.
    """
    lines = [
        f"> **{len(reasons)} generation record"
        f"{'' if len(reasons) == 1 else 's'} could not be read** and "
        "are absent from the tables and diagrams below:",
        ">",
    ]
    lines.extend(f"> * {reason}" for reason in reasons)
    return "\n".join(lines)


def _render_metadata_header(
    epoch_id: str,
    generations: Sequence[Generation],
    experiments: Sequence[Experiment],
) -> str:
    """Render a compact metadata line for the top of analysis.md.

    Reports the number of generations attempted (anything past ``v0``),
    the number that were promoted, and the timestamp span from the
    earliest to the latest recorded generation. Best-effort: if a
    timestamp is missing we omit the duration phrase rather than
    fabricating one.
    """
    attempted = max(len(generations) - 1, 0) if generations else 0
    exp_idx = _exp_by_child(experiments)
    promoted = sum(
        1
        for g in generations
        if g.parent_id is not None
        and (exp := exp_idx.get(g.id)) is not None
        and exp.outcome is not None
        and exp.outcome.tournament_decision == "promoted"
    )
    rejected = sum(
        1
        for g in generations
        if g.parent_id is not None
        and (exp := exp_idx.get(g.id)) is not None
        and exp.outcome is not None
        and exp.outcome.tournament_decision == "rejected"
    )

    start = ""
    end = ""
    timestamps = [g.created_at for g in generations if g.created_at]
    if timestamps:
        start = min(timestamps)
        end = max(timestamps)

    parts: list[str] = []
    parts.append(f"**epoch**: `{epoch_id}`")
    parts.append(f"**generations attempted**: {attempted}")
    parts.append(f"**promoted**: {promoted}")
    parts.append(f"**rejected**: {rejected}")
    if start and end and start != end:
        parts.append(f"**span**: {start} → {end}")
    elif start:
        parts.append(f"**span**: {start}")

    return "  \n".join(parts)


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def _compose_user_prompt(
    epoch_id: str,
    journal_text: str,
    experiments: Sequence[Experiment],
    patterns_text: str,
    tournament_outcomes_md: str,
) -> str:
    """Assemble the prompt body.

    Layout: pre-rendered tournament outcomes (so it anchors the rest of
    the prompt), then the journal, then the structured experiment list,
    then any patterns snapshot. The instruction line at the bottom
    re-emphasises the no-reproduce rule for the outcomes diagrams.
    """
    chunks: list[str] = []
    chunks.append(f"# Epoch under review: {epoch_id}")
    chunks.append("")
    chunks.append(
        "The following diagrams are already rendered. Refer to them in your "
        "narrative; do not reproduce them. Use them as the factual basis "
        "for which generations were promoted vs rejected."
    )
    chunks.append("")
    chunks.append(tournament_outcomes_md.rstrip())
    chunks.append("")
    chunks.append("## Journal")
    chunks.append("")
    chunks.append("```")
    chunks.append(_slice(journal_text or "(no journal entries)", _MAX_JOURNAL_CHARS))
    chunks.append("```")
    chunks.append("")
    chunks.append("## Experiments")
    if not experiments:
        chunks.append("")
        chunks.append("(no experiments recorded for this epoch)")
    else:
        head = experiments[:_MAX_EXPERIMENTS_INLINE]
        for exp in head:
            chunks.append("")
            chunks.append("```json")
            chunks.append(_format_experiment(exp))
            chunks.append("```")
        if len(experiments) > len(head):
            chunks.append("")
            chunks.append(
                f"... [{len(experiments) - len(head)} additional experiments "
                "omitted from the prompt for size]"
            )
    if patterns_text:
        chunks.append("")
        chunks.append("## Patterns")
        chunks.append("")
        chunks.append(_slice(patterns_text, _MAX_PATTERNS_CHARS))
    chunks.append("")
    chunks.append(
        "Produce the narrative half of `analysis.md` now. Emit only the five "
        "level-2 sections from the system prompt — no level-1 heading, no "
        "tournament-outcomes block. Reference the pre-rendered diagrams in "
        "prose but do not re-render them."
    )
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def generate_analysis(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: _AuxCallLLM,
    model: str = "",
) -> Path:
    """Run the analysis pass and write ``analysis.md``.

    Returns the path to the written file. The caller is responsible for
    arranging that ``aux_call_llm`` is the AUXILIARY callable (not the
    inner-harness one) — see :class:`RuntimeConfig` and
    :func:`assert_distinct_callables` for the collusion guard.

    The function is async because the LLM call is. Callers in synchronous
    contexts wrap with ``asyncio.run``; ``close_epoch`` does this for the
    common path.

    The output file structure is::

        # Epoch analysis: <id>

        <metadata header>

        ## Tournament outcomes
        <pre-computed mermaid + tables + sparkline>

        <LLM-generated five narrative sections>

        ---
        <footer>
    """
    journal_text = ""
    jpath = journal_path(workspace_root, epoch_id)
    if jpath.exists():
        journal_text = jpath.read_text()

    records, unreadable = read_epoch_experiments(workspace_root, epoch_id)
    experiments = [experiment for _generation_id, experiment in records]
    patterns_text = _collect_patterns_snapshot(workspace_root, epoch_id)

    typed_gens = _generations_for(workspace_root, epoch_id, experiments)
    tournament_outcomes_md = render_tournament_outcomes_section(typed_gens, experiments)
    metadata_md = _render_metadata_header(epoch_id, typed_gens, experiments)
    if unreadable:
        metadata_md = metadata_md.rstrip() + "\n\n" + _render_unreadable_note(unreadable)

    user_prompt = _compose_user_prompt(
        epoch_id=epoch_id,
        journal_text=journal_text,
        experiments=experiments,
        patterns_text=patterns_text,
        tournament_outcomes_md=tournament_outcomes_md,
    )

    try:
        narrative = await asyncio.wait_for(
            aux_call_llm(_SYSTEM_PROMPT, user_prompt, model),
            timeout=aux_call_timeout_s(),
        )
    except TimeoutError:
        logging.getLogger(__name__).warning(
            "analysis pass timed out after %.1fs; substituting placeholder narrative",
            aux_call_timeout_s(),
        )
        narrative = (
            "## Headline movements\n\n"
            "_(analysis LLM timed out; placeholder narrative written. "
            "Re-run the analysis pass after the endpoint recovers.)_\n\n"
            "## Hypotheses that held\n\n_(unavailable)_\n\n"
            "## Hypotheses that didn't\n\n_(unavailable)_\n\n"
            "## Surface still open at epoch close\n\n_(unavailable)_\n\n"
            "## Recommended focus for next epoch\n\n_(unavailable)_\n"
        )

    composed: list[str] = []
    composed.append(f"# Epoch analysis: {epoch_id}")
    composed.append("")
    composed.append(metadata_md)
    composed.append("")
    composed.append(tournament_outcomes_md.rstrip())
    composed.append("")
    composed.append(narrative.strip())
    composed.append("")
    composed.append("---")
    composed.append("")
    composed.append(
        f"_Generated from `{journal_path(workspace_root, epoch_id).name}` and "
        f"`generations/*/experiment.json` under epoch `{epoch_id}`._"
    )
    composed.append("")

    out_path = analysis_path(workspace_root, epoch_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(composed))

    write_html_companion(workspace_root, epoch_id, out_path)
    return out_path


def write_html_companion(workspace_root: Path, epoch_id: str, md_path: Path) -> Path | None:
    """Render the sibling ``analysis.html`` from the markdown at *md_path*.

    ``analysis.html`` is always :func:`zicato.analyzer.report.render_report_html`
    applied to the ``analysis.md`` beside it, whichever pass wrote that
    markdown — so the served document can never disagree with the report it
    accompanies, and every epoch lifecycle phase serves one renderer's
    output. The structured epoch view (:func:`gather_epoch_report_data`)
    supplies the inline figure SVGs the document's figure markers request.

    Returns the written path, or ``None`` when there is no markdown to
    render or the render failed. Best-effort by contract: the markdown is
    the canonical artifact, so a failure here logs at debug level and
    leaves any existing HTML in place.
    """
    from zicato.analyzer.report import render_report_html
    from zicato.analyzer.report_data import gather_epoch_report_data

    try:
        report_md = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    html_path = md_path.with_suffix(".html")
    try:
        data = gather_epoch_report_data(workspace_root, epoch_id)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_report_html(epoch_id, report_md, data=data), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — HTML is non-critical
        logging.getLogger(__name__).debug("skipping analysis.html (render raised): %s", exc)
        return None
    return html_path


def regenerate_in_progress_html(workspace_root: Path, epoch_id: str) -> Path | None:
    """Refresh ``analysis.md`` and ``analysis.html`` mid-epoch — no LLM call.

    Delegates to the analyzer's deterministic report regeneration, which
    re-templates every data-bearing section from the current workspace data,
    preserves any LLM-authored prose verbatim, and rewrites the HTML
    companion. Returns the HTML path once it exists, or ``None`` for an
    epoch that has produced no generation yet.

    The evolve loop calls this after every round so that ``file://`` readers
    and the dashboard's static fallback see the latest round rather than
    only the state at epoch close. It is the same call the round epilogue
    makes (:func:`zicato.evolve.round_reporting._regenerate_epoch_report`),
    is digest-gated, and rewrites nothing when the round moved no data.
    """
    from zicato.analyzer.report import regenerate_epoch_report_deterministic

    layout = WorkspaceLayout.from_root(workspace_root)
    if not any(
        layout.experiment(epoch_id, generation_id).is_file()
        for generation_id in generation_ids(layout, epoch_id)
    ):
        return None
    regenerate_epoch_report_deterministic(workspace_root, epoch_id)
    html_path = analysis_path(workspace_root, epoch_id).with_suffix(".html")
    return html_path if html_path.exists() else None


__all__ = [
    "REQUIRED_SECTIONS",
    "generate_analysis",
    "regenerate_in_progress_html",
    "render_drift_kind_movement_table",
    "render_mermaid_lineage",
    "render_metric_movement_table",
    "render_score_sparkline",
    "render_tournament_outcomes_section",
    "render_trajectory_table",
    "write_html_companion",
]
