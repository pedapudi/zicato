"""Instrument: reflection evidence, board quality and recommendation remedies.

The service supplies findings, recorded evidence and proposed operations. The
terminal displays those facts, with the existing CLI apply commands as text.
It does not execute commands or write workspace records.
"""

from __future__ import annotations

from dataclasses import replace
from shlex import quote
from typing import Any

from zicato.tui import present
from zicato.tui.client import Client, ServiceError
from zicato.tui.lenses.base import (
    LensContext,
    as_dict,
    as_list,
    evidence,
    kv_row,
    missing,
)
from zicato.tui.lenses.review import board_review, content_view, record_rows
from zicato.tui.routes import Route
from zicato.tui.view import Block, Span, View, columns, pad, row, rpad

#: The severity mark. Redundant with the severity word beside it, which always
#: prints — so a NO_COLOR, ASCII terminal loses no information.
_MARK = {"bad": "!", "warn": "*", "good": "+", "faint": "-"}


def _or_null(value: Any) -> str:
    """A count as text — ``0`` when it is genuinely zero, ``—`` when absent.

    ``value or NULL`` would turn a real zero into an em-dash, which is the
    inverse of the rule and just as dishonest.
    """
    return present.NULL if value is None else str(value)


class InstrumentLens:
    name = "instrument"
    title = "Instrument"
    label = "Instrument"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        if ctx.route.params.get("detail") in {"board", "boards", "evals"}:
            return board_review(client, ctx.route, ascii_only=ctx.ascii_only)
        epoch_id = ctx.epoch
        board_link = Block(
            rows=(
                row(
                    "board",
                    ("Board status and outcome distribution", "plain"),
                    action=Route(
                        "instrument",
                        {"detail": "evals", **({"epoch": epoch_id} if epoch_id else {})},
                    ).to_path(),
                    selectable=True,
                ),
            )
        )
        proposer = _proposer_blocks(client, epoch_id)
        query = f"?epoch={epoch_id}" if epoch_id else ""
        listing = as_list(as_dict(client.get(f"/api/reflections{query}")).get("reflections"))
        reflection_id = ctx.route.params.get("reflection")
        if reflection_id is None:
            latest = _latest(listing)
            reflection_id = latest.get("reflection_id") if latest else None
        if reflection_id is None:
            return content_view(
                InstrumentLens.title,
                [board_link, *proposer],
                degraded="No reflection is recorded; board evidence remains available.",
            )

        summary = as_dict(client.get(f"/api/reflection/{reflection_id}/summary"))
        if not summary.get("found"):
            return missing(
                InstrumentLens.title,
                f"reflection {reflection_id} is not in this workspace",
                hint=str(summary.get("note") or "press 3 to choose a recorded reflection"),
            )
        scorecards = as_dict(client.get(f"/api/reflection/{reflection_id}/scorecards"))
        practices = as_dict(client.get(f"/api/reflection/{reflection_id}/practices"))
        findings = [f for f in as_list(summary.get("findings")) if isinstance(f, dict)]

        blocks = [
            board_link,
            _header_block(summary),
            _pillars_block(summary),
            _findings_block(findings, reflection_id),
            _queue_block(findings, reflection_id),
            _judges_block(scorecards),
            _practices_block(practices),
            _listing_block(listing, reflection_id),
            *proposer,
        ]
        return replace(
            content_view(
                f"Instrument · {reflection_id}",
                [b for b in blocks if b.rows],
                subtitle=_subtitle(summary),
            ),
            meta={"reflection_id": reflection_id, "epoch_id": summary.get("epoch_id")},
        )


def _proposer_blocks(client: Client, epoch_id: str | None) -> list[Block]:
    """Show the served pending remedies; commands remain plain text."""
    try:
        queue = as_dict(client.get("/api/proposer/recommendations"))
    except ServiceError as exc:
        return [Block(title="Proposer recommendations", note=str(exc))]
    rows = []
    for item in as_list(queue.get("pending")):
        if not isinstance(item, dict):
            continue
        finding_id = item.get("finding_id")
        owner = item.get("epoch_id")
        if epoch_id and owner != epoch_id:
            continue
        key = f"{owner}:{item.get('reflection_id')}:{finding_id}"
        rows.extend(record_rows(key, item))
        if finding_id and owner:
            command = (
                f"zicato proposer apply-recommendation {quote(str(finding_id))} "
                f"--epoch {quote(str(owner))}"
            )
            rows.append(row(f"apply:{key}", (command, "accent"), selectable=True))
    if not rows:
        rows.append(
            row(
                "empty",
                (
                    "No pending recommendations for this selection."
                    if queue.get("found") is True
                    else "Recommendation evidence is unavailable.",
                    "faint",
                ),
            )
        )
    return [
        Block(
            title="Proposer recommendations",
            rows=tuple(rows),
            note="Review the recorded remedy before applying; "
            "run an apply command only after reviewing its remedy.",
        )
    ]


