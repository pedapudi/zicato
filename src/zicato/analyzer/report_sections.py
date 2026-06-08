"""Deterministic, data-bearing sections of the epoch analysis report.

Every renderer here turns a :class:`zicato.analyzer.report_data.EpochReportData`
view into a block of markdown that is *exact by construction* — the
numbers come straight from the structured workspace artifacts and are
never paraphrased or rounded by an LLM. The report generator
(:mod:`zicato.analyzer.report`) stitches these blocks together with the
LLM-written prose sections.

These cover the report's Title/metadata header, the Methodology
section (board + scoring + tournament protocol), and the whole
Experimental Results section (per-generation scalars and deltas, the
score trajectory, drift / metric movements, per-generation patch
tables). The Approach/Implementation section's deterministic half (the
mutation surface, the per-generation hypothesis + patch list) lives
here too.

The renderers are pure: identical inputs yield byte-identical output,
which keeps the report diffable round-to-round and the tests stable.

Section numbers and table / figure numbers are NOT written here:
headings emit only their textual title, table captions use the
``Caption:`` marker and figures use the ``<!-- FIGURE:name -->``
anchor. The HTML renderer (:mod:`zicato.analyzer.report`) auto-numbers
``h2 / h3 / h4``, tables, and figures so a section's number is always
its absolute position in the assembled document.
"""

from __future__ import annotations

from zicato.analyzer.report_data import (
    BoardEntryView,
    EpochReportData,
    GenerationView,
)


