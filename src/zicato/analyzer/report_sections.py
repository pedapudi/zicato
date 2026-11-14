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

The output is plain Markdown with pipe tables. Headings carry only their
textual title and a table is introduced by a ``Caption:`` line, so nothing
here depends on the section's position in the assembled document.
"""

from __future__ import annotations

import re
from typing import Any

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
    """Render the report title plus its masthead.

    The masthead reads like an academic-paper cover: a line naming the
    artifact, the epoch as the title, then a list of labelled metadata
    covering the contract coordinates (epoch id, contract hash, generation
    span, status, attempt counts).
    """
    lines: list[str] = []
    lines.append("Zicato improvement campaign · epoch analysis report")
    lines.append("")
    lines.append(f"# {data.epoch_name}")
    lines.append("")
    status = "closed" if data.closed else "in progress"
    # Each masthead entry is a ``**Label**: value`` pair, one list item each.
    meta_bits: list[str] = [
        f"**Epoch id**: `{data.epoch_id}`",
        f"**Status**: {status}",
    ]
    # LIVING DRAFT stamp — visible while the epoch is open so a reader
    # knows the document is mid-run and not the finished paper. Data-
    # derived: present iff the epoch is not yet closed, so the close
    # render drops it automatically. Suppressed before any round settles
    # (nothing to be a draft "through" yet).
    if not data.closed and data.last_round > 0:
        meta_bits.append(f"**Draft**: LIVING DRAFT — through round {data.last_round}")
    meta_bits.append(
        f"**Generations**: {data.attempted} attempted · "
        f"{data.promoted} promoted · {data.rejected} rejected"
    )
    if data.deferred:
        meta_bits.append(f"**Deferred**: {data.deferred}")
    # A generation whose record could not be read is in none of the counts
    # above and in none of the tables below. Saying so on the masthead is
    # what stops the report reading as a complete account of a shorter
    # epoch than the one that ran.
    if data.unreadable_generations:
        unreadable = len(data.unreadable_generations)
        meta_bits.append(
            f"**Unreadable records**: {unreadable} generation "
            f"{'record' if unreadable == 1 else 'records'} could not be read "
            "and are excluded from every count and table below "
            f"({'; '.join(data.unreadable_generations)})"
        )
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
    lines.extend(f"- {bit}" for bit in meta_bits)
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

    _row("pass_weight", "pass_weight")
    _row("plan_revision_weight", "plan_revision_weight")
    _row("task_failure_weight", "task_failure_weight")
    _row("not_completed_weight", "not_completed_weight")
    _row("promote_margin", "promote_margin")
    _row("pass_rate_monotonicity", "pass_rate_monotonicity")
    _row("pass_rate_monotonicity_scope", "pass_rate_monotonicity_scope")
    _row("telemetry_dialect", "telemetry_dialect")

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


def _render_tournament_structure_block(structure: dict[str, object]) -> str:
    """Render the recorded tournament format and its parameters."""
    name = str(structure.get("structure", "") or "") if structure else ""
    if not name:
        return "No tournament structure is recorded."
    params = structure.get("params")
    params = params if isinstance(params, dict) else {}
    lines: list[str] = []
    lines.append(f"Tournament structure: **{_esc_cell(name)}**.")
    lines.append("")
    if not params:
        lines.append("_Structure defaults applied; no structure params were overridden._")
        return "\n".join(lines)
    lines.append("| structure param | value |")
    lines.append("| --- | --- |")
    for key in sorted(params):
        val = params[key]
        if isinstance(val, dict):
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(val.items()))
        else:
            rendered = str(val)
        lines.append(f"| `{_esc_cell(str(key))}` | {_esc_cell(rendered)} |")
    return "\n".join(lines)


def _render_proposer_config_block(scoring: dict[str, object]) -> str:
    """Render retained ordinary and experimental proposer settings.

    Historical decoding preserves values stored at former field locations.
    Execution role bindings are absent from the scoring artifact.
    """
    from zicato.workspace_loader import scoring_weights_from_dict

    weights = scoring_weights_from_dict(scoring)
    quality = weights.proposer_quality
    experimental = weights.experimental
    best_of_n = quality.best_of_n
    critique = quality.critique_enabled and best_of_n > 1
    screen = quality.screen_entries
    veto_only = quality.screen_veto_only
    exemplars = experimental.process_exemplars
    genealogy = experimental.genealogy
    recombine = experimental.recombine
    merge = experimental.recombine_merge

    lines: list[str] = []
    lines.append("| proposer lever | setting |")
    lines.append("| --- | --- |")
    lines.append(
        f"| best_of_n | {best_of_n}"
        + (" (single sample — no slate)" if best_of_n == 1 else " (slate)")
        + " |"
    )
    lines.append(
        "| self-critique | " + ("on" if critique else "off (inert without a slate)") + " |"
    )
    if screen > 0:
        lines.append(
            f"| pre-tournament screen | {screen} "
            f"{'entry' if screen == 1 else 'entries'}"
            + (" · veto-only" if veto_only else " · veto+advise")
            + " |"
        )
    else:
        lines.append("| pre-tournament screen | off |")
    lines.append(f"| process exemplars | {exemplars if exemplars else 'off'} |")
    lines.append(f"| genealogy channel | {genealogy if genealogy else 'off'} |")
    if recombine:
        lines.append(f"| recombination | on · `{_esc_cell(merge)}` merge |")
    else:
        lines.append("| recombination | off |")
    lines.append("| breadth/depth roles | _not recorded in the scoring artifact_ |")
    return "\n".join(lines)


def render_methodology_section(data: EpochReportData) -> str:
    """Render the full ``## Methodology`` section.

    Deterministic throughout: the evaluation board, the scoring model,
    and the tournament protocol are all templated directly from
    ``board.jsonl`` / ``scoring.json``. The tournament-protocol prose is
    fixed text describing zicato's champion-vs-challenger contract,
    parameterised only by the recorded promotion margin.

    Section numbers are not emitted here, so the report reads the same
    regardless of which sections happen to be present.
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
    parts.append("")
    parts.append("### Tournament structure")
    parts.append("")
    parts.append(_render_tournament_structure_block(data.tournament_structure))
    parts.append("")
    parts.append("### Proposer configuration")
    parts.append("")
    parts.append(
        "The proposer is a first-class contract input. The levers frozen for "
        "this epoch (a change to any of which rolls the epoch):"
    )
    parts.append("")
    parts.append("Caption: Proposer-quality configuration frozen for the epoch.")
    parts.append("")
    parts.append(_render_proposer_config_block(data.scoring))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Approach / implementation — mutation surface + per-generation patches
