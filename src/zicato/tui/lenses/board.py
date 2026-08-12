"""Board — the instrument's own health: split, rotation, holdout, outcomes.

The board is the measuring instrument, and this lens asks whether it can still
measure: is the train/holdout split honest, is the contract due for rotation,
what is the smallest effect this board could detect at all, and which entries
are dead (never discriminate) or noisy (flip under replication).

The per-entry outcome distribution renders as a compact heat-strip — one cell
per candidate, shaded by pass ratio — so a row of "always passes" and a row of
"flips constantly" are told apart at a glance.

Payloads: ``/api/epoch``, ``/api/epoch/{id}/eval-health``,
``/api/epoch/{id}/evals``.
"""

from __future__ import annotations

from typing import Any

from zicato.tui import present
from zicato.tui.client import Client
from zicato.tui.lenses.base import (
    LABEL_WIDTH,
    LensContext,
    as_dict,
    as_list,
    evidence,
    kv_row,
    missing,
)
from zicato.tui.view import Block, Span, View, columns, digest_of, pad, row, rpad

#: The heat-strip ramp, low pass-ratio to high. A cell the candidate never ran
#: is a SPACE-holding dot, never a zero — an unrun cell is not a failure.
_HEAT = " ░▒▓█"
_HEAT_ASCII = ".:-=#"
_ABSENT = "·"
_ABSENT_ASCII = " "


class BoardLens:
    name = "board"
    title = "Board"
    label = "Board"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        epoch = as_dict(client.get("/api/epoch" + (f"?epoch={ctx.epoch}" if ctx.epoch else "")))
        epoch_id = epoch.get("epoch_id") or ctx.epoch
        if not epoch_id:
            return missing(
                BoardLens.title,
                "no epoch to show a board for",
                hint="open an epoch with `zicato evolve`",
            )
        health = as_dict(client.get(f"/api/epoch/{epoch_id}/eval-health"))
        evals = as_dict(client.get(f"/api/epoch/{epoch_id}/evals"))

        blocks = [
            _split_block(epoch, health),
            _rotation_block(health),
            _power_block(health),
            _entries_block(epoch, evals, ctx),
            _flagged_block(health),
        ]
        return View(
            title=f"Board · {epoch_id}",
            subtitle=_subtitle(epoch),
            blocks=tuple(b for b in blocks if b.rows),
            degraded=None
            if health.get("found")
            else "no evaluation health recorded for this epoch",
            digest=digest_of(
                "board",
                epoch_id,
                _split_digest(epoch),
                _health_digest(health),
                _evals_digest(evals),
            ),
            meta={"epoch_id": epoch_id},
        )


def _subtitle(epoch: dict[str, Any]) -> str:
    board = as_list(epoch.get("board"))
    split = as_dict(epoch.get("board_split"))
    if split.get("enabled"):
        return (
            f"{len(board)} entries · {split.get('train_count')} train / "
            f"{split.get('holdout_count')} holdout"
        )
    return f"{len(board)} entries · no holdout split"


def _split_block(epoch: dict[str, Any], health: dict[str, Any]) -> Block:
    split = as_dict(epoch.get("board_split"))
    holdout = as_dict(epoch.get("holdout"))
    budget = as_dict(health.get("holdout_budget"))
    rows = [
        kv_row(
            "configured",
            "split configured",
            "yes" if split.get("configured") else "no",
            style="plain" if split.get("configured") else "faint",
        ),
        kv_row(
            "enabled",
            "split enabled",
            "yes" if split.get("enabled") else "no",
            style="good" if split.get("enabled") else "warn",
        ),
        kv_row("fraction", "holdout fraction", present.fmt(split.get("holdout_fraction"), 2)),
        kv_row(
            "tags",
            "holdout tags",
            ", ".join(str(t) for t in as_list(split.get("holdout_tags"))) or present.NULL,
        ),
        kv_row(
            "counts",
            "train / holdout",
            f"{split.get('train_count', present.NULL)} / "
            f"{split.get('holdout_count', present.NULL)}",
        ),
    ]
    if holdout:
        rows.append(
            kv_row(
                "train-scalar", "latest train scalar", present.fmt(holdout.get("train_scalar"), 3)
            )
        )
        rows.append(
            kv_row(
                "holdout-scalar",
                "latest holdout scalar",
                present.fmt(holdout.get("holdout_scalar"), 3),
            )
        )
        rows.append(
            kv_row("ladder", "ladder budget", str(holdout.get("budget_remaining") or present.NULL))
        )
    if budget:
        rows.append(
            kv_row(
                "budget",
                "holdout budget",
                f"{budget.get('spent', present.NULL)} spent / {budget.get('budget', present.NULL)}",
            )
        )
    return Block(title="Split", rows=tuple(rows))


