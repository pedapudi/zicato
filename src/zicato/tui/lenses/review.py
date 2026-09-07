"""Candidate, board and health evidence within the existing terminal views."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from zicato.tui import glyphs, present
from zicato.tui.client import Client
from zicato.tui.lenses.base import as_dict, as_list, evidence, kv_row, missing
from zicato.tui.routes import Route, segment
from zicato.tui.view import Block, Row, View, digest_of, pad, row


def content_view(
    title: str, blocks: list[Block], *, degraded: str | None = None, subtitle: str | None = None
) -> View:
    """Digest the rendered rows, evidence and actions after formatting."""
    view = View(title=title, blocks=tuple(blocks), degraded=degraded, subtitle=subtitle)
    return replace(
        view,
        digest=digest_of(
            title,
            degraded,
            [
                (
                    key,
                    [(span.text, span.style) for span in item.spans],
                    item.evidence,
                    item.action,
                    item.indent,
                    item.selectable,
                )
                for key, item in view.lines()
            ],
        ),
    )


def _text(value: Any) -> str:
    if value is None:
        return present.NULL
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return present.fmt(value, 4)
    return str(value)


def record_rows(key: str, values: dict[str, Any], *, indent: int = 0) -> list[Row]:
    """Keep nested evidence reachable without dropping zero or false values."""
    rows = []
    for name, value in values.items():
        label = str(name).replace("_", " ")
        child_key = f"{key}:{name}"
        if isinstance(value, dict):
            rows.append(row(child_key, (label, "faint"), indent=indent))
            rows.extend(record_rows(child_key, value, indent=indent + 1))
        elif isinstance(value, list):
            if not value:
                rows.append(kv_row(child_key, label, "none recorded", indent=indent))
            for i, item in enumerate(value):
                rows.extend(record_rows(f"{child_key}:{i}", {label: item}, indent=indent))
        else:
            rows.append(
                replace(
                    kv_row(child_key, label, _text(value), indent=indent),
                    selectable=True,
                    evidence=evidence(
                        what=label,
                        measured=_text(value),
                        uncertainty=present.NULL,
                        decision=present.NULL,
                        provenance=key,
                    ),
                )
            )
    return rows


def _block(title: str, value: Any, *, absent: str = "not recorded") -> Block:
    data = as_dict(value)
    return Block(
        title=title,
        rows=tuple(record_rows(title, data)) if data else (row("absent", (absent, "faint")),),
    )


def _epoch(client: Client, ctx_epoch: str | None) -> dict[str, Any]:
    return as_dict(client.get("/api/epoch" + (f"?epoch={segment(ctx_epoch)}" if ctx_epoch else "")))


def candidate_review(client: Client, route: Route, *, ascii_only: bool = False) -> View:
    generation = route.params["gen"]
    epoch = route.params.get("epoch") or _epoch(client, None).get("epoch_id")
    if not epoch:
        return missing(
            "Candidate",
            "no epoch is selected",
            hint="open an epoch's standings and select a candidate",
        )
    path = f"/api/epoch/{segment(epoch)}/candidate/{segment(generation)}"
    entry = route.params.get("entry")
    dossier = as_dict(client.get(path + (f"?entry={segment(entry)}" if entry else "")))
    if dossier.get("found") is not True:
        return missing(f"Candidate · {epoch}/{generation}", "no candidate record at this address")
    record = as_dict(dossier.get("generation"))
    experiment = as_dict(dossier.get("experiment"))
    identity = {
        "decision": record.get("decision_label"),
        "parent": record.get("parent_generation_id"),
        "rating": present.rating_text(record, games=True),
        "hypothesis": experiment.get("hypothesis"),
    }
    blocks = [_block("Candidate", identity)]
    if (
        record.get("unreadable")
        or dossier.get("lineage_note")
        or dossier.get("parent_inconsistency")
    ):
        blocks.append(
            _block(
                "Unavailable evidence",
                {
                    "experiment": record.get("unreadable"),
                    "lineage": dossier.get("lineage_note"),
                    "parent": dossier.get("parent_inconsistency"),
                },
            )
        )
    gates = [g for g in as_list(dossier.get("gates")) if isinstance(g, dict)]
    for pair in gates:
        gate = as_dict(pair.get("gate"))
        label = f"Gate · {pair.get('champion')} against {pair.get('challenger')}"
        rules = [r for r in as_list(gate.get("rules")) if isinstance(r, dict)]
        gate_rows = record_rows(
            "gate",
            {
                key: gate.get(key)
                for key in (
                    "decision",
                    "deciding_rule",
                    "reason",
                    "champion_scalar",
                    "challenger_scalar",
                    "delta_scalar",
                    "margin",
                    "delta_pass_rate",
                    "regressed_predicate",
                    "regressed_namespace",
                    "primary_driver",
                )
            },
        )
        for rule in rules:
            gate_rows.append(
                row(
                    str(rule.get("id")),
                    (pad(str(rule.get("status") or "unavailable"), 12), "plain"),
                    (str(rule.get("label") or rule.get("id")), "bold"),
                    (" · deciding rule", "accent") if rule.get("fired") else None,
                    evidence=evidence(
                        what=str(rule.get("label")),
                        measured=_text(rule.get("detail")),
                        uncertainty=_text(rule.get("margin")),
                        decision=str(rule.get("status") or "unavailable"),
                        provenance=path,
                    ),
                    selectable=True,
                )
            )
            if rule.get("detail"):
                gate_rows.extend(
                    record_rows(str(rule.get("id")), {"detail": rule["detail"]}, indent=1)
                )
        if not rules:
            gate_rows.append(
                row(
                    "rules:absent",
                    (
                        "No rule breakdown was recorded; the decision remains historical evidence.",
                        "faint",
                    ),
                )
            )
        blocks.append(Block(title=label, rows=tuple(gate_rows)))
        if gate.get("scalar_decomposition"):
            blocks.append(_block("Scalar contributions", gate["scalar_decomposition"]))
        rating = as_dict(gate.get("rating"))
        if rating.get("present"):
            blocks.append(_rating_block(rating, ascii_only=ascii_only))
        override = as_dict(gate.get("override"))
        if override.get("present"):
            blocks.append(
                _block("Operator override", {k: v for k, v in override.items() if k != "present"})
            )
        comparison = as_dict(pair.get("judge_comparison"))
        if comparison:
            blocks.append(_block("Judge comparison", comparison))
    if not gates:
        blocks.append(_block("Gate evidence", None, absent="No recorded gate for this candidate."))
    per_entry = as_dict(dossier.get("per_entry"))
    if as_dict(per_entry.get("facet_scores")).get("facets"):
        blocks.append(_block("Facet scores", per_entry["facet_scores"]))
    if dossier.get("comparison"):
        blocks.append(_block("Champion comparison", dossier["comparison"]))
    entries = []
    for item in as_list(per_entry.get("entries")):
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("entry_id") or "")
        entries.append(
            row(
                entry_id,
                (pad(entry_id, 24), "bold"),
                (f"loss {present.fmt(item.get('drift_loss'), 4)}  ", "plain")
                if per_entry.get("drift_present") is not False
                else None,
                (
                    "passed"
                    if item.get("pass_fail") is True
                    else "failed"
                    if item.get("pass_fail") is False
                    else "unmeasured",
                    "plain",
                ),
                evidence=evidence(
                    what=entry_id,
                    measured=f"runtime {_text(item.get('runtime_ms'))} ms",
                    uncertainty="open run evidence for measurements and uncertainty",
                    decision=f"budget exceeded: {_text(item.get('wall_clock_budget_exceeded'))}",
                    provenance=f"run {_text(item.get('run_id'))}",
                ),
                action=Route(
                    "standings", {"epoch": epoch, "gen": generation, "entry": entry_id}
                ).to_path(),
                selectable=True,
            )
        )
    blocks.append(Block(title="Board results", rows=tuple(entries), note=per_entry.get("note")))
    blocks.append(_block("Judge losses", dossier.get("per_judge")))
    if entry:
        blocks.append(_block(f"Run evidence · {entry}", dossier.get("drilldown")))
    relatives = []
    for item in as_list(dossier.get("relatives")):
        if isinstance(item, dict):
            eid, gid = str(item["epoch_id"]), str(item["generation_id"])
            relatives.append(
                row(
                    f"{eid}:{gid}",
                    (f"{item['relationship']}  {eid}/{gid}  ", "plain"),
                    (_text(item.get("decision_label")), "faint"),
                    action=Route("standings", {"epoch": eid, "gen": gid}).to_path(),
                    selectable=True,
                )
            )
    blocks.append(
        Block(
            title="Lineage",
            rows=tuple(relatives),
            note=None if relatives else "No related generation was recorded.",
        )
    )
    return content_view(f"Candidate · {epoch}/{generation}", blocks)


def _rating_block(rating: dict[str, Any], *, ascii_only: bool) -> Block:
    intervals = [as_dict(rating.get(role)) for role in ("champion", "challenger")]
    limits = [side.get(key) for side in intervals for key in ("ci_lo", "ci_hi")]
    bounds = [float(v) for v in limits if isinstance(v, int | float) and present.is_num(v)]
    scale = (min(bounds), max(bounds)) if len(bounds) == 4 else None
    rows = record_rows(
        "rating",
        {key: rating.get(key) for key in ("p_stronger", "threshold", "credible", "decision")},
    )
    for role, interval in zip(("champion", "challenger"), intervals, strict=True):
        text = f"{present.fmt(interval.get('theta'), 3)} ± {present.fmt(interval.get('se'), 3)}"
        if scale and scale[0] < scale[1]:
            text += "  " + glyphs.whisker(
                interval.get("ci_lo"),
                interval.get("theta"),
                interval.get("ci_hi"),
                scale=scale,
                ascii_only=ascii_only,
            )
        rows.append(kv_row(role, role, text))
    return Block(title="Recorded rating evidence", rows=tuple(rows))


def board_review(client: Client, route: Route, *, ascii_only: bool = False) -> View:
    epoch = _epoch(client, route.params.get("epoch"))
    epoch_id = route.params.get("epoch") or epoch.get("epoch_id")
    if not epoch_id:
        return missing("Board", "no epoch is selected")
    prefix = f"/api/epoch/{segment(epoch_id)}"
    entry = route.params.get("entry")
    if entry and route.params.get("gen"):
        return candidate_review(
            client,
            replace(route, params={**route.params, "epoch": epoch_id}),
            ascii_only=ascii_only,
        )
    if entry:
        dossier = as_dict(client.get(f"{prefix}/eval/{segment(entry)}"))
        if dossier.get("found") is not True:
            return missing(f"Board entry · {entry}", "no evaluation evidence at this address")
        runs = [
            row(
                str(item["generation_id"]),
                (str(item["generation_id"]), "bold"),
                action=Route(
                    "instrument",
                    {
                        "epoch": epoch_id,
                        "detail": "board",
                        "entry": entry,
                        "gen": str(item["generation_id"]),
                    },
                ).to_path(),
                selectable=True,
            )
            for item in map(as_dict, as_list(dossier.get("trajectory")))
            if item.get("generation_id")
        ]
        return content_view(
            f"Board entry · {epoch_id}/{entry}",
            [
                _block(
                    "Evaluation evidence",
                    {
                        k: v
                        for k, v in dossier.items()
                        if k not in {"found", "epoch_id", "entry_id"}
                    },
                ),
                Block(title="Candidate runs", rows=tuple(runs)),
            ],
        )
    health = as_dict(client.get(f"{prefix}/eval-health"))
    matrix = as_dict(client.get(f"{prefix}/evals"))
    blocks = [_block("Train and holdout split", epoch.get("board_split"))]
    for key, label in (
        ("rotation", "Rotation"),
        ("holdout_budget", "Holdout budget"),
        ("mde", "Detectable effect and power"),
        ("noisiest", "Noisiest entries"),
        ("dead", "Entries without discrimination"),
        ("insufficient", "Insufficient comparisons"),
        ("runtime_cost", "Runtime cost"),
        ("redundancy", "Redundancy"),
    ):
        value = health.get(key)
        blocks.append(_block(label, {"entries": value} if isinstance(value, list) else value))
    candidates = [as_dict(c).get("generation_id") for c in as_list(matrix.get("candidates"))]
    entries = [as_dict(e) for e in as_list(matrix.get("entries"))]
    cells = as_list(matrix.get("cells"))
    cell_width = max([7, *(len(str(g)) + 2 for g in candidates)])
    rows = [
        row(
            "axis",
            (pad("entry", 24) + "".join(pad(str(g), cell_width) for g in candidates), "faint"),
        )
    ]
    for i, item in enumerate(entries):
        entry_id = str(item.get("entry_id") or "")
        ratios = []
        entry_cells = as_list(cells[i]) if i < len(cells) else []
        for j in range(len(candidates)):
            value = as_dict(entry_cells[j] if j < len(entry_cells) else None).get("pass_ratio")
            mark = ""
            if isinstance(value, int | float) and present.is_num(value) and 0 <= value <= 1:
                mark = (".:-=#" if ascii_only else "·░▒▓█")[round(value * 4)] + " "
            ratios.append(pad(mark + present.fmt(value, 2), cell_width))
        rows.append(
            row(
                entry_id,
                (pad(entry_id, 24), "bold"),
                ("".join(ratios), "plain"),
                evidence=evidence(
                    what=entry_id,
                    measured=f"slice {_text(item.get('slice'))}",
                    uncertainty=f"flip rate {_text(item.get('flip_rate'))}",
                    decision="pass ratios as served; absent cells are unmeasured",
                    provenance=prefix + "/evals",
                ),
                action=Route(
                    "instrument", {"epoch": epoch_id, "detail": "board", "entry": entry_id}
                ).to_path(),
                selectable=True,
            )
        )
    blocks.append(
        Block(
            title="Outcome distribution",
            rows=tuple(rows),
            note="Each cell is the recorded pass ratio; — means no measurement.",
        )
    )
    return content_view(
        f"Board · {epoch_id}",
        blocks,
        degraded=None
        if health.get("found") is True
        else "Evaluation health is unavailable for this epoch.",
    )


def health_review(client: Client, route: Route) -> View:
    report = as_dict(client.get("/api/health-report"))
    epoch = route.params.get("epoch")
    if epoch and report.get("epoch_id") != epoch:
        report = {}
    unavailable = (
        "Health reports describe the active epoch; no report is available for this selection."
        if epoch and not report
        else "No health report is available."
    )
    status = report.get("healthy")
    rows = [
        kv_row(
            "status",
            "reported health",
            "healthy"
            if status is True
            else "findings require attention"
            if status is False
            else "unavailable",
        )
    ]
    for i, finding in enumerate(as_list(report.get("findings"))):
        if not isinstance(finding, dict):
            continue
        rows.append(
            row(
                f"finding:{i}",
                (str(finding.get("severity") or "unavailable") + "  ", "plain"),
                (str(finding.get("summary") or finding.get("message") or ""), "bold"),
                evidence=evidence(
                    what=str(finding.get("detector") or finding.get("code")),
                    measured=_text(finding.get("summary")),
                    uncertainty=present.NULL,
                    decision=_text(as_dict(finding.get("detail")).get("recommendation")),
                    provenance=f"epoch {_text(report.get('epoch_id'))}",
                ),
                selectable=True,
            )
        )
        detail = finding.get("detail")
        rows.extend(
            record_rows(
                f"detail:{i}", detail if isinstance(detail, dict) else {"detail": detail}, indent=1
            )
        )
    service = as_dict(client.get("/api/health"))
    heartbeat = as_dict(client.get("/api/heartbeat"))
    logs = as_dict(client.get("/api/logs?limit=200"))
    log_rows = []
    for i, item in enumerate(as_list(logs.get("records"))):
        if not isinstance(item, dict) or epoch and item.get("epoch_id") != epoch:
            continue
        log_rows.append(
            row(
                str(item.get("cursor", i)),
                (pad(str(item.get("level") or ""), 9), "faint"),
                (str(item.get("message") or ""), "plain"),
                evidence=evidence(
                    what=str(item.get("component") or "log record"),
                    measured=str(item.get("message") or ""),
                    uncertainty=present.NULL,
                    decision=str(item.get("level") or ""),
                    provenance=(
                        f"epoch {_text(item.get('epoch_id'))} · "
                        f"generation {_text(item.get('generation_id'))} · "
                        f"run {_text(item.get('run_id'))}"
                    ),
                ),
                selectable=True,
            )
        )
    blocks = [
        Block(title="Health findings", rows=tuple(rows)),
        _block(
            "Service",
            {k: service.get(k) for k in ("status", "version", "workspace", "port", "read_only")},
        ),
        _block(
            "Workspace runtime",
            {k: heartbeat.get(k) for k in ("epoch_id", "generation_id", "phase", "paused")},
        ),
        Block(
            title="Log tail",
            rows=tuple(log_rows),
            note=(
                f"Up to 200 records from invocation {_text(logs.get('invocation'))}; "
                "refreshed as a bounded tail."
            ),
        ),
    ]
    return content_view(
        "Health and logs" + (f" · {epoch}" if epoch else ""),
        blocks,
        degraded=None if report else unavailable,
    )