def _latest(listing: list[Any]) -> dict[str, Any] | None:
    """The most recent EXECUTED reflection, else the most recent of any.

    A planned-but-unrun reflection has no findings; landing on one by default
    would show an empty screen and read as "the instrument is healthy".
    """
    records = [r for r in listing if isinstance(r, dict)]
    if not records:
        return None
    executed = [r for r in records if r.get("executed")]
    pool = executed or records
    return max(pool, key=lambda r: str(r.get("created_at") or ""))


def _subtitle(summary: dict[str, Any]) -> str:
    state = "executed" if summary.get("executed") else "planned (not run)"
    return (
        f"{summary.get('epoch_id') or present.NULL} · {summary.get('mode') or present.NULL} · "
        f"{state} · {summary.get('created_at') or present.NULL}"
    )


def _header_block(summary: dict[str, Any]) -> Block:
    rows = [
        kv_row(
            "floor",
            "noise floor",
            present.fmt(summary.get("noise_floor_max_abs_delta"), 4),
        ),
        kv_row(
            "flip",
            "decision flip P",
            present.fmt(summary.get("decision_flip_p"), 3),
        ),
        kv_row(
            "tiers",
            "fidelity tiers",
            ", ".join(str(t) for t in as_list(summary.get("fidelity_tiers"))) or present.NULL,
        ),
    ]
    return Block(title="Reflection", rows=tuple(rows))


def _pillars_block(summary: dict[str, Any]) -> Block:
    pillars = as_dict(summary.get("pillars"))
    if not pillars:
        return Block()
    rows = []
    for name, value in sorted(pillars.items()):
        record = as_dict(value)
        verdict = str(record.get("verdict") or value)
        tone = present.practice_tone(verdict)
        rows.append(
            row(
                f"pillar:{name}",
                (pad(str(name), 24), "plain"),
                (pad(verdict, 12), tone),
                (present.truncate(record.get("detail") or "", 46), "faint"),
                evidence=evidence(
                    what=f"pillar {name}",
                    measured=str(record.get("detail") or present.NULL),
                    uncertainty=present.NULL,
                    decision=verdict,
                    provenance="served reflection summary pillars",
                ),
                selectable=True,
            )
        )
    return Block(title="Pillars", rows=tuple(rows))


def _findings_block(findings: list[dict[str, Any]], reflection_id: str) -> Block:
    if not findings:
        return Block(
            title="Findings",
            rows=(
                row(
                    "none",
                    (
                        "no findings — the instrument reads healthy on every pillar "
                        "this reflection measured",
                        "faint",
                    ),
                ),
            ),
        )
    rows = []
    for f in findings:
        tone = present.severity_tone(f.get("severity"))
        ev = [e for e in as_list(f.get("evidence")) if isinstance(e, dict)]
        rows.append(
            row(
                f"finding:{f.get('finding_id')}",
                (_MARK.get(tone, "-") + " ", tone),
                (pad(str(f.get("severity") or "info"), 10), tone),
                (str(f.get("title") or f.get("finding_id") or present.NULL), "bold"),
                evidence=evidence(
                    what=str(f.get("title") or f.get("finding_id")),
                    measured=str(f.get("detail") or present.NULL),
                    uncertainty=_evidence_summary(ev),
                    decision=str(f.get("severity") or "info"),
                    provenance=f"reflection {reflection_id} finding {f.get('finding_id')}",
                ),
                selectable=True,
            )
        )
        if f.get("detail"):
            rows.append(
                row(
                    f"detail:{f.get('finding_id')}",
                    (present.truncate(f["detail"], 84), "faint"),
                    indent=1,
                )
            )
        if ev:
            rows.append(
                row(
                    f"ev:{f.get('finding_id')}",
                    ("evidence · ", "faint"),
                    (_evidence_summary(ev), "faint"),
                    indent=1,
                )
            )
    return Block(title="Findings", rows=tuple(rows))


def _evidence_summary(ev: list[dict[str, Any]]) -> str:
    if not ev:
        return present.NULL
    parts = []
    for e in ev[:4]:
        label = " ".join(str(x) for x in (e.get("verdict"), e.get("span") or e.get("run_ref")) if x)
        parts.append(label or str(e.get("run_ref") or "?"))
    if len(ev) > 4:
        parts.append(f"+{len(ev) - 4} more")
    return " · ".join(parts)


def _apply_command(f: dict[str, Any], reflection_id: str) -> str | None:
    """The CLI invocation that applies this finding — printed, never run."""
    op = as_dict(f.get("proposed_op")).get("op")
    finding_id = f.get("finding_id")
    if not op or not finding_id:
        return None
    return f"zicato inspect reflection apply {quote(reflection_id)} {quote(str(finding_id))}"


