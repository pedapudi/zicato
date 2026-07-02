"""``zicato builder`` — launch the dashboard focused on the tournament builder.

ADVANCED — off the happy path. The tournament builder is a first-class
dashboard VIEW: a form + live preview + chat copilot for composing an epoch's
evaluation contract (board · proposer brief · scoring · structure ·
overfitting), rendered full-width at its own ``#/builder`` route (Settings
keeps a launcher to it). ``zicato evolve`` already serves it as part of the
live dashboard; this command is the standalone counterpart that boots the same
dashboard service against an *existing* workspace and points the operator
straight at the builder's deep-link.

It is a thin focus-wrapper over ``zicato dashboard``: it reuses the same
dashboard launch machinery (the server's :func:`run` entry point + the bundled
static asset directory via :func:`zicato.dashboard.static_assets.resolve_static_dir`),
honours the same loopback-only bind rule, and prints the builder deep-link
(``http://<host>:<port>/#/builder``) as the primary link so the browser opens
on the builder rather than the environment overview.

Standalone command file picked up by :mod:`zicato.cli.discovery`.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.config import DashboardConfig
from zicato.dashboard.static_assets import resolve_static_dir

#: The dashboard hash-route the builder lives behind. ``zicato builder`` prints
#: this deep-link so the browser opens directly on the standalone tournament-
#: builder view (the router resolves ``#/builder`` to its own first-class view,
#: rendered full-width — no longer nested inside Settings).
BUILDER_FRAGMENT = "/#/builder"


def builder_url(host: str, port: int) -> str:
    """Return the dashboard URL deep-linked to the tournament builder."""
    return f"http://{host}:{port}{BUILDER_FRAGMENT}"


@click.command(
    name="builder",
    short_help="Advanced: launch the dashboard focused on the tournament builder.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root to serve.",
)
@click.option(
    "--dashboard-port",
    "port",
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
def builder_cmd(workspace: str, port: int, static_dir_flag: str | None) -> None:
    """Launch the dashboard focused on the tournament builder.

    Boots the same dashboard service ``zicato dashboard`` runs, against the
    given workspace, and prints the builder deep-link so the browser opens on
    the builder rather than the environment overview. The server runs in the
    foreground until interrupted (Ctrl-C).

    The bind address is fixed at the loopback ``127.0.0.1`` — the dashboard is
    a local inspection surface, never exposed on a routable interface (the same
    rule ``zicato dashboard`` / ``zicato evolve`` honour).
    """
    # Loopback only — never a routable bind. Matches the dashboard / evolve
    # rule: the dashboard (and the builder it homes) is a local surface.
    host = "127.0.0.1"
    workspace_root = Path(workspace).resolve()
    static_dir = resolve_static_dir(
        DashboardConfig(static_dir=static_dir_flag) if static_dir_flag else None
    )

    # Lazy import: the dashboard service pulls in Starlette. Importing it here
    # (rather than at module top level) keeps `zicato --help` fast and means an
    # environment without the dashboard's optional deps does not break the rest
    # of the CLI — the discovery layer would otherwise drop this command.
    try:
        from zicato.dashboard import server as dashboard_server  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise click.ClickException(
            f"the dashboard service (zicato.dashboard.server) is not available in this build: {exc}"
        ) from exc

    click.echo(f"Tournament builder: {builder_url(host, port)}")
    click.echo(f"Serving workspace {workspace_root}", err=True)
    dashboard_server.run(
        workspace_root=workspace_root,
        host=host,
        port=port,
        static_dir=static_dir,
    )


__all__ = ["builder_cmd", "builder_url", "BUILDER_FRAGMENT"]