def _esc_cell(text: str) -> str:
    """Sanitise a string for safe inclusion in a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _fmt_delta(value: float) -> str:
    """Format a signed delta like ``+0.080`` / ``-0.012``."""
    return f"{value:+.3f}"


def _fmt_num(value: float) -> str:
    """Format a non-signed numeric value to three decimal places."""
    return f"{value:.3f}"


# ---------------------------------------------------------------------------
# Title + metadata
# ---------------------------------------------------------------------------


def render_title_block(data: EpochReportData) -> str:
    """Render the report title plus the paper-style masthead block.

    The masthead reads like an academic-paper cover: a small-caps eyebrow
    naming the artifact, the epoch as the title, a thin rule, then a
    structured metadata row covering the contract coordinates (epoch id,
    contract hash, generation span, status, attempt counts). The HTML
    renderer turns the structured metadata into a multi-column block
    beneath the title via the ``<!-- META -->`` marker; each labelled
    pair becomes one stacked label/value cell so the cover reads as a
    real paper masthead rather than a flat sentence.
    """
    lines: list[str] = []
    # The eyebrow marker tells the HTML renderer to emit a small-caps
    # label above the title. Invisible in the markdown source.
    lines.append("<!-- EYEBROW -->")
    lines.append("Zicato improvement campaign · epoch analysis report")
    lines.append("")
    lines.append(f"# {data.epoch_name}")
    lines.append("")
    status = "closed" if data.closed else "in progress"
    # The masthead metadata is rendered as labelled cells (one per
    # entry). Each entry is written as a ``**Label**: value`` pair on
    # its own line — the renderer splits these into stacked
    # label-over-value cells inside a CSS-grid metadata block.
    meta_bits: list[str] = [
        f"**Epoch id**: `{data.epoch_id}`",
        f"**Status**: {status}",
        f"**Generations**: {data.attempted} attempted · "
        f"{data.promoted} promoted · {data.rejected} rejected",
    ]
    if data.deferred:
        meta_bits.append(f"**Deferred**: {data.deferred}")
    if data.contract_hash:
        meta_bits.append(f"**Contract hash**: `{data.contract_hash[:12]}`")
    if data.created_at:
        meta_bits.append(f"**Created**: {data.created_at}")
    if data.span_start and data.span_end and data.span_start != data.span_end:
        meta_bits.append(f"**Generation span**: {data.span_start} → {data.span_end}")
    elif data.span_start:
        meta_bits.append(f"**Generation span**: {data.span_start}")
    if data.closed and data.closed_at:
        meta_bits.append(f"**Closed**: {data.closed_at}")
    # The ``<!-- META -->`` marker tells the HTML renderer to wrap the
    # following paragraph in a paper-style masthead block instead of an
    # ordinary ``<p>``. The marker is invisible in the markdown source.
    lines.append("<!-- META -->")
    lines.append("  \n".join(meta_bits))
    # The operator-supplied goal sits directly under the masthead so
    # the *why* of the epoch is the first thing a reader sees. Empty
    # goals are rendered as "(no goal recorded)" so the report shape
    # stays uniform across epochs that predate the field. Multi-line
    # goals are rendered verbatim under the heading.
    lines.append("")
    lines.append("### Goal")
    lines.append("")
    if data.goal.strip():
        lines.append(data.goal.strip())
    else:
        lines.append("_(no goal recorded)_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------


def _render_board_table(entries: tuple[BoardEntryView, ...]) -> str:
    """Render the evaluation board as a markdown table."""
    if not entries:
        return "_No board entries were recorded for this epoch._"
    lines: list[str] = []
    lines.append("| entry | kind | weight | expectation | judges | budget (s) |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for e in entries:
        exp = e.expectation_kind or "—"
        if e.expectation_spec:
            exp = f"{exp}: {_esc_cell(e.expectation_spec[:60])}"
        judges = ", ".join(e.judges) if e.judges else "—"
        lines.append(
            f"| `{_esc_cell(e.id)}` | {_esc_cell(e.kind)} | {e.weight:.2f} "
            f"| {_esc_cell(exp)} | {_esc_cell(judges)} | {e.wall_clock_budget_seconds} |"
        )
    return "\n".join(lines)


def _render_scoring_block(scoring: dict[str, object]) -> str:
    """Render the scoring model: weights, severity multipliers, gates."""
    if not scoring:
        return "_No scoring model was recorded; epoch defaults applied._"
    lines: list[str] = []
    lines.append("| parameter | value |")
    lines.append("| --- | --- |")

    def _row(label: str, key: str) -> None:
        if key in scoring:
            lines.append(f"| {label} | `{scoring[key]}` |")

    _row("drift_weight", "drift_weight")
    _row("pass_weight", "pass_weight")
    _row("plan_revision_weight", "plan_revision_weight")
    _row("runtime_weight", "runtime_weight")
    _row("promote_margin", "promote_margin")
    _row("pass_rate_monotonicity", "pass_rate_monotonicity")
    _row("pass_rate_monotonicity_scope", "pass_rate_monotonicity_scope")

    sev = scoring.get("severity_weights")
    if isinstance(sev, dict) and sev:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(sev.items()))
        lines.append(f"| severity_weights | {_esc_cell(rendered)} |")
    per_kind = scoring.get("per_kind_weights")
    if isinstance(per_kind, dict) and per_kind:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(per_kind.items()))
        lines.append(f"| per_kind_weights | {_esc_cell(rendered)} |")
    per_judge = scoring.get("per_judge_weights")
    if isinstance(per_judge, dict) and per_judge:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(per_judge.items()))
        lines.append(f"| per_judge_weights | {_esc_cell(rendered)} |")
    ns = scoring.get("namespace_weights")
    if isinstance(ns, dict) and ns:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(ns.items()))
        lines.append(f"| namespace_weights | {_esc_cell(rendered)} |")
    return "\n".join(lines)


def render_methodology_section(data: EpochReportData) -> str:
    """Render the full ``## Methodology`` section.

    Deterministic throughout: the evaluation board, the scoring model,
    and the tournament protocol are all templated directly from
    ``board.jsonl`` / ``scoring.json``. The tournament-protocol prose is
    fixed text describing zicato's champion-vs-challenger contract,
    parameterised only by the recorded promotion margin.

    Section numbers are not emitted here — the HTML renderer
    auto-numbers ``h2 / h3`` so the report reads as one consistently
    numbered document regardless of which sections happen to be present.
    """
    parts: list[str] = []
    parts.append("## Methodology")
    parts.append("")
    parts.append("### Evaluation board")
    parts.append("")
    n_entries = len(data.board_entries)
    n_judged = sum(1 for e in data.board_entries if e.judges)
    parts.append(
        f"The board frozen for this epoch carries {n_entries} "
        f"{'entry' if n_entries == 1 else 'entries'}; "
        f"{n_judged} of them attach one or more in-run process judges. Each "
        "entry is one evaluation unit run against both the champion and the "
        "challenger."
    )
    if data.disable_drift:
        parts.append("")
        parts.append(
            "Board-level `disable_drift` suppresses these drift kinds: "
            + ", ".join(f"`{d}`" for d in data.disable_drift)
            + "."
        )
    parts.append("")
    parts.append("Caption: Evaluation board entries — kinds, weights, expectations and judges.")
    parts.append("")
    parts.append(_render_board_table(data.board_entries))
    parts.append("")
    parts.append("### Scoring model")
    parts.append("")
    parts.append(
        "The tournament reduces each generation to a single scalar: a "
        "weighted, drift-derived loss term plus per-task pass/fail "
        "predicate outcomes. In-run process-judge drift folds into the loss "
        "term, weighted per judge name via `per_judge_weights`. A lower "
        "scalar is better."
    )
    parts.append("")
    parts.append("Caption: Scoring model weights and gates frozen for the epoch.")
    parts.append("")
    parts.append(_render_scoring_block(data.scoring))
    parts.append("")
    parts.append("### Tournament protocol")
    parts.append("")
    margin = data.scoring.get("promote_margin", 0.01)
    parts.append(
        "Each generation is a single round of a champion-vs-challenger "
        "tournament. The challenger (the patched generation) and the "
        "current champion (the lineage head) are each evaluated across "
        "every board entry; board entries are the unit of parallelism. The "
        f"challenger is promoted only when its scalar improves on the "
        f"champion's by at least the promotion margin (`promote_margin = "
        f"{margin}`); otherwise the round is rejected and the champion is "
        "retained. A promoted challenger becomes the champion for the next "
        "round."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Approach / implementation — mutation surface + per-generation patches
# ---------------------------------------------------------------------------


def _render_mutation_surface_table(surface: tuple[dict[str, object], ...]) -> str:
    """Render the most-recent enumerated mutation surface as a table."""
    if not surface:
        return "_No mutation-surface snapshot was recorded for this epoch._"
    lines: list[str] = []
    lines.append("| mutation id | kind | file |")
    lines.append("| --- | --- | --- |")
    for m in surface:
        mid = _esc_cell(str(m.get("id", "")))
        kind = _esc_cell(str(m.get("kind", "")))
        mfile = _esc_cell(str(m.get("file", "")))
        lines.append(f"| `{mid}` | {kind} | `{mfile}` |")
    return "\n".join(lines)


def render_approach_section(data: EpochReportData) -> str:
    """Render the deterministic half of ``## Approach & Implementation``.

    Covers the mutation surface (the editable points the proposer was
    offered) and a per-generation log of the proposer's hypothesis and
    the patch it applied. The interpretive prose is left to the LLM
    layer; this is the factual record. The HTML renderer auto-numbers
    sections and figures, so this writer only emits the textual heading
    plus the figure / table anchors.
    """
    parts: list[str] = []
    parts.append("## Approach & Implementation")
    parts.append("")
    parts.append("### Mutation surface")
    parts.append("")
    n_mut = len(data.mutation_surface)
    parts.append(
        f"The proposer was offered {n_mut} enumerated mutation "
        f"{'point' if n_mut == 1 else 'points'} on the inner harness's "
        "editable surface (the snapshot below reflects the most recent "
        "round's enumeration). Every patch a generation applied addresses "
        "an id drawn from this surface."
    )
    parts.append("")
    parts.append("Caption: Editable mutation points the proposer was offered this round.")
    parts.append("")
    parts.append("<!-- FIGURE:mutation-surface -->")
    parts.append("")
    parts.append("Caption: Mutation surface — full enumeration.")
    parts.append("")
    parts.append(_render_mutation_surface_table(data.mutation_surface))
    parts.append("")
    parts.append("### Mutation-impact matrix")
    parts.append("")
    parts.append(
        "The impact matrix shows which mutation sites each challenger "
        "touched and the tournament outcome of that round. Rows are "
        "mutation sites the campaign has actually addressed (untouched "
        "sites are dropped); columns are challenger generations in lineage "
        "order. A filled cell marks a patch at that site; the cell colour "
        "is the round's outcome — promoted, rejected, or incomplete."
    )
    parts.append("")
    parts.append(
        "Caption: Mutation-impact matrix — exploration pattern across the "
        "campaign, by site × generation × outcome."
    )
    parts.append("")
    parts.append("<!-- FIGURE:mutation-impact-matrix -->")
    parts.append("")
    parts.append("### Lineage diagram")
    parts.append("")
    parts.append(
        "The lineage diagram tracks one champion-vs-challenger lineage. "
        "The promoted spine runs across the centerline; rejected and "
        "deferred branches sit below."
    )
    parts.append("")
    parts.append("Caption: Lineage diagram — promoted spine, rejected and deferred branches.")
    parts.append("")
    parts.append("<!-- FIGURE:lineage -->")
    parts.append("")
    parts.append("### Per-generation hypotheses and patches")
    parts.append("")
    challengers = [g for g in data.generations if not g.is_baseline]
    if not challengers:
        parts.append("_No challenger generations have been proposed yet._")
        return "\n".join(parts)
    for g in challengers:
        parts.append(f"#### Generation {g.generation_id}")
        parts.append("")
        core = g.core_idea.strip() or "(no core idea recorded)"
        parts.append(f"**Hypothesis.** {core}")
        if g.why.strip():
            parts.append("")
            parts.append(f"**Rationale.** {g.why.strip()}")
        if g.risks.strip():
            parts.append("")
            parts.append(f"**Anticipated risks.** {g.risks.strip()}")
        if g.expected_pass_rate_delta.strip():
            parts.append("")
            parts.append(f"**Predicted pass-rate movement.** {g.expected_pass_rate_delta.strip()}")
        parts.append("")
        if g.patches:
            parts.append("| mutation id | op | rationale |")
            parts.append("| --- | --- | --- |")
            for p in g.patches:
                parts.append(
                    f"| `{_esc_cell(p['mutation_id'])}` | `{_esc_cell(p['op'])}` "
                    f"| {_esc_cell(p['rationale'])} |"
                )
        else:
            parts.append("_No patches were applied (proposer produced no valid patch set)._")
        parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Experimental results
# ---------------------------------------------------------------------------


def render_score_trajectory_table(data: EpochReportData) -> str:
    """Render the per-generation scalar trajectory as a markdown table.

    Columns: generation id, cumulative scalar, Δscalar from parent,
    Δdrift_loss, Δpass_rate, decision, and the proposer's one-line core
    idea. The baseline row carries the seed scalar and no deltas.
    """
    if not data.generations:
        return "_No generations have been recorded for this epoch._"
    lines: list[str] = []
    lines.append("| gen | scalar | Δscalar | Δdrift_loss | Δpass_rate " "| decision | core idea |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for g in data.generations:
        if g.is_baseline:
            lines.append(
                f"| `{g.generation_id}` | {_fmt_delta(g.cumulative_scalar)} "
                f"| — | — | — | baseline | (seed) |"
            )
        else:
            core = _esc_cell(g.core_idea.splitlines()[0]) if g.core_idea else ""
            lines.append(
                f"| `{g.generation_id}` | {_fmt_delta(g.cumulative_scalar)} "
                f"| {_fmt_delta(g.scalar_score_delta)} "
                f"| {_fmt_delta(g.drift_loss_delta)} "
                f"| {_fmt_delta(g.pass_rate_delta)} | {g.decision} | {core} |"
            )
    return "\n".join(lines)


def render_score_sparkline(data: EpochReportData, width: int = 28) -> str:
    """Render an ASCII bar chart of the cumulative-scalar trajectory.

    One line per generation, bar normalised across the observed scalar
    range. A fenced code block keeps the column alignment intact. The
    most-recent generation is annotated ``<- current``.
    """
    gens = list(data.generations)
    if not gens:
        return "```\n(no generations)\n```"
    if width <= 0:
        width = 1
    values = [g.cumulative_scalar for g in gens]
    lo, hi = min(values), max(values)
    if hi <= lo:
        spread = max(abs(lo), abs(hi), 1.0) * 0.1
        lo -= spread
        hi += spread
    label_width = max(len(g.generation_id) for g in gens)
    last_idx = len(gens) - 1
    body: list[str] = []
    for i, g in enumerate(gens):
        ratio = (g.cumulative_scalar - lo) / (hi - lo) if hi > lo else 0.5
        ratio = max(0.0, min(1.0, ratio))
        filled = int(round(ratio * width))
        bar = "█" * filled + "░" * (width - filled)
        if g.is_baseline:
            tail = "baseline"
        else:
            arrow = "↑" if g.scalar_score_delta > 0 else ("↓" if g.scalar_score_delta < 0 else "·")
            tail = f"{arrow} {_fmt_delta(g.scalar_score_delta)}  {g.decision}"
        line = (
            f"{g.generation_id:<{label_width}}: {bar}  "
            f"{_fmt_delta(g.cumulative_scalar)}  {tail}"
        )
        if i == last_idx:
            line = line.rstrip() + "   <- current"
        body.append(line.rstrip())
    return "```\n" + "\n".join(body) + "\n```"


def _promoted_lineage(data: EpochReportData) -> list[GenerationView]:
    """Return the baseline plus every promoted generation, in lineage order."""
    by_parent: dict[str, GenerationView] = {}
    baseline: GenerationView | None = None
    for g in data.generations:
        if g.is_baseline and baseline is None:
            baseline = g
        elif g.decision == "promoted":
            by_parent[g.parent_generation_id] = g
    if baseline is None:
        return []
    chain: list[GenerationView] = [baseline]
    current = baseline.generation_id
    seen = {current}
    while current in by_parent:
        nxt = by_parent[current]
        if nxt.generation_id in seen:
            break
        chain.append(nxt)
        seen.add(nxt.generation_id)
        current = nxt.generation_id
    return chain


def render_drift_movement_table(data: EpochReportData) -> str:
    """Render per-drift-kind rate movements across the promoted lineage.

    Walks each promoted generation's realised drift movements and
    stitches the per-kind from/to rates into a column sequence. Returns
    a "no movements" notice when nothing was recorded.
    """
    chain = _promoted_lineage(data)
    if len(chain) < 2:
        return "_No promoted lineage long enough to chart drift movements yet._"
    n_cols = len(chain)
    per_kind: dict[str, list[float | None]] = {}
    seen_any = False
    for step, child in enumerate(chain[1:], start=1):
        for mv in child.drift_movements:
            kind = str(mv.get("kind", ""))
            if not kind:
                continue
            seen_any = True
            seq = per_kind.setdefault(kind, [None] * n_cols)
            from_rate = _coerce_float(mv.get("from_rate"))
            to_rate = _coerce_float(mv.get("to_rate"))
            if seq[step - 1] is None:
                seq[step - 1] = from_rate
            seq[step] = to_rate
    if not seen_any:
        return "_No drift movements were recorded across the promoted lineage._"
    rows: list[tuple[str, list[float | None], float]] = []
    for kind, seq in per_kind.items():
        first = next((v for v in seq if v is not None), None)
        last = next((v for v in reversed(seq) if v is not None), None)
        net = 0.0 if first is None or last is None else last - first
        rows.append((kind, seq, net))
    rows.sort(key=lambda r: (-abs(r[2]), r[0]))
    header = ["drift kind", *[g.generation_id for g in chain], "net change"]
    lines: list[str] = []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for kind, seq, net in rows:
        cells = [f"`{_esc_cell(kind)}`"]
        cells.extend("" if v is None else _fmt_num(v) for v in seq)
        cells.append(_fmt_delta(net))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_per_judge_attribution_table(data: EpochReportData) -> str:
    """Render a judge_name x generation_id table of weighted loss.

    Each row is one in-run process judge that fired at least once
    across the epoch's generations; each column is one generation in
    lineage order. Cells carry the judge's per-generation weighted
    loss total (the sum across every board entry's run under that
    generation). A blank cell means the judge did not fire (or the
    generation produced no readable loss profiles). The final column
    sums the row across generations so the operator can rank judges
    by total cost-of-drift in one read.

    Answers the meta-loop question "which judges drove the drift
    change between v1 and v3?" — a judge whose weighted loss climbs
    or falls along the row is the proximate driver of the per-
    generation scalar movement.

    A no-custom-judge epoch (no judge fired in any generation)
    short-circuits to a single-line italic notice rather than
    rendering an empty table.
    """
    gens = list(data.generations)
    if not gens:
        return "_No generations have been recorded for this epoch._"
    # Per (judge_name, generation_index) → weighted_loss. Tracking the
    # set of seen judges lets us decide whether to render the table at
    # all.
    judge_names: set[str] = set()
    by_cell: dict[tuple[str, int], float] = {}
    for idx, g in enumerate(gens):
        for name, weighted in g.per_judge_loss_totals:
            judge_names.add(name)
            by_cell[(name, idx)] = weighted
    if not judge_names:
        return "_No custom in-run process judge fired across this epoch's generations._"

    # Row sort: row total descending, then judge_name. Puts the
    # noisiest judge at the top so the operator's eye lands on it.
    row_totals: dict[str, float] = {}
    for name in judge_names:
        row_totals[name] = sum(by_cell.get((name, i), 0.0) for i in range(len(gens)))
    sorted_names = sorted(judge_names, key=lambda n: (-row_totals[n], n))

    header = ["judge", *[g.generation_id for g in gens], "total"]
    lines: list[str] = []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for name in sorted_names:
        cells: list[str] = [f"`{_esc_cell(name)}`"]
        for i in range(len(gens)):
            val = by_cell.get((name, i))
            cells.append(_fmt_num(val) if val is not None else "")
        cells.append(_fmt_num(row_totals[name]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_per_board_outcomes(data: EpochReportData) -> str:
    """Render per-board-entry outcome columns across promoted generations.

    Each promoted generation's cached ``gen_score.json`` may carry a
    per-entry breakdown; when present, this renders a board-entry x
    generation table of per-entry scalars. Best-effort: if no
    ``gen_score.json`` carries an entry breakdown the section reports
    that and falls back to the aggregate gen-score table.
    """
    chain = _promoted_lineage(data)
    scored = [g for g in chain if g.gen_score]
    if not scored:
        return "_No cached generation scores are available for the promoted lineage yet._"
    # Aggregate gen-score table — the keys the tournament runner caches.
    agg_keys = ["scalar", "drift_loss_mean", "pass_rate"]
    present = [k for k in agg_keys if any(k in g.gen_score for g in scored)]
    if not present:
        return "_Cached generation scores carry no recognised aggregate keys._"
    header = ["aggregate", *[g.generation_id for g in scored]]
    lines: list[str] = []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for key in present:
        cells = [f"`{key}`"]
        for g in scored:
            val = g.gen_score.get(key)
            cells.append(_fmt_num(_coerce_float(val)) if val is not None else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_results_section(data: EpochReportData) -> str:
    """Render the full ``## Experimental Results`` section.

    Entirely deterministic — every table and figure is templated from
    the structured workspace data. Sub-sections: the scalar trajectory
    (figure + table), the per-generation drift-kind movements (figure +
    table), the per-board outcomes heatmap, and the cached aggregate
    generation scores. The HTML renderer auto-numbers everything.
    """
    parts: list[str] = []
    parts.append("## Experimental Results")
    parts.append("")
    parts.append("### Score trajectory")
    parts.append("")
    parts.append(
        "Each row is one generation. The scalar is cumulative — seeded "
        "at 0.000 on the baseline and advanced by each promoted "
        "generation's `Δscalar`. A negative `Δscalar` is an "
        "improvement (lower loss)."
    )
    parts.append("")
    parts.append(
        "Caption: Scalar (loss — lower is better) across generations. "
        "Promoted points connect along the promoted spine; rejected "
        "points sit off-spine."
    )
    parts.append("")
    parts.append("<!-- FIGURE:score-trajectory -->")
    parts.append("")
    parts.append("Caption: Per-generation scalar deltas with proposer's one-line idea.")
    parts.append("")
    parts.append(render_score_trajectory_table(data))
    parts.append("")
    parts.append(render_score_sparkline(data))
    parts.append("")
    callout = _render_campaign_callout(data)
    if callout:
        parts.append(callout)
        parts.append("")
    parts.append("### Hypothesis vs outcome")
    parts.append("")
    parts.append(
        "For every completed challenger, the figure pairs the proposer's "
        "PREDICTED Δ (outlined, dashed) with the tournament's ACTUAL Δ "
        "(filled, decision-coloured) on both pass rate and drift loss. "
        "Predictions are projected onto the same axis the realised Δ "
        "occupies: a textual `expected_pass_rate_delta` like "
        '`"+0.05 to +0.15"` becomes its midpoint; an '
        "`expected_drift_movements` entry's direction and magnitude bucket "
        '(e.g. "decrease / moderate") becomes a signed magnitude on the '
        "drift axis. The proposer's hit rate is the share of pairs that "
        "point the same direction with comparable magnitude."
    )
    parts.append("")
    parts.append(
        "Caption: Proposer hypothesis vs tournament outcome — pass-rate "
        "and drift-loss Δ, predicted (outlined) vs actual (filled)."
    )
    parts.append("")
    parts.append("<!-- FIGURE:hypothesis-vs-outcome -->")
    parts.append("")
    parts.append("### Drift-kind movements")
    parts.append("")
    parts.append(
        "For every challenger round that produced drift movements, the "
        "figure pairs the from-rate (top, light) with the to-rate "
        "(bottom, solid) for each drift kind and labels the signed "
        "rate Δ. The accompanying table threads the per-kind rates "
        "along the promoted lineage."
    )
    parts.append("")
    parts.append(
        "Caption: Per-generation drift-kind rate movements. "
        "Each panel pairs from-rate vs. to-rate for one challenger."
    )
    parts.append("")
    parts.append("<!-- FIGURE:drift-movements -->")
    parts.append("")
    parts.append("Caption: Per-drift-kind rate movements along the promoted lineage.")
    parts.append("")
    parts.append(render_drift_movement_table(data))
    parts.append("")
    parts.append("### Per-judge drift attribution")
    parts.append("")
    parts.append(
        "For every custom in-run process judge that fired across this "
        "epoch, the table threads its per-generation weighted-loss "
        "contribution along the lineage. The contribution is the "
        "judge's `raw_loss * weight` summed across every board entry's "
        "run within the generation; the `total` column sums the row so "
        "the operator can rank judges by cumulative cost-of-drift. "
        "Cells with no value mean the judge did not fire (or the "
        "generation produced no readable loss profiles). A judge whose "
        "weighted loss climbs or falls along the row is the proximate "
        "driver of the per-generation scalar movement — this is the "
        '"which judges drove the meta-loop\'s progress" view.'
    )
    parts.append("")
    parts.append("Caption: Per-judge weighted-loss attribution across generations.")
    parts.append("")
    parts.append(render_per_judge_attribution_table(data))
    parts.append("")
    parts.append("### Per-board outcomes")
    parts.append("")
    parts.append(
        "Each cell encodes the per-entry Δ scalar of the challenger "
        "against the round's champion (red = worse, grey = ~flat, "
        "green = better). Columns marked 'cached' reused the previous "
        "champion's evaluation rather than re-running it."
    )
    parts.append("")
    parts.append(
        "Caption: Per-board entry Δ scalar across challenger generations. "
        "Hatched cells mark entries with no per-entry breakdown recorded."
    )
    parts.append("")
    parts.append("<!-- FIGURE:per-board-heatmap -->")
    parts.append("")
    parts.append("### Aggregate generation scores")
    parts.append("")
    parts.append("Caption: Aggregate generation scores cached by the tournament runner.")
    parts.append("")
    parts.append(render_per_board_outcomes(data))
    return "\n".join(parts)


def _coerce_float(value: object) -> float:
    """Coerce ``value`` to ``float``, defaulting to ``0.0`` on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _render_campaign_callout(data: EpochReportData) -> str:
    """Render a one-line deterministic callout summarising the campaign.

    The callout sits in the Experimental Results section after the score
    trajectory and highlights the headline number the operator cares
    about: the cumulative scalar so far, and the promoted/attempted
    ratio. It uses the ``<!-- CALLOUT:LABEL -->`` marker the HTML
    renderer turns into a sidenote-style block.

    Conservative: emits the empty string when there are no challengers
    yet (no story to tell).
    """
    if data.attempted == 0:
        return ""
    final = data.final_scalar
    direction = "improved" if final < 0 else ("regressed" if final > 0 else "held")
    bits: list[str] = []
    bits.append("<!-- CALLOUT:KEY OBSERVATION -->")
    bits.append(
        f"The promoted lineage's cumulative scalar has {direction} to "
        f"`{final:+.3f}` after {data.attempted} challenger "
        f"{'generation' if data.attempted == 1 else 'generations'} "
        f"({data.promoted} promoted, {data.rejected} rejected). "
        f"A lower scalar is better; the trajectory figure plots the path."
    )
    return "\n".join(bits)


