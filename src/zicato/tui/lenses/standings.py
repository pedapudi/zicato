"""Standings — the structure view: who is racing, who is ahead, by how much.

The dashboard draws brackets and ladders; a terminal draws the same model as an
aligned table with the rung structure carried by indentation. Two shapes:

* **gauntlet** (the default): one row per champion-vs-challenger match-up, with
  the Δscalar as a signed, champion-anchored bar against the promote margin.
* **swiss / racing / elimination**: the served standings, with each round or
  rung an indented group, so the field narrowing is legible as shape.

Payloads: ``/api/tournaments``, ``/api/active-tournament``,
``/api/tournament-structure/{epoch}/{tournament}``, ``/api/lineage``.
"""

from __future__ import annotations

from typing import Any

from zicato.tui import glyphs, present
from zicato.tui.client import Client
from zicato.tui.lenses.base import (
    DECISION_STYLE,
    LensContext,
    as_dict,
    as_list,
    decision_span,
    evidence,
    missing,
    rating_scale,
    rating_spans,
)
from zicato.tui.view import Block, Span, View, columns, digest_of, pad, row, rpad

#: Δscalar at full bar deflection. A margin larger than this is clamped and
#: flagged, never silently truncated.
BAR_SCALE = 0.10

STATUS_STYLE = {
    "promoted": "good",
    "won": "good",
    "survived": "good",
    "rejected": "bad",
    "cut": "bad",
    "eliminated": "bad",
    "racing": "accent",
    "in_flight": "accent",
    "pending": "faint",
}


class StandingsLens:
    name = "standings"
    title = "Standings"
    label = "Standings"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        bracket = as_dict(client.get("/api/tournaments"))
        epoch_id = ctx.epoch or bracket.get("epoch_id")
        if not epoch_id:
            return missing(
                StandingsLens.title,
                "no epoch to show standings for",
                hint="open an epoch with `zicato evolve`, or pass --view /e/<epoch>/gens",
            )
        structure = str(bracket.get("structure") or "gauntlet")
        lineage = as_list(as_dict(client.get("/api/lineage")).get("generations"))

        if structure == "gauntlet":
            blocks, digest_parts = _gauntlet(bracket, lineage, ctx)
        else:
            resolved = _resolve(client, bracket, epoch_id)
            if resolved is None:
                return missing(
                    StandingsLens.title,
                    f"no {structure} tournament recorded for {epoch_id} yet",
                    hint="the field appears once the first round is seeded",
                )
            blocks, digest_parts = _structured(resolved, lineage, ctx)

        return View(
            title=f"Standings · {structure}",
            subtitle=_params_line(bracket),
            blocks=tuple(blocks),
            digest=digest_of("standings", epoch_id, structure, digest_parts),
            meta={"epoch_id": epoch_id, "structure": structure},
        )


def _params_line(bracket: dict[str, Any]) -> str | None:
    params = as_dict(bracket.get("structure_params"))
    if not params:
        return None
    return " · ".join(f"{k}={v}" for k, v in sorted(params.items()))


def _resolve(client: Client, bracket: dict[str, Any], epoch_id: str) -> dict[str, Any] | None:
    """Live tournament if one is in flight for this epoch, else the record.

    Live-first, exactly as the browser resolves it: a run in flight governs the
    topology, so the ladder fills in round by round instead of showing a stale
    settled bracket. A foreign epoch's live payload is ignored.
    """
    live_raw = as_dict(client.get("/api/active-tournament"))
    if live_raw.get("epoch_id") == epoch_id:
        live = present.normalize_structure(live_raw, True)
        if live and live["live"]:
            return live

    tournaments = as_list(bracket.get("tournaments"))
    for record in reversed(tournaments):
        tid = record.get("tournament_id") if isinstance(record, dict) else None
        if not tid:
            continue
        payload = client.get(f"/api/tournament-structure/{epoch_id}/{tid}")
        normalized = present.normalize_structure(payload, False)
        if normalized:
            return normalized
    if live_raw.get("epoch_id") == epoch_id:
        return present.normalize_structure(live_raw, False)
    return None


# ---------------------------------------------------------------------------
# gauntlet
# ---------------------------------------------------------------------------