# ---------------------------------------------------------------------------


def _render_mutation_surface_table(surface: tuple[dict[str, object], ...]) -> str:
    """Render the most-recent enumerated mutation surface as a table."""
    if not surface:
        return "_No mutation-surface snapshot was recorded for this epoch._"
    lines: list[str] = []
    lines.append("| mutation id | kind | file | lines |")
    lines.append("| --- | --- | --- | --- |")
    for m in surface:
        mid = _esc_cell(str(m.get("id", "")))
        kind = _esc_cell(str(m.get("kind", "")))
        mfile = _esc_cell(str(m.get("file", "")))
        start, end = m.get("line_start") or m.get("lineno"), m.get("line_end")
        span = f"{start}-{end}" if start and end else (str(start) if start else "")
        lines.append(f"| `{mid}` | {kind} | `{mfile}` | {span} |")
    return "\n".join(lines)


def _decision_word(g: GenerationView) -> str:
    """The generation's outcome as one word; an undecided one reads ``pending``."""
    return "baseline" if g.is_baseline else (g.decision or "pending")


def _table_head(header: list[str]) -> list[str]:
    """The header and delimiter rows of a pipe table."""
    return ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]


def render_mutation_impact_table(data: EpochReportData) -> str:
    """Render mutation site x challenger generation, marking each patched site.

    Rows are the sites at least one challenger patched, in first-touched
    order; columns are the challengers in lineage order, headed by their
    outcome. A site no challenger touched is dropped.
    """
    challengers = [g for g in data.generations if not g.is_baseline]
    if not challengers:
        return "_No challengers yet — the mutation-impact matrix is empty._"
    touched = [
        {str(p.get("mutation_id", "")) for p in g.patches if p.get("mutation_id")}
        for g in challengers
    ]
    sites = list(dict.fromkeys(mid for ids in touched for mid in sorted(ids)))
    if not sites:
        return "_No patches touched the mutation surface yet._"
    lines = _table_head(
        ["mutation id", *[f"{g.generation_id} ({_decision_word(g)})" for g in challengers]]
    )
    for mid in sites:
        cells = [f"`{_esc_cell(mid)}`", *["patched" if mid in ids else "" for ids in touched]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_lineage_table(data: EpochReportData) -> str:
    """Render each generation beside its parent and its outcome."""
    if not data.generations:
        return "_No generations have been recorded for this epoch._"
    lines = _table_head(["gen", "parent", "decision"])
    for g in data.generations:
        parent = "—" if g.is_baseline or not g.parent_generation_id else g.parent_generation_id
        lines.append(f"| `{g.generation_id}` | {parent} | {_decision_word(g)} |")
    return "\n".join(lines)


def render_approach_section(data: EpochReportData) -> str:
    """Render the deterministic half of ``## Approach & Implementation``.

    Covers the mutation surface (the editable points the proposer was
    offered) and a per-generation log of the proposer's hypothesis and
    the patch it applied. The interpretive prose is left to the LLM
    layer; this is the factual record.
    """
    parts: list[str] = []
    parts.append("## Approach & Implementation")
    parts.append("")
    parts.append("### Mutation surface")
    parts.append("")
    n_mut = len(data.mutation_surface)
    parts.append(
        f"The proposer was offered {n_mut} enumerated mutation "
        f"{'point' if n_mut == 1 else 'points'} on the system under test's "
        "editable surface (the snapshot below reflects the most recent "
        "round's enumeration). Every patch a generation applied addresses "
        "an id drawn from this surface."
    )
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
        "order, each headed by its round's outcome. A `patched` cell marks "
        "a patch at that site."
    )
    parts.append("")
    parts.append(
        "Caption: Mutation-impact matrix — exploration pattern across the "
        "campaign, by site × generation × outcome."
    )
    parts.append("")
    parts.append(render_mutation_impact_table(data))
    parts.append("")
    parts.append("### Lineage")
    parts.append("")
    parts.append(
        "The lineage tracks one champion-vs-challenger line of descent. "
        "Baseline and promoted generations form the spine; rejected and "
        "deferred generations branch off the parent they challenged."
    )
    parts.append("")
    parts.append("Caption: Lineage — each generation, its parent, and its outcome.")
    parts.append("")
    parts.append(render_lineage_table(data))
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

    Columns: generation id, decision, cumulative scalar, Δscalar from
    parent, Δdrift_loss, Δpass_rate, and the proposer's one-line core
    idea. The baseline row carries the seed scalar and no deltas.

    ``decision`` sits beside ``scalar`` because it qualifies it: for a
    rejected row the cumulative is a counterfactual — where the lineage
    would have stood had that challenger been accepted — and the
    lineage did not in fact move. A note under the table says so, so
    the column cannot be read as a running score of the champion.
    """
    if not data.generations:
        return "_No generations have been recorded for this epoch._"
    lines: list[str] = []
    lines.append("| gen | decision | scalar | Δscalar | Δdrift_loss | Δpass_rate " "| core idea |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for g in data.generations:
        if g.is_baseline:
            lines.append(
                f"| `{g.generation_id}` | baseline | {_fmt_delta(g.cumulative_scalar)} "
                f"| — | — | — | (seed) |"
            )
        else:
            core = _esc_cell(g.core_idea.splitlines()[0]) if g.core_idea else ""
            lines.append(
                f"| `{g.generation_id}` | {g.decision} | {_fmt_delta(g.cumulative_scalar)} "
                f"| {_fmt_delta(g.scalar_score_delta)} "
                f"| {_fmt_delta(g.drift_loss_delta)} "
                f"| {_fmt_delta(g.pass_rate_delta)} | {core} |"
            )
    lines.append("")
    lines.append(
        "_`scalar` is cumulative from the seed. On a **rejected** row it is the "
        "counterfactual the challenger would have reached had it been promoted — "
        "the lineage stayed where it was. Only baseline and promoted rows report "
        "a score the harness stands behind._"
    )
    return "\n".join(lines)


def render_score_sparkline(data: EpochReportData, width: int = 28) -> str:
    """Render an ASCII bar chart of the cumulative-scalar trajectory.

    One line per generation, bar normalised across the observed scalar
    range. A fenced code block keeps the column alignment intact.

    ``<- current`` marks the generation actually IN FORCE — the last
    promoted one, or the baseline when nothing has promoted — not the
    newest row. The newest row is a rejected challenger whenever the
    last round did not promote, and its bar is a counterfactual; marking
    it "current" tells the operator the discarded attempt is the state
    of the system.
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
    champion = data.champion_history[-1] if data.champion_history else None
    body: list[str] = []
    for g in gens:
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
        if g.generation_id == champion:
            line = line.rstrip() + "   <- current"
        body.append(line.rstrip())
    return "```\n" + "\n".join(body) + "\n```"


