"""Candidate — the dossier: gate evidence, facets, per-board rows, lineage.

The one screen that answers "why did this candidate win or lose?". The promote
gate's rules are shown as the SHORT-CIRCUITING ladder the server records, in
order, with the deciding rule marked — not as a summary verdict, because the
summary is exactly the part an operator would want to check.

Payloads: ``/api/lineage``, ``/api/epoch``,
``/api/round/{epoch}/{champion}/{challenger}/gate``,
``/api/generation/{epoch}/{gen}/per-entry``,
``/api/generation/{epoch}/{gen}/per-judge``.
"""

from __future__ import annotations

from typing import Any

from zicato.tui import glyphs, present
from zicato.tui.client import Client
from zicato.tui.lenses.base import (
    LABEL_WIDTH,
    LensContext,
    as_dict,
    as_list,
    decision_span,
    evidence,
    kv_row,
    missing,
    rating_spans,
)
from zicato.tui.lenses.standings import BAR_SCALE
from zicato.tui.view import Block, Span, View, columns, digest_of, pad, row, rpad

#: Rule status to style. The status word always prints beside it.
RULE_STYLE = {
    "pass": "good",
    "passed": "good",
    "fail": "bad",
    "failed": "bad",
    "not_reached": "faint",
    "skipped": "faint",
    "pending": "faint",
    "info": "plain",
}


class CandidateLens:
    name = "candidate"
    title = "Candidate"
    label = "Candidate"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        lineage = as_list(as_dict(client.get("/api/lineage")).get("generations"))
        gen_id = ctx.route.params.get("gen")
        epoch_id = ctx.epoch
        if gen_id is None:
            # No generation addressed: land on the most recent one rather than
            # an empty screen, and say which one was chosen.
            latest = lineage[-1] if lineage else None
            gen_id = latest.get("generation_id") if isinstance(latest, dict) else None
        if gen_id is None:
            return missing(
                CandidateLens.title,
                "no candidate to show",
                hint="pass --view candidate/<generation-id>, or press enter on a standings row",
            )
        record = next(
            (g for g in lineage if isinstance(g, dict) and g.get("generation_id") == gen_id),
            None,
        )
        if record is None:
            return missing(
                CandidateLens.title,
                f"generation {gen_id} is not in this workspace's lineage",
                hint="check the id, or press 2 for the standings",
            )
        epoch_id = epoch_id or record.get("epoch_id")
        epoch = as_dict(client.get(f"/api/epoch?epoch={epoch_id}"))
        exp = next(
            (
                e
                for e in as_list(epoch.get("experiments"))
                if isinstance(e, dict) and e.get("generation_id") == gen_id
            ),
            {},
        )
        parent = record.get("parent_generation_id") or exp.get("parent_generation_id")
        gate = (
            as_dict(client.get(f"/api/round/{epoch_id}/{parent}/{gen_id}/gate")) if parent else {}
        )
        per_entry = as_dict(client.get(f"/api/generation/{epoch_id}/{gen_id}/per-entry"))
        per_judge = as_dict(client.get(f"/api/generation/{epoch_id}/{gen_id}/per-judge"))

        decision = present.decision_for(
            parent=parent, promoted=record.get("promoted"), exp=exp, gate=gate
        )
        blocks = [
            _identity_block(gen_id, record, exp, decision, ctx),
            _gate_block(gate, ctx),
            _facet_block(per_entry),
            _judge_block(per_judge),
            _entries_block(per_entry, ctx),
            _lineage_block(lineage, gen_id, ctx),
        ]
        return View(
            title=f"Candidate · {gen_id}",
            subtitle=_subtitle(record, exp, parent),
            blocks=tuple(b for b in blocks if b.rows),
            digest=digest_of(
                "candidate",
                gen_id,
                decision,
                _gate_digest(gate),
                _entry_digest(per_entry),
                [
                    [
                        j.get("judge_name"),
                        present.fmt(j.get("weighted_loss"), 4),
                        j.get("run_count"),
                    ]
                    for j in as_list(per_judge.get("judges"))
                    if isinstance(j, dict)
                ],
                present.rating_text(record, games=True),
            ),
            meta={"epoch_id": epoch_id, "generation_id": gen_id},
        )