def _gauntlet(
    bracket: dict[str, Any], lineage: list[Any], ctx: LensContext
) -> tuple[list[Block], Any]:
    matchups = [m for m in as_list(bracket.get("matchups")) if isinstance(m, dict)]
    if not matchups:
        return (
            [
                Block(
                    title="Match-ups",
                    rows=(row("empty", ("no match-up has been raced in this epoch", "faint")),),
                )
            ],
            [],
        )
    by_gen = {g.get("generation_id"): g for g in lineage if isinstance(g, dict)}
    scale = rating_scale(
        [by_gen[m["challenger"]] for m in matchups if m.get("challenger") in by_gen]
    )

    cells = [["challenger", "vs champion", "Δscalar", "margin", "decision", "rating"]] + [
        [
            present.truncate(m.get("challenger"), 20, fallback=present.NULL),
            present.truncate(m.get("champion"), 20, fallback=present.NULL),
            present.fmt_signed(m.get("delta_scalar"), 3),
            "",
            present.verdict_label(present.decision_of(m) or "pending"),
            present.rating_text(by_gen.get(m.get("challenger"))),
        ]
        for m in matchups
    ]
    widths = columns(cells)

    rows = [
        row(
            "head",
            (pad(cells[0][0], widths[0]), "faint"),
            (pad(cells[0][1], widths[1]), "faint"),
            (rpad(cells[0][2], widths[2]), "faint"),
            (pad("  " + cells[0][3], 12), "faint"),
            (pad(cells[0][4], widths[4]), "faint"),
            (cells[0][5], "faint"),
        )
    ]
    for m, cell in zip(matchups, cells[1:], strict=True):
        decision = present.decision_of(m) or "pending"
        challenger = m.get("challenger")
        record = by_gen.get(challenger)
        delta = m.get("delta_scalar")
        rows.append(
            row(
                f"m:{challenger}",
                (pad(cell[0], widths[0]), DECISION_STYLE.get(decision, "plain")),
                (pad(cell[1], widths[1]), "faint"),
                (rpad(cell[2], widths[2]), _delta_style(delta)),
                Span("  "),
                (
                    glyphs.margin_bar(
                        _negated(delta),
                        scale=BAR_SCALE,
                        ascii_only=ctx.ascii_only,
                    ),
                    _delta_style(delta),
                ),
                Span("  "),
                (pad(cell[4], widths[4]), DECISION_STYLE.get(decision, "plain")),
                *rating_spans(record, ascii_only=ctx.ascii_only, scale=scale),
                evidence=evidence(
                    what=f"{challenger} against champion {m.get('champion')}",
                    measured=f"Δscalar {present.fmt_signed(delta, 4)} (lower loss is better)",
                    uncertainty=present.rating_text(record, games=True),
                    decision=(
                        present.verdict_label(decision)
                        + (f" — {m['rejection_reason']}" if m.get("rejection_reason") else "")
                    ),
                    provenance=(
                        "/api/tournaments matchups · " f"ran_at {m.get('ran_at') or present.NULL}"
                    ),
                ),
                action=f"candidate/{challenger}" if challenger else None,
                selectable=True,
            )
        )
    note = (
        "bar is champion-anchored: right = the challenger improved on the champion"
        if not ctx.narrow
        else None
    )
    digest = [
        [
            m.get("challenger"),
            m.get("champion"),
            present.decision_of(m),
            present.fmt(m.get("delta_scalar"), 4),
            m.get("rejection_reason"),
        ]
        for m in matchups
    ]
    return [Block(title="Match-ups", rows=tuple(rows), note=note)], digest


def _negated(delta: Any) -> Any:
    """Flip Δscalar's sign for the bar: lower loss is better, so right = better.

    The number itself is NEVER flipped — only the bar's direction, and the
    block's note says so. A signed number that silently changed meaning between
    the column and the glyph beside it would be the worst kind of quiet lie.
    """
    return -float(delta) if present.is_num(delta) else None


def _delta_style(delta: Any) -> str:
    if not present.is_num(delta):
        return "faint"
    return "good" if float(delta) < 0 else "bad" if float(delta) > 0 else "plain"


