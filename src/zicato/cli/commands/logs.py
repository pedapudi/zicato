"""``zicato inspect logs`` — read the structured operator-log streams.

ADVANCED — off the happy path. Every ``zicato evolve`` (and ``zicato
reflect run``) invocation writes one structured JSONL stream under
``.zicato/logs/`` (see docs/design/LOGGING.md). This command tails a
stream through the SAME query-layer reader the dashboard uses — the files
are canonical.

An empty / no-logs workspace prints nothing and exits 0 (honest silence,
not an error).

Standalone command file picked up by :mod:`zicato.cli.discovery`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import click

from zicato.query import WorkspacePaths, build_log_view, clamp_log_limit

#: Poll interval for ``--follow`` (seconds). Matches the run-log tail
#: cadence — responsive without busy-spinning.
_FOLLOW_INTERVAL_S = 0.5


def format_record(record: dict[str, Any]) -> str:
    """Render one log record as a single terminal line.

    ``<ts> <LEVEL> <component> [<context>] <message>``. The context bits
    (epoch / generation / run) are shown only when present, so an unbound
    record stays terse. Pure + free-function so a test can assert on the
    rendered text without the click runner.
    """
    ts = str(record.get("ts") or "")
    level = str(record.get("level") or "").ljust(7)
    component = str(record.get("component") or "")
    message = str(record.get("message") or "")
    ctx_bits = []
    for key in ("epoch_id", "generation_id", "run_id"):
        val = record.get(key)
        if val:
            ctx_bits.append(f"{key}={val}")
    ctx = (" [" + " ".join(ctx_bits) + "]") if ctx_bits else ""
    return f"{ts} {level} {component}{ctx} {message}"


def _print_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        click.echo(format_record(record))


@click.command(
    name="logs",
    short_help="Advanced: tail the structured operator-log stream for an invocation.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root (the directory `zicato init` made).",
)
@click.option(
    "--invocation",
    default="latest",
    show_default=True,
    help=(
        "Which invocation stream to read: 'latest' (the newest) or a "
        "specific <stamp>-<pid> id (list them with --list)."
    ),
)
@click.option(
    "--level",
    default=None,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    help="Show only records at or above this level. Unset shows everything captured.",
)
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(min=1),
    help="Tail at most this many records (default 200).",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    default=False,
    help="Poll-tail the stream, printing new records as they land (Ctrl-C to stop).",
)
@click.option(
    "--list",
    "list_invocations_flag",
    is_flag=True,
    default=False,
    help="List the available invocation streams (newest first) and exit.",
)
def logs_cmd(
    workspace: str,
    invocation: str,
    level: str | None,
    limit: int | None,
    follow: bool,
    list_invocations_flag: bool,
) -> None:
    """Tail the structured operator-log stream for one evolve invocation.

    The streams live under `.zicato/logs/<stamp>-<pid>.jsonl` — one per
    `evolve` / `reflect run` invocation — and this reads them through the
    same reader the dashboard log pane uses. A workspace with no logs
    prints nothing and exits 0.
    """
    paths = WorkspacePaths(Path(workspace).resolve())
    level_name = level.upper() if level else None
    resolved_limit = clamp_log_limit(limit)

    if list_invocations_flag:
        view = build_log_view(paths, limit=resolved_limit, level=level_name, invocation=invocation)
        for inv in view["invocations"]:
            click.echo(f"{inv['id']}  ({inv['size']} bytes)")
        return

    view = build_log_view(paths, limit=resolved_limit, level=level_name, invocation=invocation)
    _print_records(view["records"])

    if not follow:
        return

    # Follow: poll for records past the last cursor and print them. Pin to
    # the resolved invocation id so a NEW invocation starting mid-follow
    # does not silently switch streams under us.
    invocation_id = view.get("invocation") or invocation
    cursor = view.get("cursor")
    try:
        while True:
            time.sleep(_FOLLOW_INTERVAL_S)
            tick = build_log_view(
                paths,
                limit=resolved_limit,
                level=level_name,
                after=cursor,
                invocation=invocation_id,
            )
            _print_records(tick["records"])
            if tick.get("cursor") is not None:
                cursor = tick["cursor"]
    except KeyboardInterrupt:  # pragma: no cover — interactive stop
        pass


__all__ = ["logs_cmd", "format_record"]