def _subtitle(record: dict[str, Any], exp: dict[str, Any], parent: Any) -> str:
    born = record.get("created_at") or exp.get("proposed_at") or present.NULL
    lineage = f"from {parent}" if parent else "seed (no parent)"
    return f"{lineage} · {born}"


def _identity_block(
    gen_id: str,
    record: dict[str, Any],
    exp: dict[str, Any],
    decision: str,
    ctx: LensContext,
) -> Block:
    rows = [
        row(
            "decision",
            ("decision".ljust(LABEL_WIDTH), "faint"),
            decision_span(decision),
            evidence=evidence(
                what=f"generation {gen_id}",
                measured=f"outcome {as_dict(exp.get('outcome')) or present.NULL}",
                uncertainty=present.rating_text(record, games=True),
                decision=present.verdict_label(decision),
                provenance="/api/lineage promoted + the stamped experiment decision",
            ),
            selectable=True,
        ),
        row(
            "rating",
            ("rating".ljust(LABEL_WIDTH), "faint"),
            *rating_spans(record, ascii_only=ctx.ascii_only),
        ),
    ]
    hypothesis = exp.get("hypothesis") or as_dict(exp.get("outcome")).get("hypothesis")
    if isinstance(hypothesis, str) and hypothesis:
        rows.append(kv_row("hypothesis", "hypothesis", present.truncate(hypothesis, 68)))
    patches = exp.get("patches")
    if isinstance(patches, dict) and patches:
        rows.append(kv_row("patches", "patched files", str(len(patches))))
    return Block(title="Candidate", rows=tuple(rows))


def _gate_block(gate: dict[str, Any], ctx: LensContext) -> Block:
    """The promote gate: headline numbers, then the short-circuiting ladder."""
    if not gate:
        return Block(
            title="Gate", rows=(row("nogate", ("this candidate has not met the gate", "faint")),)
        )

    delta = gate.get("delta_scalar")
    delta_num = present.num(delta)
    margin = present.num(gate.get("margin"))
    rows = [
        row(
            "gate-decision",
            ("decision".ljust(LABEL_WIDTH), "faint"),
            decision_span(present.decision_of(gate) or "pending"),
            Span("   "),
            (present.truncate(gate.get("reason") or "", 50), "faint"),
            evidence=evidence(
                what="the promote gate for this pair",
                measured=(
                    f"Δscalar {present.fmt_signed(delta, 4)} · "
                    f"champion {present.fmt(gate.get('champion_scalar'), 3)} · "
                    f"challenger {present.fmt(gate.get('challenger_scalar'), 3)}"
                ),
                uncertainty=_rating_evidence(as_dict(gate.get("rating"))),
                decision=present.verdict_label(present.decision_of(gate) or "pending"),
                provenance="/api/round/{epoch}/{champion}/{challenger}/gate",
            ),
            selectable=True,
        ),
        row(
            "gate-margin",
            ("Δscalar".ljust(LABEL_WIDTH), "faint"),
            (present.fmt_signed(delta, 4), _delta_style(delta)),
            Span("  "),
            (
                glyphs.margin_bar(
                    -delta_num if delta_num is not None else None,
                    scale=BAR_SCALE,
                    threshold=margin,
                    ascii_only=ctx.ascii_only,
                ),
                _delta_style(delta),
            ),
            Span("  "),
            (f"promote margin {present.fmt(margin, 4)}" if margin is not None else "", "faint"),
        ),
        kv_row("champ-scalar", "champion scalar", present.fmt(gate.get("champion_scalar"), 3)),
        kv_row(
            "chall-scalar",
            "challenger scalar",
            present.fmt(gate.get("challenger_scalar"), 3),
        ),
        kv_row("pass-rate", "Δ pass rate", present.fmt_signed(gate.get("delta_pass_rate"), 3)),
    ]
    driver = as_dict(gate.get("primary_driver")).get("judge")
    if driver:
        rows.append(kv_row("driver", "primary driver", str(driver)))
    regressed = gate.get("regressed_predicate") or gate.get("regressed_namespace")
    if regressed:
        rows.append(kv_row("regressed", "regressed", str(regressed), style="bad"))

    rating = as_dict(gate.get("rating"))
    if rating.get("present"):
        rows.extend(_rating_rows(rating, ctx))

    override = as_dict(gate.get("override"))
    if override.get("present"):
        action = "forced↑" if override.get("action") == "promote" else "forced✕"
        rows.append(
            row(
                "override",
                (f"{action} · operator", "warn"),
                Span("  "),
                (present.truncate(override.get("reason") or "", 48), "faint"),
            )
        )

    rules = [r for r in as_list(gate.get("rules")) if isinstance(r, dict)]
    deciding = gate.get("deciding_rule")
    for i, rule in enumerate(rules):
        status = str(rule.get("status") or "pending")
        is_deciding = rule.get("id") == deciding
        rows.append(
            row(
                f"rule:{rule.get('id') or i}",
                (("→ " if not ctx.ascii_only else "> ") if is_deciding else "  ", "accent"),
                (
                    pad(str(rule.get("label") or rule.get("id") or "?"), 34),
                    "bold" if is_deciding else "plain",
                ),
                (pad(status.replace("_", " "), 12), RULE_STYLE.get(status, "plain")),
                (present.truncate(rule.get("detail") or "", 44), "faint"),
                evidence=evidence(
                    what=f"gate rule {rule.get('id')}",
                    measured=str(rule.get("detail") or present.NULL),
                    uncertainty=present.NULL,
                    decision=status + (" · deciding" if is_deciding else ""),
                    provenance="served gate.rules[] — evaluated in order, short-circuiting",
                ),
                selectable=True,
            )
        )
    note = "rules short-circuit in order; → marks the one that decided" if rules else None
    return Block(title="Gate", rows=tuple(rows), note=note)


