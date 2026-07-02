"""``zicato dashboard`` — serve the workspace dashboard over HTTP.

ADVANCED — off the happy path. ``zicato evolve`` auto-spawns the
dashboard for the lifetime of a loop and prints its URL. ``zicato
dashboard`` is the standalone counterpart: it runs the same Python
dashboard service against an *existing* workspace — for post-mortem
inspection of a completed epoch, or read-only viewing of a run some
other process is driving.

Standalone command file picked up by :mod:`zicato.cli.discovery`.

The dashboard service itself lives in :mod:`zicato.dashboard.server`
and exposes two entry points::

    create_app(workspace_root, static_dir, *, read_only=True) -> Starlette
    run(workspace_root, host, port, static_dir) -> None

This command resolves the bundled static asset directory (via
:func:`zicato.dashboard.static_assets.resolve_static_dir` — the
dashboard owns its own bundle) and calls ``run(...)``.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.config import DashboardConfig
from zicato.dashboard.static_assets import resolve_static_dir


@click.command(
    name="dashboard",
    short_help="Advanced: serve the dashboard for an existing workspace (evolve auto-spawns it).",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root to serve.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host/bind address for the dashboard HTTP server.",
)
@click.option(
    "--port",
    default=7892,
    show_default=True,
    type=click.IntRange(min=1, max=65535),
    help="Port for the dashboard HTTP server.",
)
@click.option(
    "--static-dir",
    "static_dir_flag",
    default=None,
    type=click.Path(file_okay=False),
    help=(
        "Filesystem path to the dashboard static-asset directory. "
        "Shadows the dashboard.static_dir config knob. Unset (the "
        "default) serves the bundled zicato/dashboard/static directory."
    ),
)
def dashboard_cmd(workspace: str, host: str, port: int, static_dir_flag: str | None) -> None:
    """Serve the dashboard for an existing workspace over HTTP.

    Point this at any workspace — a completed epoch for a post-mortem,
    or a workspace some other ``zicato evolve`` is currently driving —
    and open the printed URL in a browser. The server runs in the
    foreground until interrupted (Ctrl-C).
    """
    workspace_root = Path(workspace).resolve()
    static_dir = resolve_static_dir(
        DashboardConfig(static_dir=static_dir_flag) if static_dir_flag else None
    )

    # Lazy import: the dashboard service pulls in Starlette. Importing it
    # here (rather than at module top level) keeps `zicato --help` fast
    # and means an environment without the dashboard's optional deps does
    # not break the rest of the CLI — the discovery layer would otherwise
    # drop this whole command.
    try:
        from zicato.dashboard import server as dashboard_server  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise click.ClickException(
            f"the dashboard service (zicato.dashboard.server) is not available in this build: {exc}"
        ) from exc

    # The definitive ``Dashboard:`` URL is printed by ``server.run`` once
    # the real bound port is known (``_pick_port`` may walk +1 off the
    # requested port on a TIME_WAIT bounce), so it is NOT pre-printed here.
    click.echo(f"Serving workspace {workspace_root}", err=True)
    dashboard_server.run(
        workspace_root=workspace_root,
        host=host,
        port=port,
        static_dir=static_dir,
    )


__all__ = ["dashboard_cmd"]