def _queue_block(findings: list[dict[str, Any]], reflection_id: str) -> Block:
    """Board findings with a proposed operation and its operator command."""
    queued = [f for f in findings if as_dict(f.get("proposed_op")).get("op")]
    if not queued:
        return Block()
    rows = []
    for f in queued:
        op = as_dict(f.get("proposed_op"))
        args = ", ".join(as_dict(op.get("args")).keys())
        command = _apply_command(f, reflection_id) or ""
        rows.append(
            row(
                f"queue:{f.get('finding_id')}",
                (pad(present.truncate(f"{op.get('op')}({args})", 36), 36), "plain"),
                (command, "accent"),
                evidence=evidence(
                    what=f"proposed op {op.get('op')}",
                    measured=f"args {args or present.NULL}",
                    uncertainty=_evidence_summary(
                        [e for e in as_list(f.get("evidence")) if isinstance(e, dict)]
                    ),
                    decision=f"recommendation from {f.get('finding_id')}",
                    provenance="board reflection (recommend-only; the CLI applies)",
                ),
                selectable=True,
            )
        )
        rows.extend(record_rows(f"operation:{f.get('finding_id')}", op, indent=1))
    return Block(
        title="Recommendation queue",
        rows=tuple(rows),
        note="copy an apply line and run it yourself — this console applies nothing",
    )


def _judges_block(scorecards: dict[str, Any]) -> Block:
    judges = [j for j in as_list(scorecards.get("judges")) if isinstance(j, dict)]
    if not judges:
        return Block()
    header = ["judge", "P", "R", "kappa", "disagree", "TP/FP/FN/TN", "exercised"]
    body = [
        [
            present.truncate(j.get("judge_name"), 24, fallback=present.NULL),
            present.fmt(j.get("precision"), 2),
            present.fmt(j.get("recall"), 2),
            present.fmt(j.get("self_consistency_kappa"), 2),
            present.fmt(j.get("disagreement_rate"), 2),
            "/".join(
                str(j.get(k) if j.get(k) is not None else present.NULL)
                for k in ("tp", "fp", "fn", "tn")
            ),
            "yes" if j.get("exercised") else "no",
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
                *[(rpad(c, widths[i + 1]), "plain") for i, c in enumerate(cell[1:5])],
                (pad("  " + cell[5], widths[5] + 2), "faint"),
                (cell[6], "plain" if j.get("exercised") else "warn"),
                evidence=evidence(
                    what=f"judge {j.get('judge_name')}",
                    measured=f"precision {cell[1]} · recall {cell[2]} · confusion {cell[5]}",
                    uncertainty=(
                        f"self-consistency kappa {cell[3]} · disagreement {cell[4]} · "
                        f"ambiguous {_or_null(j.get('ambiguous'))}"
                    ),
                    decision="exercised" if j.get("exercised") else "NEVER EXERCISED by this board",
                    provenance="served judge scorecards (counts only; evidence rides on findings)",
                ),
                selectable=True,
            )
        )
    return Block(title="Judge audit", rows=tuple(rows))


def _practices_block(practices: dict[str, Any]) -> Block:
    checks = [c for c in as_list(practices.get("checks")) if isinstance(c, dict)]
    counts = as_dict(practices.get("verdict_counts"))
    if not checks and not counts:
        return Block()
    rows = []
    if counts:
        rows.append(
            row(
                "counts",
                *[Span(f"{k} {v}   ", present.practice_tone(k)) for k, v in counts.items()],
            )
        )
    for c in checks:
        # The served keys are PracticeCheck.to_json's: the check's identity is
        # ``check_id``, its measured sentence is ``headline``, and its doctrine
        # grounding is ``rationale``. The dashboard's own practice row renders
        # the same three, so the two renderers show one payload the same way.
        check_id = str(c.get("check_id") or "")
        verdict = str(c.get("verdict") or "unmeasured")
        headline = str(c.get("headline") or check_id)
        rows.append(
            row(
                f"check:{check_id}",
                (pad(present.truncate(headline, 32, fallback=present.NULL), 34), "plain"),
                (pad(verdict, 12), present.practice_tone(verdict)),
                (present.truncate(str(c.get("rationale") or ""), 36), "faint"),
                evidence=evidence(
                    what=f"practice {check_id or present.NULL}",
                    # The headline carries the measurement with its numbers
                    # inline, so it is the measured statement.
                    measured=headline or present.NULL,
                    # An ``unmeasured`` check names the input it lacked; every
                    # other verdict has no uncertainty to state here.
                    uncertainty=str(c.get("unmeasured_reason") or present.NULL),
                    decision=verdict,
                    provenance="served practice review",
                ),
                selectable=True,
            )
        )
    return Block(title="Practices", rows=tuple(rows))


def _listing_block(listing: list[Any], current: str) -> Block:
    records = [r for r in listing if isinstance(r, dict)]
    if len(records) < 2:
        return Block()
    rows = []
    for r in sorted(records, key=lambda x: str(x.get("created_at") or ""), reverse=True):
        rid = str(r.get("reflection_id"))
        rows.append(
            row(
                f"refl:{rid}",
                (pad(rid, 28), "accent" if rid == current else "plain"),
                (pad(str(r.get("mode") or present.NULL), 12), "faint"),
                (pad("executed" if r.get("executed") else "planned", 10), "faint"),
                (
                    f"{_or_null(r.get('n_findings'))} findings",
                    "faint",
                ),
                action=f"instrument/{rid}",
                selectable=True,
            )
        )
    return Block(title="Reflections", rows=tuple(rows))


__all__ = ["InstrumentLens"]