# ---------------------------------------------------------------------------
# swiss / racing / elimination
# ---------------------------------------------------------------------------


def _structured(
    st: dict[str, Any], lineage: list[Any], ctx: LensContext
) -> tuple[list[Block], Any]:
    blocks: list[Block] = []
    by_gen = {g.get("generation_id"): g for g in lineage if isinstance(g, dict)}
    standings = [s for s in st["standings"] if isinstance(s, dict)]
    scale = rating_scale(
        [by_gen[s["generation_id"]] for s in standings if s.get("generation_id") in by_gen]
    )

    if standings:
        blocks.append(_standings_block(standings, by_gen, scale, ctx, live=st["live"]))
    rounds = [r for r in st["rounds"] if isinstance(r, dict)]
    if rounds:
        blocks.append(_rounds_block(rounds, ctx))
    field = [f for f in st["field_status"] if isinstance(f, dict)]
    if field:
        blocks.append(_field_block(field))
    if not blocks:
        blocks.append(
            Block(
                title=str(st["structure"]),
                rows=(row("seeding", ("the field is being seeded", "faint")),),
            )
        )
    digest = [
        st["structure"],
        st["live"],
        st["phase"],
        [
            [
                s.get("generation_id"),
                s.get("rank"),
                present.fmt(s.get("scalar"), 4),
                s.get("wins"),
                s.get("losses"),
                s.get("points"),
                s.get("status"),
                bool(s.get("in_flight")),
                present.fmt(s.get("projected_scalar"), 4),
            ]
            for s in standings
        ],
        [
            [
                r.get("round_index"),
                r.get("label"),
                [
                    [
                        m.get("match_id"),
                        "/".join(str(c) for c in as_list(m.get("competitors"))),
                        m.get("winner"),
                        m.get("decision"),
                        m.get("bye"),
                        "/".join(str(c) for c in as_list(m.get("survivors"))),
                        "/".join(str(c) for c in as_list(m.get("cut"))),
                    ]
                    for m in as_list(r.get("matches"))
                    if isinstance(m, dict)
                ],
            ]
            for r in rounds
        ],
        [[f.get("generation_id"), f.get("status"), f.get("reason")] for f in field],
    ]
    return blocks, digest


def _standings_block(
    standings: list[dict[str, Any]],
    by_gen: dict[Any, Any],
    scale: tuple[float, float] | None,
    ctx: LensContext,
    *,
    live: bool,
) -> Block:
    header = ["#", "generation", "scalar", "W-L", "status", "rating"]
    body = [
        [
            str(s.get("rank") if s.get("rank") is not None else present.NULL),
            present.truncate(s.get("generation_id"), 22, fallback=present.NULL),
            _scalar_text(s),
            _record_text(s),
            str(s.get("status") or ("racing" if s.get("in_flight") else present.NULL)),
            present.rating_text(by_gen.get(s.get("generation_id"))),
        ]
        for s in standings
    ]
    widths = columns([header, *body])
    rows = [
        row(
            "head",
            *[(pad(h, widths[i]), "faint") for i, h in enumerate(header)],
        )
    ]
    for s, cell in zip(standings, body, strict=True):
        gen = s.get("generation_id")
        status = str(s.get("status") or ("in_flight" if s.get("in_flight") else "pending"))
        rows.append(
            row(
                f"s:{gen}",
                (rpad(cell[0], widths[0]), "faint"),
                (pad(cell[1], widths[1]), STATUS_STYLE.get(status, "plain")),
                (rpad(cell[2], widths[2]), "plain"),
                Span("  "),
                (pad(cell[3], widths[3]), "plain"),
                (pad(cell[4], widths[4]), STATUS_STYLE.get(status, "plain")),
                *rating_spans(by_gen.get(gen), ascii_only=ctx.ascii_only, scale=scale),
                evidence=evidence(
                    what=f"{gen} at rank {cell[0]}",
                    measured=f"scalar {_scalar_text(s)} · record {_record_text(s)}",
                    uncertainty=present.rating_text(by_gen.get(gen), games=True),
                    decision=status,
                    provenance="served standings (never re-derived client-side)",
                ),
                action=f"candidate/{gen}" if gen else None,
                selectable=True,
            )
        )
    note = "~ marks a projected scalar: the competitor is still racing" if live else None
    return Block(title="Standings" + (" · live" if live else ""), rows=tuple(rows), note=note)