def _promoted_lineage(data: EpochReportData) -> list[GenerationView]:
    """Return the recorded champions with available measurements, in round order."""
    generations = {generation.generation_id: generation for generation in data.generations}
    return [generations[gid] for gid in data.champion_history if gid in generations]


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
        for mv in child.metric_movements:
            kind = str(mv.get("metric_name", ""))
            if not kind.startswith("drift:"):
                continue
            kind = kind.removeprefix("drift:")
            seen_any = True
            seq = per_kind.setdefault(kind, [None] * n_cols)
            from_rate = _coerce_float(mv.get("from_value"))
            to_rate = _coerce_float(mv.get("to_value"))
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
    lines = _table_head(header)
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
    lines = _table_head(header)
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
        # Name the true cause. "No scores available yet" reads as a harness
        # that has not run; when nothing promoted, the harness ran fine and
        # scored every challenger — the promoted lineage is just the baseline,
        # so there is no second column to compare against.
        if data.promoted == 0 and data.rejected > 0:
            return (
                f"_Nothing has promoted, so the promoted lineage is the baseline alone "
                f"and there is no cross-generation comparison to draw. "
                f"{data.rejected} challenger "
                f"{'generation was' if data.rejected == 1 else 'generations were'} "
                f"scored and rejected; those scores are in the trajectory table above._"
            )
        return "_No cached generation scores are available for the promoted lineage yet._"
    # Aggregate gen-score table — the keys the tournament runner caches.
    agg_keys = ["scalar", "drift_loss_mean", "pass_rate"]
    present = [k for k in agg_keys if any(k in g.gen_score for g in scored)]
    if not present:
        return "_Cached generation scores carry no recognised aggregate keys._"
    header = ["aggregate", *[g.generation_id for g in scored]]
    lines = _table_head(header)
    for key in present:
        cells = [f"`{key}`"]
        for g in scored:
            val = g.gen_score.get(key)
            cells.append(_fmt_num(_coerce_float(val)) if val is not None else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# Magnitude words a hypothesis attaches to an expected drift movement,
# projected onto the rate axis the realised drift-loss Δ is read on. The
# values are coarse: the prediction is a category rather than a measurement.
_MAGNITUDE_MAP = {"small": 0.05, "medium": 0.10, "large": 0.20}
_DIRECTION_SIGN = {
    "decrease": -1,
    "decrease_or_neutral": -1,
    "increase": 1,
    "increase_or_neutral": 1,
}


def _parse_expected_pass_rate_delta(text: str) -> float | None:
    """Coerce a textual ``expected_pass_rate_delta`` to a midpoint float.

    The proposer writes free-form predictions like ``"+0.05 to +0.15"`` or
    ``"-0.10"``. A single signed number is returned as is, a range as its
    midpoint, and text carrying no number as ``None``.
    """
    if not text or not isinstance(text, str):
        return None
    values = [float(n) for n in re.findall(r"[+-]?\d+\.\d+|[+-]?\d+", text)]
    if not values:
        return None
    return (values[0] + values[1]) / 2.0 if len(values) >= 2 else values[0]


def _predicted_drift_delta_sum(g: GenerationView) -> float | None:
    """Project the proposer's expected drift movements to a single Δ scalar.

    Sums the signed magnitude of every expected drift movement: a
    "decrease / medium" contributes -0.10, an "increase / small" +0.05.
    Returns ``None`` when the hypothesis predicted no directional drift
    movement, so the table can say "no prediction" rather than zero.
    """
    total = 0.0
    seen = False
    for mv in g.expected_metric_movements:
        if not mv["metric_name"].startswith("drift:"):
            continue
        amount = _MAGNITUDE_MAP[mv["magnitude"]]
        sign = _DIRECTION_SIGN.get(mv["direction"], 0)
        if sign == 0:
            continue
        total += sign * amount
        seen = True
    return total if seen else None


def _hit_or_miss(pred: float | None, act: float | None) -> str:
    """``hit`` when prediction and outcome agree in direction, else ``miss``.

    Two values that are both about zero agree. Empty when there is no
    prediction to score.
    """
    if pred is None or act is None:
        return ""
    if abs(pred) < 1e-6 and abs(act) < 1e-6:
        return "hit"
    return "hit" if (pred >= 0) == (act >= 0) else "miss"


def render_hypothesis_outcome_table(data: EpochReportData) -> str:
    """Render each decided challenger's predicted Δ beside its actual Δ.

    Rejected and deferred rounds are included: the prediction was made, so
    the pair is reported. A missing prediction reads as a dash.
    """
    rounds = [
        g for g in data.generations if not g.is_baseline and g.decision not in ("pending", "")
    ]
    if not rounds:
        return "_No scored challengers to compare against predictions._"
    lines = _table_head(
        [
            "gen",
            "decision",
            "predicted Δpass_rate",
            "actual Δpass_rate",
            "pass-rate call",
            "predicted Δdrift_loss",
            "actual Δdrift_loss",
            "drift call",
        ]
    )
    for g in rounds:
        cells = [f"`{g.generation_id}`", g.decision]
        for pred, act in (
            (_parse_expected_pass_rate_delta(g.expected_pass_rate_delta), g.pass_rate_delta),
            (_predicted_drift_delta_sum(g), g.drift_loss_delta),
        ):
            cells.append("—" if pred is None else _fmt_delta(pred))
            cells.append(_fmt_delta(act))
            cells.append(_hit_or_miss(pred, act) or "—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _opt_float(value: object) -> float | None:
    """Coerce ``value`` to ``float``, preserving absence as ``None``."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def render_challenger_drift_table(data: EpochReportData) -> str:
    """Render every challenger's drift-kind movements, whatever its outcome.

    One row per (challenger, drift kind) with the from-rate, the to-rate
    and the signed Δ. Unlike :func:`render_drift_movement_table` this
    includes rejected and deferred challengers.
    """
    lines = _table_head(["gen", "decision", "drift kind", "from", "to", "Δ"])
    for g in data.generations:
        if g.is_baseline:
            continue
        for mv in g.metric_movements:
            kind = str(mv.get("metric_name", "")).strip()
            if not kind.startswith("drift:"):
                continue
            fr, to = _opt_float(mv.get("from_value")), _opt_float(mv.get("to_value"))
            lines.append(
                f"| `{g.generation_id}` | {g.decision} "
                f"| `{_esc_cell(kind.removeprefix('drift:'))}` "
                f"| {'—' if fr is None else _fmt_num(fr)} "
                f"| {'—' if to is None else _fmt_num(to)} "
                f"| {'—' if fr is None or to is None else _fmt_delta(to - fr)} |"
            )
    if len(lines) == 2:
        return "_No drift movements recorded yet._"
    return "\n".join(lines)


_ENTRY_DELTA_KEYS = ("scalar_delta", "delta", "scalar_score_delta")
_ENTRY_LOSS_KEYS = ("drift_loss", "loss", "loss_mean")


def _entry_records(g: GenerationView, entry_id: str) -> list[dict[str, Any]]:
    """One board entry's cached records, under ``entries`` then ``per_entry``.

    The tournament runner has cached per-entry results under several
    shapes: ``entries`` keyed by id, and ``per_entry`` either keyed by id
    or as a list of rows naming their ``entry_id``.
    """
    records: list[Any] = []
    for key in ("entries", "per_entry"):
        block = g.gen_score.get(key)
        if isinstance(block, dict):
            records.append(block.get(entry_id))
        elif isinstance(block, list) and key == "per_entry":
            records.extend(
                r for r in block if isinstance(r, dict) and str(r.get("entry_id", "")) == entry_id
            )
    return [r for r in records if isinstance(r, dict)]


def _first_float(records: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    """The first of ``keys`` any record carries as a number, record by record."""
    for record in records:
        for key in keys:
            value = _opt_float(record[key]) if key in record else None
            if value is not None:
                return value
    return None


def _per_entry_delta_against(
    g: GenerationView, parent: GenerationView | None, entry_id: str
) -> float | None:
    """Resolve one challenger x board-entry Δ scalar.

    A cached per-entry delta is used when present. Otherwise the cell is
    the challenger's absolute per-entry drift loss minus the parent
    champion's, which is the shape the orchestrator currently caches.
    ``None`` means neither reading is available, so the cell is reported
    as having no data rather than as zero.
    """
    cached = _first_float(_entry_records(g, entry_id), _ENTRY_DELTA_KEYS)
    if cached is not None or parent is None:
        return cached
    chal = _first_float(_entry_records(g, entry_id)[::-1], _ENTRY_LOSS_KEYS)
    champ = _first_float(_entry_records(parent, entry_id)[::-1], _ENTRY_LOSS_KEYS)
    return None if chal is None or champ is None else chal - champ


def render_per_board_delta_table(data: EpochReportData) -> str:
    """Render board entry x challenger generation per-entry Δ scalar.

    Rows are board entries in board order and columns are challengers in
    lineage order. A column whose round reused a cached champion
    evaluation is headed ``(cached)``.
    """
    challengers = [g for g in data.generations if not g.is_baseline]
    if not data.board_entries or not challengers:
        return "_No per-board outcomes recorded yet._"
    by_id = {g.generation_id: g for g in data.generations}
    header = ["board entry"]
    for g in challengers:
        cached = any(g.gen_score.get(k) for k in ("champion_cached", "cached", "champion_reused"))
        header.append(f"{g.generation_id} (cached)" if cached else g.generation_id)
    lines = _table_head(header)
    for e in data.board_entries:
        cells = [f"`{_esc_cell(e.id)}`"]
        for g in challengers:
            value = _per_entry_delta_against(g, by_id.get(g.parent_generation_id), e.id)
            cells.append("—" if value is None else _fmt_delta(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_results_section(data: EpochReportData) -> str:
    """Render the full ``## Experimental Results`` section.

    Entirely deterministic — every table is templated from the
    structured workspace data. Sub-sections: the scalar trajectory, the
    proposer's predictions beside the outcomes, the drift-kind movements
    per challenger and along the promoted lineage, the per-judge
    attribution, the per-board-entry outcomes, and the cached aggregate
    generation scores.
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
        "For every completed challenger, the table pairs the proposer's "
        "PREDICTED Δ with the tournament's ACTUAL Δ on both pass rate and "
        "drift loss. "
        "Predictions are projected onto the same axis the realised Δ "
        "occupies: a textual `expected_pass_rate_delta` like "
        '`"+0.05 to +0.15"` becomes its midpoint; an '
        "`expected_metric_movements` entry's direction and magnitude bucket "
        '(e.g. "decrease / moderate") becomes a signed magnitude on the '
        "drift axis. A pair is a `hit` when prediction and outcome point "
        "the same direction (or both are zero) and a `miss` when they "
        "disagree."
    )
    parts.append("")
    parts.append(
        "Caption: Proposer hypothesis vs tournament outcome — pass-rate "
        "and drift-loss Δ, predicted vs actual."
    )
    parts.append("")
    parts.append(render_hypothesis_outcome_table(data))
    parts.append("")
    parts.append("### Drift-kind movements")
    parts.append("")
    parts.append(
        "For every challenger round that produced drift movements, the "
        "first table pairs the from-rate with the to-rate for each drift "
        "kind and states the signed rate Δ. The second table threads the "
        "per-kind rates along the promoted lineage."
    )
    parts.append("")
    parts.append("Caption: Per-challenger drift-kind rate movements, from-rate vs to-rate.")
    parts.append("")
    parts.append(render_challenger_drift_table(data))
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
        "Each cell is the per-entry Δ scalar of the challenger against "
        "the round's champion; a positive value is worse and a negative "
        "one better. Columns marked 'cached' reused the previous "
        "champion's evaluation rather than re-running it."
    )
    parts.append("")
    parts.append(
        "Caption: Per-board entry Δ scalar across challenger generations. "
        "A dash marks an entry with no per-entry breakdown recorded."
    )
    parts.append("")
    parts.append(render_per_board_delta_table(data))
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
    ratio. It opens with a bold ``KEY OBSERVATION`` label.

    The number is :attr:`~EpochReportData.final_scalar`, which is
    champion-anchored — so an epoch that promoted nothing is headlined
    as having *held*, and the last rejected challenger's counterfactual
    appears (when there is one) under its own label rather than under
    the lineage's.

    Conservative: emits the empty string when there are no challengers
    yet (no story to tell).
    """
    if data.attempted == 0:
        return ""
    final = data.final_scalar
    direction = "improved" if final < 0 else ("regressed" if final > 0 else "held")
    sentence = (
        "**KEY OBSERVATION.** "
        f"The promoted lineage's cumulative scalar has {direction} to "
        f"`{final:+.3f}` after {data.attempted} challenger "
        f"{'generation' if data.attempted == 1 else 'generations'} "
        f"({data.promoted} promoted, {data.rejected} rejected). "
        f"A lower scalar is better; the trajectory table lists the path."
    )
    counterfactual = data.latest_rejected_scalar
    if data.promoted == 0 and counterfactual is not None:
        # With nothing promoted the headline number is the baseline, so the
        # only measured movement in the epoch is on a path the gate refused.
        # It is worth reporting — under a label that says whose it is.
        sentence += (
            f" The most recent rejected challenger measured `{counterfactual:+.3f}`"
            f" — a path not taken."
        )
    return sentence


# ---------------------------------------------------------------------------
# Statistical integrity (validity)
# ---------------------------------------------------------------------------


def _round_records(data: EpochReportData) -> list[Any]:
    """The folded per-round records, or an empty list when none were read."""
    return [r for r in data.round_records if r is not None]


def render_statistical_integrity_section(data: EpochReportData) -> str:
    """Render ``## Statistical Integrity`` — the validity differentiator.

    Folds the durable per-round event records: pre-tournament screen
    veto/confirm counts, evidence-gate replication/deferral statistics,
    Ladder holdout confirmations, and any placebo-arm outcome (a promoted
    placebo is a CRITICAL callout). Every sub-claim degrades honestly:
    when no round records were folded (no round has settled yet) or the
    measure's feature was disabled for the epoch, each measure renders an
    honest one-liner naming the true cause rather than a fabricated
    number, and the section still frames the guarantees the contract
    makes.
    """
    parts: list[str] = []
    parts.append("## Statistical Integrity")
    parts.append("")
    records = _round_records(data)

    # Placebo arm — the single most important integrity signal. A promoted
    # placebo means the gate crowned noise.
    placebo_promoted = 0
    placebo_seen = 0
    for r in records:
        prov = getattr(r, "decision_provenance", {}) or {}
        if isinstance(prov, dict) and prov.get("placebo"):
            placebo_seen += 1
            if getattr(r, "decision", "") == "promoted":
                placebo_promoted += 1
    if placebo_promoted:
        parts.append(
            f"**CRITICAL.** A PLACEBO arm was PROMOTED in {placebo_promoted} "
            f"{'round' if placebo_promoted == 1 else 'rounds'}. A placebo carries "
            "no real change, so a promotion means the gate crowned noise — treat "
            "every promotion this epoch as suspect until the floor is re-measured."
        )
        parts.append("")
    elif placebo_seen:
        parts.append(
            f"**Placebo arms.** {placebo_seen} placebo "
            f"{'arm was' if placebo_seen == 1 else 'arms were'} run and none was "
            "promoted — the gate rejected the no-op change, as it should."
        )
        parts.append("")

    # Screen veto / confirm.
    screened = sum(int(getattr(r.proposal, "candidates_screened", 0)) for r in records)
    vetoes = sum(int(getattr(r.proposal, "screen_vetoes", 0)) for r in records)
    if screened:
        parts.append(
            f"**Pre-tournament screen.** {screened} slate "
            f"{'candidate was' if screened == 1 else 'candidates were'} screened; "
            f"{vetoes} vetoed before the tournament, {screened - vetoes} advanced."
        )
    elif records:
        parts.append(
            "**Pre-tournament screen.** No screen events were recorded for this "
            "epoch — pre-tournament screening was not enabled."
        )
    else:
        parts.append(
            "**Pre-tournament screen.** No round has settled for this epoch yet, "
            "so there are no screen events to report."
        )
    parts.append("")

    # Evidence-gate replication / deferral.
    evidence_rounds = sum(1 for r in records if getattr(r, "evidence_trail", ()))
    replicates = sum(len(getattr(r, "evidence_trail", ())) for r in records)
    deferred_rounds = sum(1 for r in records if getattr(r, "decision", "") == "deferred")
    if replicates or deferred_rounds:
        parts.append(
            f"**Evidence gate.** {replicates} replicate "
            f"{'refit' if replicates == 1 else 'refits'} across {evidence_rounds} "
            f"{'round' if evidence_rounds == 1 else 'rounds'}; {deferred_rounds} "
            f"{'round was' if deferred_rounds == 1 else 'rounds were'} deferred for "
            "more evidence rather than crowned or rejected on a single duel."
        )
    elif records:
        parts.append(
            "**Evidence gate.** No replicate-duel or deferral events were recorded "
            "for this epoch — the evidence gate did not fire (every round settled on "
            "a single duel)."
        )
    else:
        parts.append(
            "**Evidence gate.** No round has settled for this epoch yet, so there "
            "are no evidence-gate events to report."
        )
    parts.append("")

    # Ladder holdout confirmations.
    holdout_conf = sum(1 for r in records if getattr(r, "holdout", None) and r.holdout.confirmed)
    holdout_rel = sum(1 for r in records if getattr(r, "holdout", None) is not None)
    if holdout_rel:
        parts.append(
            f"**Ladder holdout.** The holdout-confirmation bit was released in "
            f"{holdout_rel} {'round' if holdout_rel == 1 else 'rounds'}; "
            f"{holdout_conf} confirmed the crowning on held-out entries."
        )
    elif records:
        parts.append(
            "**Ladder holdout.** No holdout-confirmation events were recorded for "
            "this epoch — holdout confirmation was not enabled."
        )
    else:
        parts.append(
            "**Ladder holdout.** No round has settled for this epoch yet, so there "
            "are no holdout-confirmation events to report."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Proposer analytics
# ---------------------------------------------------------------------------


def _hypothesis_calibration(data: EpochReportData) -> tuple[int, int]:
    """Return ``(hits, total)`` for the proposer's pass-rate predictions.

    A completed challenger with a parseable ``expected_pass_rate_delta``
    contributes one comparison; it is a HIT when the predicted direction
    matches the realised ``pass_rate_delta`` sign (a flat prediction hits a
    flat outcome). This is the same projection the hypothesis-vs-outcome
    table uses, reduced to a scalar calibration fraction.
    """
    hits = 0
    total = 0
    for g in data.generations:
        if g.is_baseline or g.decision not in ("promoted", "rejected"):
            continue
        predicted = _parse_expected_pass_rate_delta(g.expected_pass_rate_delta)
        if predicted is None:
            continue
        total += 1
        actual = g.pass_rate_delta
        pred_sign = (predicted > 0) - (predicted < 0)
        act_sign = (actual > 0) - (actual < 0)
        if pred_sign == act_sign:
            hits += 1
    return hits, total


def render_proposer_analytics_section(data: EpochReportData) -> str:
    """Render ``## Proposer Analytics``.

    The hypothesis-calibration fraction (predicted vs realised pass-rate
    direction), plus the slate / selection-mode mix folded from the per-
    round records. The calibration is computed from data always present in
    the generation views; the slate mix degrades honestly when the per-
    round event log is absent.
    """
    parts: list[str] = []
    parts.append("## Proposer Analytics")
    parts.append("")
    hits, total = _hypothesis_calibration(data)
    if total:
        frac = hits / total
        parts.append(
            f"**Hypothesis calibration.** The proposer's predicted pass-rate "
            f"direction matched the realised movement in {hits} of {total} "
            f"completed challengers ({frac:.0%}). A low fraction means the "
            "proposer is guessing at outcomes it cannot yet foresee; a high one "
            "means its hypotheses are load-bearing."
        )
    else:
        parts.append(
            "**Hypothesis calibration.** No completed challenger carried a "
            "parseable pass-rate prediction, so calibration cannot be scored for "
            "this epoch."
        )
    parts.append("")

    records = _round_records(data)
    recombined = sum(int(getattr(r.proposal, "recombined_sampled", 0)) for r in records)
    sampled = sum(int(getattr(r.proposal, "candidates_sampled", 0)) for r in records)
    if sampled:
        parts.append(
            f"**Slate mix.** {sampled} candidate "
            f"{'was' if sampled == 1 else 'were'} sampled across the recorded "
            f"rounds; {recombined} came from mechanical recombination of rejected "
            "parents rather than a fresh proposer sample."
        )
    elif records:
        parts.append(
            "**Slate mix.** The recorded rounds carried no per-candidate sampling "
            "counts; the slate width frozen for the epoch is reported under the "
            "Methodology's proposer configuration."
        )
    else:
        parts.append(
            "**Slate mix.** No round has settled for this epoch yet, so the slate "
            "mix cannot be summarised; the slate width frozen for the epoch is "
            "reported under the Methodology's proposer configuration."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Threats to validity
# ---------------------------------------------------------------------------


def render_threats_section(data: EpochReportData) -> str:
    """Render ``## Threats to Validity & Limitations``.

    Parameterised by the epoch's measurable scale — board size,
    generation count, judge coverage — so the caveats reflect the
    actual run rather than boilerplate.
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
        "narrows coverage of the system under test's behaviour space; a "
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
    "render_statistical_integrity_section",
    "render_proposer_analytics_section",
    "render_threats_section",
]