def _rating_rows(rating: dict[str, Any], ctx: LensContext) -> list[Any]:
    """The Bradley-Terry pre-gate: two θ̂ intervals and P(challenger stronger).

    Drawn on ONE shared axis so the overlap between the two intervals is
    visible — which is the whole question a confidence-thresholded run asks.
    """
    champion = as_dict(rating.get("champion"))
    challenger = as_dict(rating.get("challenger"))
    bounds = [
        n
        for n in (
            present.num(block.get(key))
            for block in (champion, challenger)
            for key in ("ci_lo", "ci_hi")
        )
        if n is not None
    ]
    scale = (min(bounds), max(bounds)) if len(bounds) >= 2 and min(bounds) < max(bounds) else None
    rows = []
    for label, block in (("champion θ", champion), ("challenger θ", challenger)):
        if not block:
            continue
        rows.append(
            row(
                f"theta:{label}",
                (pad(label, LABEL_WIDTH - 2), "faint"),
                (rpad(present.fmt(block.get("theta"), 3), 8), "plain"),
                Span("  "),
                (
                    glyphs.whisker(
                        block.get("ci_lo"),
                        block.get("theta"),
                        block.get("ci_hi"),
                        scale=scale,
                        width=17,
                        ascii_only=ctx.ascii_only,
                    ),
                    "faint",
                ),
                Span("  "),
                (
                    f"±{present.fmt(block.get('se'), 3)}"
                    if present.is_num(block.get("se"))
                    else present.NULL,
                    "faint",
                ),
            )
        )
    p_stronger = rating.get("p_stronger")
    if present.is_num(p_stronger):
        threshold = rating.get("threshold")
        rows.append(
            kv_row(
                "p-stronger",
                "P(challenger stronger)",
                present.fmt(p_stronger, 3)
                + (f" · needs ≥ {present.fmt(threshold, 3)}" if present.is_num(threshold) else ""),
            )
        )
    return rows


def _rating_evidence(rating: dict[str, Any]) -> str:
    if not rating.get("present"):
        return present.NULL
    p = rating.get("p_stronger")
    return (
        f"P(challenger stronger) {present.fmt(p, 3)}"
        if present.is_num(p)
        else "Bradley-Terry rating present"
    )