def _scalar_text(s: dict[str, Any]) -> str:
    """The settled scalar, or the projection marked as one.

    A projected value is prefixed ``~`` so it can never be read as settled —
    the browser dashes the same value for the same reason.
    """
    if present.is_num(s.get("scalar")):
        return present.fmt(s["scalar"], 3)
    if s.get("in_flight") and present.is_num(s.get("projected_scalar")):
        return "~" + present.fmt(s["projected_scalar"], 3)
    return present.NULL


def _record_text(s: dict[str, Any]) -> str:
    wins, losses, draws = s.get("wins"), s.get("losses"), s.get("draws")
    if not present.is_num(wins) and not present.is_num(losses):
        points = s.get("points")
        return present.fmt(points, 1) if present.is_num(points) else present.NULL
    parts = f"{wins or 0}-{losses or 0}"
    if present.is_num(draws) and draws:
        parts += f"-{draws}"
    return parts


def _rounds_block(rounds: list[dict[str, Any]], ctx: LensContext) -> Block:
    """Each round or rung an indented group — the field narrowing as shape."""
    rows = []
    for r in rounds:
        index = r.get("round_index")
        label = r.get("label") or (f"round {index}" if index is not None else "round")
        rows.append(row(f"r:{index}", (str(label), "bold")))
        matches = [m for m in as_list(r.get("matches")) if isinstance(m, dict)]
        if not matches:
            rows.append(row(f"r:{index}:empty", ("(no committed match)", "faint"), indent=1))
            continue
        for m in matches:
            competitors = " vs ".join(
                present.truncate(c, 18) for c in as_list(m.get("competitors"))
            )
            winner = m.get("winner")
            decision = m.get("decision")
            cut = as_list(m.get("cut"))
            survivors = as_list(m.get("survivors"))
            suffix = []
            if m.get("bye"):
                suffix.append("bye")
            if winner:
                suffix.append(f"won by {present.truncate(winner, 18)}")
            if cut:
                suffix.append(("cut " if ctx.ascii_only else "✂ ") + ", ".join(str(c) for c in cut))
            rows.append(
                row(
                    f"m:{m.get('match_id')}",
                    (competitors or present.NULL, "plain"),
                    Span("  "),
                    (" · ".join(suffix), "faint") if suffix else None,
                    Span("  "),
                    decision_span(str(decision)) if decision else None,
                    indent=1,
                    evidence=evidence(
                        what=f"match {m.get('match_id')}",
                        measured=competitors or present.NULL,
                        uncertainty=(
                            f"survivors {', '.join(str(s) for s in survivors)}"
                            if survivors
                            else present.NULL
                        ),
                        decision=str(decision or winner or "unresolved"),
                        provenance="served rounds[].matches[]",
                    ),
                    selectable=True,
                )
            )
    return Block(title="Rounds", rows=tuple(rows))


def _field_block(field: list[dict[str, Any]]) -> Block:
    """The proposing outcomes — which challengers made it into the field at all."""
    rows = []
    for f in field:
        status = str(f.get("status") or present.NULL)
        rows.append(
            row(
                f"f:{f.get('generation_id')}",
                (
                    pad(present.truncate(f.get("generation_id"), 22, fallback=present.NULL), 24),
                    "plain",
                ),
                (pad(status, 12), STATUS_STYLE.get(status, "plain")),
                (present.truncate(f.get("reason") or "", 46), "faint"),
                evidence=evidence(
                    what=f"proposed challenger {f.get('generation_id')}",
                    measured="mutations "
                    + (", ".join(str(m) for m in as_list(f.get("mutation_ids"))) or present.NULL),
                    uncertainty=present.NULL,
                    decision=status,
                    provenance="served field_status[]",
                ),
                selectable=True,
            )
        )
    return Block(title="Proposed field", rows=tuple(rows))


__all__ = ["BAR_SCALE", "StandingsLens"]
