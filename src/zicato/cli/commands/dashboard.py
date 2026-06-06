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

This command resolves the bundled static asset directory and calls
``run(...)``. The static bundle is the dashboard's own — it lives next
to the dashboard package at ``zicato/dashboard/static/`` and is served
straight off disk.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.config import DashboardConfig, load_config


def resolve_static_dir(config: DashboardConfig | None = None) -> Path:
    """Return the path to the bundled dashboard static asset directory.

    The dashboard front-end (``index.html`` / ``app.js`` / ``style.css``
    / ``icons.svg``) is the dashboard package's own asset bundle. It
    lives beside the package source and is served straight off disk.

    Resolution order:

    1. The ``static_dir`` of :class:`~zicato.config.DashboardConfig`,
       sourced from the ``ZICATO_DASHBOARD_STATIC_DIR`` environment
       variable — useful for tests and for installed wheels that
       relocate the bundle.
    2. The in-tree ``zicato/dashboard/static`` directory. This file is at
       ``zicato/cli/commands/dashboard.py``; the bundle lives at
       ``zicato/dashboard/static`` under the same package root.

    Parameters
    ----------
    config:
        The :class:`~zicato.config.DashboardConfig` carrying the
        env-sourced ``static_dir``. When ``None`` it is loaded via
        :func:`zicato.config.load_config` — the single place the
        environment is read.

    The path is returned even when it does not exist on disk — the
    dashboard service is responsible for reporting a missing bundle.
    """
    dashboard = config if config is not None else load_config().dashboard
    if dashboard.static_dir:
        return Path(dashboard.static_dir)

    # zicato/cli/commands/dashboard.py -> zicato/dashboard/static
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "dashboard" / "static"


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
def dashboard_cmd(workspace: str, host: str, port: int) -> None:
    """Serve the dashboard for an existing workspace over HTTP.

    Point this at any workspace — a completed epoch for a post-mortem,
    or a workspace some other ``zicato evolve`` is currently driving —
    and open the printed URL in a browser. The server runs in the
    foreground until interrupted (Ctrl-C).
    """
    workspace_root = Path(workspace).resolve()
    static_dir = resolve_static_dir()

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


__all__ = ["dashboard_cmd", "resolve_static_dir"]