def _rotation_block(health: dict[str, Any]) -> Block:
    rotation = as_dict(health.get("rotation"))
    if not rotation:
        return Block()
    recommended = bool(rotation.get("refresh_recommended"))
    rows = [
        kv_row(
            "rotate",
            "rotation enabled",
            "yes" if rotation.get("rotate_holdout") else "no",
            style="plain" if rotation.get("rotate_holdout") else "faint",
        ),
        kv_row(
            "cadence",
            "generations / contract",
            str(rotation.get("max_generations_per_contract") or present.NULL),
        ),
        kv_row(
            "evaluated",
            "generations evaluated",
            str(
                rotation.get("evaluated_generations")
                if rotation.get("evaluated_generations") is not None
                else present.NULL
            ),
        ),
        row(
            "refresh",
            ("refresh recommended".ljust(LABEL_WIDTH), "faint"),
            ("yes" if recommended else "no", "warn" if recommended else "plain"),
            evidence=evidence(
                what="board rotation cadence",
                measured=(
                    f"{rotation.get('evaluated_generations')} of "
                    f"{rotation.get('max_generations_per_contract') or present.NULL} generations"
                ),
                uncertainty=present.NULL,
                decision="refresh recommended" if recommended else "no refresh due",
                provenance="served eval-health rotation",
            ),
            selectable=True,
        ),
    ]
    if rotation.get("recommendation"):
        rows.append(row("rec", (present.truncate(rotation["recommendation"], 76), "warn")))
    return Block(title="Rotation", rows=tuple(rows))


def _power_block(health: dict[str, Any]) -> Block:
    """What is the smallest effect this board could detect at all?"""
    mde = as_dict(health.get("mde"))
    if not mde:
        return Block()
    usable = bool(mde.get("usable"))
    rows = [
        row(
            "mde",
            ("MDE".ljust(LABEL_WIDTH), "faint"),
            (present.fmt(mde.get("mde"), 4), "plain" if usable else "faint"),
            Span("   "),
            (
                f"relaxed {present.fmt(mde.get('mde_relaxed'), 4)}",
                "faint",
            ),
            evidence=evidence(
                what="the minimum detectable effect at this board's power",
                measured=(
                    f"MDE {present.fmt(mde.get('mde'), 4)} at "
                    f"alpha {present.fmt(mde.get('alpha'), 2)}, "
                    f"power {present.fmt(mde.get('power'), 2)}"
                ),
                uncertainty=(
                    f"noise floor {present.fmt(mde.get('floor'), 4)} "
                    f"over {mde.get('replicates')} replicates"
                    if mde.get("floor_measured")
                    else "noise floor UNMEASURED — the MDE cannot be computed"
                ),
                decision="usable" if usable else "not usable",
                provenance=str(mde.get("formula") or "served eval-health mde"),
            ),
            selectable=True,
        ),
        kv_row(
            "floor",
            "noise floor",
            present.fmt(mde.get("floor"), 4) if mde.get("floor_measured") else present.NULL,
        ),
        kv_row("replicates", "replicates", str(mde.get("replicates") or present.NULL)),
    ]
    if mde.get("note"):
        rows.append(
            row("note", (present.truncate(mde["note"], 76), "warn" if not usable else "faint"))
        )
    return Block(title="Power", rows=tuple(rows))