def _facet_block(per_entry: dict[str, Any]) -> Block:
    """Per-facet re-aggregation at the epoch's frozen weights."""
    facets = as_dict(as_dict(per_entry.get("facet_scores")).get("facets"))
    overall = as_dict(per_entry.get("facet_scores")).get("overall")
    if not facets:
        return Block()
    rows = []
    header = ["facet", "scalar", "mean", "scored"]
    body = [
        [
            name,
            present.fmt(as_dict(f).get("scalar"), 3),
            present.fmt(as_dict(f).get("mean_score"), 3),
            f"{as_dict(f).get('scored_count')}/{as_dict(f).get('entry_count')}",
        ]
        for name, f in sorted(facets.items())
    ]
    if isinstance(overall, dict):
        body.append(
            [
                "overall",
                present.fmt(overall.get("scalar"), 3),
                present.fmt(overall.get("mean_score"), 3),
                f"{overall.get('scored_count')}/{overall.get('entry_count')}",
            ]
        )
    widths = columns([header, *body])
    rows.append(row("head", *[(pad(h, widths[i]), "faint") for i, h in enumerate(header)]))
    for cell in body:
        rows.append(
            row(
                f"facet:{cell[0]}",
                (pad(cell[0], widths[0]), "bold" if cell[0] == "overall" else "plain"),
                (rpad(cell[1], widths[1]), "plain"),
                (rpad(cell[2], widths[2]), "plain"),
                (rpad(cell[3], widths[3]), "faint"),
                evidence=evidence(
                    what=f"facet {cell[0]}",
                    measured=f"scalar {cell[1]} · mean score {cell[2]}",
                    uncertainty=f"{cell[3]} entries scored",
                    decision="comparable to the overall row: same frozen weights",
                    provenance="served facet_scores",
                ),
                selectable=True,
            )
        )
    return Block(title="Facets", rows=tuple(rows))


def _judge_block(per_judge: dict[str, Any]) -> Block:
    judges = [j for j in as_list(per_judge.get("judges")) if isinstance(j, dict)]
    if not judges:
        return Block()
    header = ["judge", "weighted", "raw", "weight", "runs"]
    body = [
        [
            present.truncate(j.get("judge_name"), 26, fallback=present.NULL),
            present.fmt(j.get("weighted_loss"), 4),
            present.fmt(j.get("raw_loss"), 4),
            present.fmt(j.get("weight"), 2),
            str(j.get("run_count") if j.get("run_count") is not None else present.NULL),
        ]
        for j in judges
    ]
    widths = columns([header, *body])
    rows = [row("head", *[(pad(h, widths[i]), "faint") for i, h in enumerate(header)])]
    for j, cell in zip(judges, body, strict=True):
        rows.append(
            row(
                f"judge:{j.get('judge_name')}",
                (pad(cell[0], widths[0]), "plain"),
                *[(rpad(c, widths[i + 1]), "plain") for i, c in enumerate(cell[1:])],
                evidence=evidence(
                    what=f"judge {j.get('judge_name')}",
                    measured=f"weighted {cell[1]} · raw {cell[2]} at weight {cell[3]}",
                    uncertainty=f"over {cell[4]} runs",
                    decision="a loss term, not a verdict",
                    provenance="/api/generation/{epoch}/{gen}/per-judge",
                ),
                selectable=True,
            )
        )
    return Block(title="Judges", rows=tuple(rows))


def _entries_block(per_entry: dict[str, Any], ctx: LensContext) -> Block:
    entries = [e for e in as_list(per_entry.get("entries")) if isinstance(e, dict)]
    if not entries:
        note = per_entry.get("note")
        return Block(
            title="Board",
            rows=(
                row(
                    "noentries",
                    (str(note or "no per-board rows recorded for this candidate"), "faint"),
                ),
            ),
        )
    header = ["entry", "drift loss", "pass", "runtime", "rung"]
    body = [
        [
            present.truncate(e.get("entry_id"), 28, fallback=present.NULL),
            present.fmt(e.get("drift_loss"), 4),
            _pass_text(e.get("pass_fail"), ctx),
            present.fmt_duration_ms(e.get("runtime_ms")),
            str(e.get("rung") or present.NULL),
        ]
        for e in entries
    ]
    widths = columns([header, *body])
    rows = [row("head", *[(pad(h, widths[i]), "faint") for i, h in enumerate(header)])]
    for e, cell in zip(entries, body, strict=True):
        exceeded = bool(e.get("wall_clock_budget_exceeded"))
        rows.append(
            row(
                f"entry:{e.get('entry_id')}",
                (pad(cell[0], widths[0]), "plain"),
                (rpad(cell[1], widths[1]), "plain"),
                (pad("  " + cell[2], widths[2] + 2), _pass_style(e.get("pass_fail"))),
                (rpad(cell[3], widths[3]), "warn" if exceeded else "plain"),
                (pad(cell[4], widths[4]), "faint"),
                evidence=evidence(
                    what=f"board entry {e.get('entry_id')}",
                    measured=f"drift loss {cell[1]} in {cell[3]}",
                    uncertainty=(
                        "wall-clock budget exceeded — this run was cut short"
                        if exceeded
                        else present.NULL
                    ),
                    decision=f"predicate {cell[2]}",
                    provenance=(
                        f"run {e.get('run_id') or present.NULL} · "
                        f"match {e.get('match_id') or present.NULL}"
                    ),
                ),
                selectable=True,
            )
        )
    mean = per_entry.get("mean_score")
    note = f"board mean score {present.fmt(mean, 3)}" if present.is_num(mean) else None
    return Block(title="Board", rows=tuple(rows), note=note)