# ---------------------------------------------------------------------------
# Threats to validity
# ---------------------------------------------------------------------------


def render_threats_section(data: EpochReportData) -> str:
    """Render ``## Threats to Validity & Limitations``.

    Parameterised by the epoch's measurable scale — board size,
    generation count, judge coverage — so the caveats reflect the
    actual run rather than boilerplate. Section numbers are added by
    the HTML renderer's auto-numbering pass.
    """
    n_entries = len(data.board_entries)
    n_judged = sum(1 for e in data.board_entries if e.judges)
    attempted = data.attempted
    parts: list[str] = []
    parts.append("## Threats to Validity & Limitations")
    parts.append("")
    bullets: list[str] = []
    bullets.append(
        f"**Board size.** The evaluation board carries {n_entries} "
        f"{'entry' if n_entries == 1 else 'entries'}. A small board "
        "narrows coverage of the inner harness's behaviour space; a "
        "scalar improvement may not generalise beyond the entries "
        "exercised here."
    )
    bullets.append(
        f"**Judge reliability.** {n_judged} of {n_entries} board "
        f"{'entry' if n_entries == 1 else 'entries'} attach in-run "
        "process judges. Judge verdicts are themselves LLM-derived and "
        "carry their own noise; per-judge weighting concentrates that "
        "noise into the scalar."
    )
    bullets.append(
        f"**Sample size.** {attempted} challenger "
        f"{'generation has' if attempted == 1 else 'generations have'} "
        "been evaluated. Each generation is a single tournament round; "
        "per-round scalar deltas near the promotion margin are within "
        "the noise floor of a single evaluation."
    )
    bullets.append(
        "**Emulated entries.** Multi-turn entries are driven by an "
        "emulated user; the emulator is collusion-guarded but still an "
        "approximation of real user behaviour."
    )
    bullets.append(
        "**Lineage is linear.** Only the promoted chain carries forward; "
        "rejected branches are recorded but not re-explored, so the "
        "search is greedy and can settle in a local optimum."
    )
    for b in bullets:
        parts.append(f"- {b}")
    return "\n".join(parts)


__all__ = [
    "render_title_block",
    "render_methodology_section",
    "render_approach_section",
    "render_results_section",
    "render_score_trajectory_table",
    "render_score_sparkline",
    "render_drift_movement_table",
    "render_per_board_outcomes",
    "render_per_judge_attribution_table",
    "render_threats_section",
]
