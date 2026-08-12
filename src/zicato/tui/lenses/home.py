"""Home — the loop verdict, the champion, and the live round.

The three things an operator wants in the first second: is the loop learning
anything, who is champion, and what is happening right now. Everything else is
one keystroke away.

Payloads: ``/api/workspace``, ``/api/epoch``, ``/api/epoch/{id}/trajectory``,
``/api/epoch/{id}/cost``, ``/api/live/pipeline``, ``/api/lineage``.
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
from zicato.tui.view import Block, Span, View, digest_of, row

#: The loop-verdict phrase's severity style. The phrase itself always prints,
#: so the colour is redundant encoding — which is the rule.
VERDICT_STYLE = {"nosignal": "warn", "stalled": "warn", "plateau": "warn"}

#: The live pipeline's step states, mapped to a style. ``running`` is the one
#: the accent is spent on: it is the answer to "what is happening now".
STEP_STYLE = {
    "running": "accent",
    "done": "plain",
    "complete": "plain",
    "failed": "bad",
    "error": "bad",
    "skipped": "faint",
    "pending": "faint",
}


class HomeLens:
    name = "home"
    title = "Home"
    label = "Home"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        workspace = as_dict(client.get("/api/workspace"))
        epoch_id = ctx.epoch or workspace.get("current_epoch_id")
        if not epoch_id:
            return missing(
                HomeLens.title,
                "this workspace has no epoch yet",
                hint="run `zicato evolve` to open one",
            )
        epoch = as_dict(client.get(f"/api/epoch?epoch={epoch_id}"))
        traj = as_dict(client.get(f"/api/epoch/{epoch_id}/trajectory"))
        cost = as_dict(client.get(f"/api/epoch/{epoch_id}/cost"))
        pipeline = as_dict(client.get("/api/live/pipeline"))
        lineage = as_list(as_dict(client.get("/api/lineage")).get("generations"))

        blocks = [
            _loop_block(traj, cost, ctx),
            _champion_block(epoch, lineage, ctx),
            _round_block(pipeline, ctx),
        ]
        return View(
            title=f"{epoch_id}",
            subtitle=_subtitle(epoch),
            blocks=tuple(blocks),
            digest=digest_of(
                "home",
                epoch_id,
                _loop_digest(traj, cost),
                epoch.get("current_champion"),
                epoch.get("closed"),
                _champion_rating_digest(epoch, lineage),
                _pipeline_digest(pipeline),
            ),
            meta={"epoch_id": epoch_id},
        )


def _subtitle(epoch: dict[str, Any]) -> str | None:
    goal = epoch.get("goal") or epoch.get("brief")
    closed = " · closed" if epoch.get("closed") else ""
    contract = epoch.get("contract_hash")
    parts = []
    if goal:
        parts.append(present.truncate(goal, 70))
    if contract:
        parts.append(f"contract {present.truncate(contract, 12)}")
    return (" · ".join(parts) + closed) if parts else (closed.strip(" ·") or None)


def _loop_block(traj: dict[str, Any], cost: dict[str, Any], ctx: LensContext) -> Block:
    """The loop-communication panel: is this loop learning anything?"""
    verdict = present.loop_verdict(traj)
    points = [p.get("scalar") for p in as_list(traj.get("points"))]
    floor = as_dict(traj.get("noise_floor"))
    movement = traj.get("recent_movement")

    rows = []
    if verdict is None:
        raw = traj.get("verdict")
        rows.append(
            row(
                "verdict",
                ("verdict".ljust(LABEL_WIDTH), "faint"),
                (str(raw) if raw else present.NULL, "plain"),
                evidence=evidence(
                    what="the loop-communication verdict",
                    measured=f"promotions {present.promotion_rate_label(traj) or present.NULL}",
                    uncertainty=_floor_text(floor),
                    decision=str(raw or present.NULL),
                    provenance="/api/epoch/{id}/trajectory",
                ),
                selectable=True,
            )
        )
    else:
        rows.append(
            row(
                "verdict",
                ("verdict".ljust(LABEL_WIDTH), "faint"),
                (verdict.word, VERDICT_STYLE.get(verdict.cls, "warn")),
                evidence=evidence(
                    what="the loop-communication verdict",
                    measured=f"promotions {present.promotion_rate_label(traj) or present.NULL}",
                    uncertainty=_floor_text(floor),
                    decision=verdict.word,
                    provenance="/api/epoch/{id}/trajectory",
                ),
                selectable=True,
            )
        )

    rows.append(
        row(
            "trend",
            ("loss trend".ljust(LABEL_WIDTH), "faint"),
            (glyphs.sparkline(points, ascii_only=ctx.ascii_only), "accent"),
            Span("  "),
            (f"{len(points)} settled · lower is better", "faint"),
            evidence=evidence(
                what="the champion spine's scalar loss, oldest to newest",
                measured=f"latest {present.fmt(points[-1], 3) if points else present.NULL}",
                uncertainty=_floor_text(floor),
                decision="shape only — a sparkline carries no axis",
                provenance="/api/epoch/{id}/trajectory points[].scalar",
            ),
            selectable=True,
        )
    )
    rows.append(
        kv_row("promotions", "promotions", present.promotion_rate_label(traj) or present.NULL)
    )
    # Cost-per-promotion is undefined until something has been promoted —
    # dividing by zero promotions is not a missing measurement.
    promoted = present.num(traj.get("promoted_count")) or 0
    rows.append(
        kv_row(
            "cost",
            "cost per promotion",
            present.cost_per_promotion_label(cost)
            or (present.NULL if promoted else present.unmeasured("nothing has been promoted yet")),
        )
    )
    # THE FOUR ABSENCES. Each of these three can be "nothing" for a DIFFERENT
    # reason, and the operator's next action differs in each case, so they must
    # not collapse into one em-dash.
    #
    # `recent_movement` is null with fewer than two settled scalars: the window
    # is defined, the sample is too small — measured-impossible, not missing.
    settled = len([p for p in points if present.is_num(p)])
    rows.append(
        kv_row(
            "movement",
            "recent movement",
            present.measured(movement, digits=4, enough=settled >= 2),
        )
    )
    # A null A/A floor means the calibration was never RUN — measurable, not
    # measured. The reason is the actionable half of the fact.
    rows.append(kv_row("floor", "noise floor (A/A)", _floor_text(floor)))
    rows.append(
        kv_row(
            "plateau",
            "plateau measurable",
            "yes" if traj.get("plateau_measurable") else "no",
            style="plain" if traj.get("plateau_measurable") else "faint",
        )
    )
    return Block(title="Loop", rows=tuple(rows))


def _floor_text(floor: dict[str, Any]) -> str:
    """The A/A floor as ``±0.0123 · 6 runs``, or the THIRD verdict.

    An absent floor is not a missing number — it is a measurement nobody has
    taken yet, and every verdict that depends on it ("no detectable signal")
    is unavailable until they do. Saying so, with the reason, is the whole
    point of the third verdict.
    """
    half = floor.get("max_abs_delta")
    if not present.is_num(half):
        return present.unmeasured("run the A/A calibration to measure it")
    runs = floor.get("runs")
    tail = f" · {runs} runs" if present.is_num(runs) else ""
    return f"±{present.fmt(half, 4)}{tail}"


def _champion_block(epoch: dict[str, Any], lineage: list[Any], ctx: LensContext) -> Block:
    """Who reigns, on what evidence."""
    champion_id = epoch.get("current_champion")
    if not champion_id:
        return Block(
            title="Champion",
            rows=(row("champion", ("no champion yet — nothing has raced", "faint")),),
        )
    record = next(
        (g for g in lineage if isinstance(g, dict) and g.get("generation_id") == champion_id),
        {},
    )
    summary = as_dict(epoch.get("delta_scalar_summary"))
    experiments = as_list(epoch.get("experiments"))
    exp = next(
        (e for e in experiments if isinstance(e, dict) and e.get("generation_id") == champion_id),
        {},
    )
    decision = present.decision_for(
        parent=record.get("parent_generation_id"),
        promoted=record.get("promoted"),
        exp=exp,
    )
    rows = [
        row(
            "champion",
            (present.truncate(champion_id, 32), "accent"),
            Span("  "),
            decision_span(decision),
            evidence=evidence(
                what=f"generation {champion_id}",
                measured=(
                    "champion-spine Δscalar "
                    f"{present.fmt_signed(summary.get('champion_spine'), 4)}"
                ),
                uncertainty=present.rating_text(record, games=True),
                decision=present.verdict_label(decision),
                provenance="/api/epoch current_champion + /api/lineage",
            ),
            selectable=True,
        ),
        row(
            "champion-rating",
            ("rating".ljust(LABEL_WIDTH), "faint"),
            *rating_spans(record, ascii_only=ctx.ascii_only),
        ),
        kv_row(
            "spine",
            "Δscalar (spine)",
            present.fmt_signed(summary.get("champion_spine"), 4),
        ),
        kv_row("gross", "Δscalar (gross)", present.fmt_signed(summary.get("gross"), 4)),
        kv_row("board", "board entries", str(len(as_list(epoch.get("board")))) or present.NULL),
    ]
    return Block(title="Champion", rows=tuple(rows))


def _round_block(pipeline: dict[str, Any], ctx: LensContext) -> Block:
    """The live round lifeline — one static, glanceable strip."""
    running = bool(pipeline.get("running"))
    stale = bool(pipeline.get("stale"))
    steps = [s for s in as_list(pipeline.get("steps")) if isinstance(s, dict)]
    active = pipeline.get("active_step")

    if not steps:
        stage_spans = glyphs.lifeline(
            active if isinstance(active, str) else None, ascii_only=ctx.ascii_only
        )
    else:
        stage_spans = []
        for i, step in enumerate(steps):
            if i:
                stage_spans.append((" > " if ctx.ascii_only else " ▸ ", "faint"))
            label = str(step.get("label") or step.get("id") or "?")
            state = str(step.get("state") or "pending")
            stage_spans.append((label, STEP_STYLE.get(state, "plain")))

    rows = [
        row("lifeline", *[Span(text, style) for text, style in stage_spans]),
    ]
    if not running:
        rows.append(row("idle", ("no round in flight", "faint")))
    else:
        detail = next(
            (
                str(s.get("detail") or "")
                for s in steps
                if s.get("id") == active or s.get("state") == "running"
            ),
            "",
        )
        rows.append(
            kv_row(
                "phase",
                "phase",
                str(pipeline.get("phase") or present.NULL),
                style="accent",
            )
        )
        rows.append(kv_row("round", "round", str(pipeline.get("round_index") or present.NULL)))
        rows.append(kv_row("inflight", "runs in flight", str(pipeline.get("in_flight") or 0)))
        if detail:
            rows.append(kv_row("detail", "detail", present.truncate(detail, 60)))
    if stale:
        rows.append(row("stale", ("heartbeat is stale — this may not be live", "warn")))
    if pipeline.get("decision"):
        rows.append(kv_row("decision", "last decision", str(pipeline["decision"])))
    return Block(title="Live round", rows=tuple(rows))


def _loop_digest(traj: dict[str, Any], cost: dict[str, Any]) -> list[Any]:
    """``ui.js`` ``loopStatsDigest``, plus the trend the terminal also paints."""
    floor = as_dict(traj.get("noise_floor"))
    return [
        str(traj.get("verdict")) if traj.get("verdict") else None,
        present.fmt(traj.get("promotion_rate"), 3)
        if present.is_num(traj.get("promotion_rate"))
        else None,
        traj.get("challenger_count") if present.is_num(traj.get("challenger_count")) else None,
        traj.get("promoted_count") if present.is_num(traj.get("promoted_count")) else None,
        present.fmt(floor.get("max_abs_delta"), 4)
        if present.is_num(floor.get("max_abs_delta"))
        else None,
        round(float(cost["cost_per_promotion_ms"]))
        if present.is_num(cost.get("cost_per_promotion_ms"))
        else None,
        [present.fmt(p.get("scalar"), 4) for p in as_list(traj.get("points"))],
        present.fmt(traj.get("recent_movement"), 4),
        bool(traj.get("plateau_measurable")),
    ]


def _champion_rating_digest(epoch: dict[str, Any], lineage: list[Any]) -> Any:
    champion_id = epoch.get("current_champion")
    record = next(
        (g for g in lineage if isinstance(g, dict) and g.get("generation_id") == champion_id),
        None,
    )
    model = present.rating_model(record)
    summary = as_dict(epoch.get("delta_scalar_summary"))
    return [
        [model.elo, model.se, model.games, model.provisional] if model else None,
        present.fmt(summary.get("champion_spine"), 4),
        present.fmt(summary.get("gross"), 4),
        len(as_list(epoch.get("board"))),
    ]


def _pipeline_digest(pipeline: dict[str, Any]) -> Any:
    """Fold only what the strip PAINTS — never ``generated_at``.

    ``generated_at`` changes on every heartbeat whether or not anything moved;
    folding it in would make every no-op beat look like a change, which is
    exactly the flicker this discipline exists to prevent.
    """
    return [
        bool(pipeline.get("running")),
        bool(pipeline.get("stale")),
        pipeline.get("phase"),
        pipeline.get("round_index"),
        pipeline.get("active_step"),
        pipeline.get("in_flight"),
        pipeline.get("decision"),
        [
            [s.get("id"), s.get("label"), s.get("state"), s.get("detail")]
            for s in as_list(pipeline.get("steps"))
            if isinstance(s, dict)
        ],
    ]


__all__ = ["HomeLens"]