def _pass_text(value: Any, ctx: LensContext) -> str:
    if value is None:
        return present.NULL
    if ctx.ascii_only:
        return "y" if value else "x"
    return "✓" if value else "✕"


def _pass_style(value: Any) -> str:
    if value is None:
        return "faint"
    return "good" if value else "bad"


def _lineage_block(lineage: list[Any], gen_id: str, ctx: LensContext) -> Block:
    """The ancestry strip: seed to this candidate, one row per hop."""
    by_gen = {g.get("generation_id"): g for g in lineage if isinstance(g, dict)}
    chain: list[dict[str, Any]] = []
    cursor: Any = gen_id
    seen: set[Any] = set()
    while cursor in by_gen and cursor not in seen:
        seen.add(cursor)
        record = by_gen[cursor]
        chain.append(record)
        cursor = record.get("parent_generation_id")
    chain.reverse()
    if len(chain) < 2:
        return Block()
    arrow = " > " if ctx.ascii_only else " → "
    spans: list[Any] = []
    for i, record in enumerate(chain):
        if i:
            spans.append(Span(arrow, "faint"))
        gid = str(record.get("generation_id"))
        style = "accent" if gid == gen_id else ("good" if record.get("promoted") else "faint")
        spans.append(Span(present.truncate(gid, 16), style))
    return Block(title="Lineage", rows=(row("lineage", *spans),))


def _delta_style(delta: Any) -> str:
    if not present.is_num(delta):
        return "faint"
    return "good" if float(delta) < 0 else "bad" if float(delta) > 0 else "plain"


def _gate_digest(gate: dict[str, Any]) -> Any:
    if not gate:
        return None
    rating = as_dict(gate.get("rating"))
    return [
        present.decision_of(gate),
        present.fmt(gate.get("delta_scalar"), 4),
        present.fmt(gate.get("margin"), 4),
        present.fmt(gate.get("champion_scalar"), 4),
        present.fmt(gate.get("challenger_scalar"), 4),
        present.fmt(gate.get("delta_pass_rate"), 4),
        gate.get("deciding_rule"),
        gate.get("reason"),
        gate.get("regressed_predicate") or gate.get("regressed_namespace"),
        as_dict(gate.get("primary_driver")).get("judge"),
        [
            [r.get("id"), r.get("label"), r.get("status"), r.get("detail")]
            for r in as_list(gate.get("rules"))
            if isinstance(r, dict)
        ],
        [
            rating.get("present"),
            present.fmt(rating.get("p_stronger"), 4),
            present.fmt(as_dict(rating.get("champion")).get("theta"), 4),
            present.fmt(as_dict(rating.get("challenger")).get("theta"), 4),
        ],
        [
            as_dict(gate.get("override")).get("present"),
            as_dict(gate.get("override")).get("action"),
            as_dict(gate.get("override")).get("reason"),
        ],
    ]


def _entry_digest(per_entry: dict[str, Any]) -> Any:
    return [
        present.fmt(per_entry.get("mean_score"), 4),
        [
            [
                e.get("entry_id"),
                present.fmt(e.get("drift_loss"), 4),
                e.get("pass_fail"),
                e.get("rung"),
                e.get("wall_clock_budget_exceeded"),
            ]
            for e in as_list(per_entry.get("entries"))
            if isinstance(e, dict)
        ],
        sorted(as_dict(as_dict(per_entry.get("facet_scores")).get("facets")).keys()),
    ]


__all__ = ["CandidateLens"]
