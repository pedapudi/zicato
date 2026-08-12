"""Health — loop-health findings, the service, and a log tail.

Two questions on one screen: is the loop healthy enough that its evaluation
still distinguishes candidates, and what has the process actually been saying.

The log tail is CURSOR-BASED, exactly as the browser's pane is: the payload
carries a byte cursor, and a refresh appends past it rather than re-reading the
stream. That is why a growing log does not repaint the whole lens.

Payloads: ``/api/health-report``, ``/api/health``, ``/api/heartbeat``,
``/api/logs``.
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
)
from zicato.tui.view import Block, View, digest_of, pad, row

#: Log level to style. The level word always prints beside the line.
LEVEL_STYLE = {
    "critical": "bad",
    "error": "bad",
    "warning": "warn",
    "warn": "warn",
    "info": "plain",
    "debug": "faint",
    "trace": "faint",
}

#: How many log records the tail holds. Beyond this the operator wants
#: ``zicato logs``, which streams properly.
TAIL = 200


class HealthLens:
    name = "health"
    title = "Health"
    label = "Health"

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        report = as_dict(client.get("/api/health-report"))
        service = as_dict(client.get("/api/health"))
        heartbeat = as_dict(client.get("/api/heartbeat"))
        logs = as_dict(client.get(f"/api/logs?limit={TAIL}"))

        blocks = [
            _findings_block(report),
            _service_block(service, heartbeat),
            _logs_block(logs, ctx),
        ]
        return View(
            title="Health",
            subtitle=f"checked {report.get('checked_at') or present.NULL}",
            blocks=tuple(b for b in blocks if b.rows),
            digest=digest_of(
                "health",
                report.get("healthy"),
                [
                    [
                        as_dict(f).get("severity") or as_dict(f).get("level"),
                        as_dict(f).get("detector")
                        or as_dict(f).get("name")
                        or as_dict(f).get("code"),
                        as_dict(f).get("summary") or as_dict(f).get("message"),
                    ]
                    for f in as_list(report.get("findings"))
                ],
                # The service's own uptime ticks every second and paints
                # nothing; only its identity and read-only state are folded.
                [service.get("status"), service.get("read_only"), service.get("workspace")],
                [
                    heartbeat.get("phase"),
                    heartbeat.get("round_index"),
                    heartbeat.get("paused"),
                    heartbeat.get("generation_id"),
                ],
                logs.get("cursor"),
                logs.get("invocation"),
                len(as_list(logs.get("records"))),
            ),
        )


def _findings_block(report: dict[str, Any]) -> Block:
    findings = [f for f in as_list(report.get("findings")) if isinstance(f, dict)]
    if report.get("healthy") is not False and not findings:
        return Block(
            title="Loop health",
            rows=(
                row(
                    "healthy",
                    ("loop is healthy — the evaluation distinguishes candidates", "good"),
                ),
            ),
        )
    rows = []
    for f in findings:
        severity = str(f.get("severity") or f.get("level") or "info").lower()
        name = str(f.get("detector") or f.get("name") or f.get("code") or "finding")
        detail = str(f.get("summary") or f.get("message") or "")
        rows.append(
            row(
                f"finding:{name}",
                (pad(severity, 10), "bad" if severity == "critical" else "warn"),
                (pad(name, 32), "bold"),
                (present.truncate(detail, 44), "faint"),
                evidence=evidence(
                    what=f"health finding {name}",
                    measured=detail or present.NULL,
                    uncertainty=present.NULL,
                    decision=severity,
                    provenance="/api/health-report",
                ),
                selectable=True,
            )
        )
    return Block(title=f"Loop health · {report.get('epoch_id') or present.NULL}", rows=tuple(rows))


def _service_block(service: dict[str, Any], heartbeat: dict[str, Any]) -> Block:
    rows = [
        kv_row("status", "service", str(service.get("status") or present.NULL)),
        kv_row("workspace", "workspace", str(service.get("workspace") or present.NULL)),
        kv_row(
            "readonly",
            "read only",
            "yes" if service.get("read_only") else "no",
            style="plain" if service.get("read_only") else "warn",
        ),
    ]
    if heartbeat:
        rows.append(kv_row("hb-phase", "loop phase", str(heartbeat.get("phase") or present.NULL)))
        rows.append(
            kv_row("hb-gen", "generation", str(heartbeat.get("generation_id") or present.NULL))
        )
        rows.append(
            row(
                "hb-paused",
                ("paused".ljust(LABEL_WIDTH), "faint"),
                (
                    "yes" if heartbeat.get("paused") else "no",
                    "warn" if heartbeat.get("paused") else "plain",
                ),
            )
        )
    return Block(title="Service", rows=tuple(rows))


def _logs_block(logs: dict[str, Any], ctx: LensContext) -> Block:
    records = [r for r in as_list(logs.get("records")) if isinstance(r, dict)]
    invocation = logs.get("invocation")
    if not records:
        return Block(
            title="Logs",
            rows=(
                row(
                    "nologs",
                    (
                        "no log records for this workspace"
                        if invocation is None
                        else f"no records at this level for invocation {invocation}",
                        "faint",
                    ),
                ),
            ),
        )
    rows = []
    for record in records:
        level = str(record.get("level") or "info").lower()
        rows.append(
            row(
                f"log:{record.get('cursor')}",
                (pad(level, 9), LEVEL_STYLE.get(level, "plain")),
                (pad(present.truncate(record.get("component"), 18, fallback=""), 20), "faint"),
                (
                    present.truncate(record.get("message"), 60 if ctx.narrow else 90, fallback=""),
                    "plain",
                ),
                evidence=evidence(
                    what=str(record.get("component") or "log record"),
                    measured=str(record.get("message") or present.NULL),
                    uncertainty=present.NULL,
                    decision=level,
                    provenance=" · ".join(
                        str(x)
                        for x in (
                            record.get("epoch_id"),
                            record.get("generation_id"),
                            record.get("run_id"),
                        )
                        if x
                    )
                    or f"invocation {invocation or present.NULL}",
                ),
                selectable=True,
            )
        )
    note = f"invocation {invocation} · tail of {len(records)} · `zicato logs --follow` to stream"
    return Block(title="Logs", rows=tuple(rows), note=note)


__all__ = ["TAIL", "HealthLens"]