def _entries_block(epoch: dict[str, Any], evals: dict[str, Any], ctx: LensContext) -> Block:
    """Per-entry outcome distribution as a heat-strip across candidates."""
    entries = [e for e in as_list(evals.get("entries")) if isinstance(e, dict)]
    cells = as_list(evals.get("cells"))
    candidates = [c for c in as_list(evals.get("candidates")) if isinstance(c, dict)]
    board = {b.get("entry_id"): b for b in as_list(epoch.get("board")) if isinstance(b, dict)}
    if not entries:
        return Block(
            title="Entries",
            rows=(
                row(
                    "noentries",
                    (
                        "no scored board entries yet — the outcome grid fills as candidates race",
                        "faint",
                    ),
                ),
            ),
        )
    header = ["entry", "slice", "outcomes", "flip rate", "weight"]
    # The grid is row-per-entry, column-per-candidate; a short `cells` list is a
    # payload that has not caught up, so the missing rows read as "never run"
    # rather than being dropped off the table.
    cell_rows = [cells[i] if i < len(cells) else [] for i in range(len(entries))]
    body = [
        [
            present.truncate(entry.get("entry_id"), 26, fallback=present.NULL),
            str(entry.get("slice") or present.NULL),
            _heat_strip(cell_row, ctx),
            present.fmt(entry.get("flip_rate"), 2)
            if entry.get("flip_rate_measured")
            else present.NULL,
            present.fmt(as_dict(board.get(entry.get("entry_id"))).get("weight"), 2),
        ]
        for entry, cell_row in zip(entries, cell_rows, strict=True)
    ]
    widths = columns([header, *body])
    rows = [row("head", *[(pad(h, widths[i]), "faint") for i, h in enumerate(header)])]
    for entry, cell, cell_row in zip(entries, body, cell_rows, strict=True):
        holdout = entry.get("slice") == "holdout"
        rows.append(
            row(
                f"entry:{entry.get('entry_id')}",
                (pad(cell[0], widths[0]), "plain"),
                (pad(cell[1], widths[1]), "accent" if holdout else "faint"),
                (pad(cell[2], widths[2]), "plain"),
                (rpad(cell[3], widths[3]), "warn" if _noisy(entry) else "faint"),
                (rpad(cell[4], widths[4]), "faint"),
                evidence=evidence(
                    what=f"board entry {entry.get('entry_id')} ({cell[1]})",
                    measured=_cell_summary(cell_row, candidates),
                    uncertainty=(
                        f"flip rate {cell[3]} over {entry.get('calibration_runs')} calibration runs"
                        if entry.get("flip_rate_measured")
                        else "flip rate UNMEASURED (fewer than two usable draws)"
                    ),
                    decision=("held out of training" if holdout else "trains the loop"),
                    provenance=(
                        "calibrated on " f"{entry.get('calibration_generation') or present.NULL}"
                    ),
                ),
                selectable=True,
            )
        )
    ramp = _HEAT_ASCII if ctx.ascii_only else _HEAT
    absent = _ABSENT_ASCII if ctx.ascii_only else _ABSENT
    note = (
        f"outcomes: '{ramp[0]}' none passed → '{ramp[-1]}' all passed; '{absent}' never run"
        if ctx.narrow
        else (
            f"outcomes: one cell per candidate, '{ramp[0]}' none passed → "
            f"'{ramp[-1]}' all passed; '{absent}' = never run"
        )
    )
    return Block(title="Entries", rows=tuple(rows), note=note)


def _heat_strip(cell_row: Any, ctx: LensContext) -> str:
    ramp = _HEAT_ASCII if ctx.ascii_only else _HEAT
    absent = _ABSENT_ASCII if ctx.ascii_only else _ABSENT
    out = []
    for cell in as_list(cell_row):
        ratio = present.num(as_dict(cell).get("pass_ratio"))
        if ratio is None:
            out.append(absent)
            continue
        index = min(len(ramp) - 1, max(0, round(ratio * (len(ramp) - 1))))
        out.append(ramp[index])
    return "".join(out) or absent


def _cell_summary(cell_row: Any, candidates: list[dict[str, Any]]) -> str:
    scored = [as_dict(c) for c in as_list(cell_row) if isinstance(c, dict)]
    if not scored:
        return "no candidate has run this entry"
    losses = [n for n in (present.num(c.get("drift_loss")) for c in scored) if n is not None]
    mean = sum(losses) / len(losses) if losses else None
    return (
        f"{len(scored)}/{len(candidates) or len(scored)} candidates scored · "
        f"mean drift loss {present.fmt(mean, 4)}"
    )


def _noisy(entry: dict[str, Any]) -> bool:
    rate = present.num(entry.get("flip_rate"))
    return bool(entry.get("flip_rate_measured")) and rate is not None and rate > 0


