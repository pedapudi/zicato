"""``zicato tui`` — the Console in the terminal.

The browser dashboard's peer surface: the same served model, rendered for a
terminal. Attaches to a running dashboard service with ``--url``, or starts one
against ``--workspace`` (reusing the same spawn path ``zicato evolve`` uses,
bound to loopback — there is no bind flag; the console is a local surface).

Standalone command file picked up by :mod:`zicato.cli.discovery`.
"""

from __future__ import annotations

from pathlib import Path

import click


@click.command(
    name="tui",
    short_help="Advanced: review a workspace in the terminal (the dashboard's peer surface).",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root to review.",
)
@click.option(
    "--url",
    default=None,
    help=(
        "URL of a running dashboard service to attach to (e.g. "
        "http://127.0.0.1:7892). Unset: attach to this workspace's service if "
        "one is running, else start one."
    ),
)
@click.option(
    "--view",
    default=None,
    help=(
        "Open a lens directly. Takes the SAME path the browser's hash router "
        "takes — `/e/<epoch>/gen/<gen>`, `/logs` — or a shorthand like "
        "`candidate/<gen>` or `instrument`."
    ),
)
@click.option(
    "--port",
    default=7892,
    show_default=True,
    type=click.IntRange(min=1, max=65535),
    help="Preferred port when starting a dashboard service (it walks +1 if taken).",
)
@click.option(
    "--ascii",
    "ascii_only",
    is_flag=True,
    default=False,
    help="Force the ASCII, weight-only rendering (also on under NO_COLOR / a non-UTF-8 locale).",
)
def tui_cmd(
    workspace: str,
    url: str | None,
    view: str | None,
    port: int,
    ascii_only: bool,
) -> None:
    """Review a zicato workspace from the terminal.

    The browser dashboard's peer surface: the same served payloads, rendered
    for a terminal. Six lenses -- Home, Standings, Candidate, Board,
    Instrument, Health -- with 1-6 to jump, j/k to move, enter to drill, b to
    go back, / to filter, and ? for help.

    Applying a recommendation shells out to the same CLI command an operator
    would type; the console never mutates the workspace itself.
    """
    # Lazy import: the console pulls in Textual (the `tui` extra). Importing it
    # here keeps `zicato --help` fast and means an install without the extra
    # gets a one-line instruction rather than the discovery layer silently
    # dropping this whole command.
    from zicato.tui import MISSING_EXTRA, ServiceError, run_tui  # noqa: PLC0415

    try:
        run_tui(
            url=url,
            workspace=Path(workspace).resolve() if not url else None,
            view=view,
            port=port,
            ascii_only=True if ascii_only else None,
        )
    except ImportError as exc:
        raise click.ClickException(MISSING_EXTRA) from exc
    except ServiceError as exc:
        message = str(exc)
        if exc.hint:
            message += f"\n{exc.hint}"
        raise click.ClickException(message) from exc


__all__ = ["tui_cmd"]