def _flagged_block(health: dict[str, Any]) -> Block:
    """Dead, noisy, under-powered and redundant entries — the instrument's faults."""
    rows = []
    for label, key, style in (
        ("dead (never discriminates)", "dead", "warn"),
        ("noisiest", "noisiest", "warn"),
        ("insufficient replicates", "insufficient", "warn"),
    ):
        items = as_list(health.get(key))
        if not items:
            continue
        rows.append(row(f"h:{key}", (label, "bold")))
        for item in items:
            record = as_dict(item)
            detail = " · ".join(
                f"{k} {present.fmt(v, 3) if present.is_num(v) else v}"
                for k, v in record.items()
                if k != "entry_id" and v is not None
            )
            rows.append(
                row(
                    f"{key}:{record.get('entry_id')}",
                    (
                        pad(present.truncate(record.get("entry_id"), 28, fallback=str(item)), 30),
                        style,
                    ),
                    (present.truncate(detail, 48), "faint"),
                    indent=1,
                    evidence=evidence(
                        what=f"{record.get('entry_id') or item} flagged as {key}",
                        measured=detail or present.NULL,
                        uncertainty=present.NULL,
                        decision=label,
                        provenance="served eval-health",
                    ),
                    selectable=True,
                )
            )
    redundancy = as_dict(health.get("redundancy"))
    clusters = as_list(redundancy.get("clusters"))
    if clusters:
        rows.append(row("h:redundancy", ("redundant clusters", "bold")))
        for i, cluster in enumerate(clusters):
            members = as_list(as_dict(cluster).get("entries")) or as_list(cluster)
            rows.append(
                row(
                    f"cluster:{i}",
                    (", ".join(str(m) for m in members), "faint"),
                    indent=1,
                )
            )
    elif redundancy.get("note"):
        rows.append(row("h:redundancy-note", (str(redundancy["note"]), "faint")))
    return Block(title="Flagged", rows=tuple(rows)) if rows else Block()


def _split_digest(epoch: dict[str, Any]) -> Any:
    split = as_dict(epoch.get("board_split"))
    holdout = as_dict(epoch.get("holdout"))
    return [
        split.get("configured"),
        split.get("enabled"),
        present.fmt(split.get("holdout_fraction"), 3),
        sorted(str(t) for t in as_list(split.get("holdout_tags"))),
        split.get("train_count"),
        split.get("holdout_count"),
        present.fmt(holdout.get("train_scalar"), 4),
        present.fmt(holdout.get("holdout_scalar"), 4),
        holdout.get("budget_remaining"),
        len(as_list(epoch.get("board"))),
    ]


def _health_digest(health: dict[str, Any]) -> Any:
    mde = as_dict(health.get("mde"))
    rotation = as_dict(health.get("rotation"))
    return [
        health.get("found"),
        [
            mde.get("usable"),
            mde.get("floor_measured"),
            present.fmt(mde.get("mde"), 5),
            present.fmt(mde.get("mde_relaxed"), 5),
            present.fmt(mde.get("floor"), 5),
            mde.get("replicates"),
            mde.get("note"),
        ],
        [
            rotation.get("rotate_holdout"),
            rotation.get("max_generations_per_contract"),
            rotation.get("evaluated_generations"),
            rotation.get("refresh_recommended"),
            rotation.get("recommendation"),
        ],
        as_list(health.get("dead")),
        as_list(health.get("noisiest")),
        as_list(health.get("insufficient")),
        as_dict(health.get("holdout_budget")),
        as_list(as_dict(health.get("redundancy")).get("clusters")),
        as_dict(health.get("redundancy")).get("note"),
    ]


def _evals_digest(evals: dict[str, Any]) -> Any:
    return [
        [
            [
                e.get("entry_id"),
                e.get("slice"),
                present.fmt(e.get("flip_rate"), 3),
                e.get("flip_rate_measured"),
            ]
            for e in as_list(evals.get("entries"))
            if isinstance(e, dict)
        ],
        [
            [present.fmt(as_dict(c).get("pass_ratio"), 3) if c else None for c in as_list(cell_row)]
            for cell_row in as_list(evals.get("cells"))
        ],
        [as_dict(c).get("generation_id") for c in as_list(evals.get("candidates"))],
    ]


__all__ = ["BoardLens"]
