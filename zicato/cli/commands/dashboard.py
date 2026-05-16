"""``zicato dashboard`` — serve the workspace dashboard over HTTP.

Standalone command file picked up by :mod:`zicato.cli.discovery`.

This command runs the Python dashboard service against an *existing*
workspace, for post-mortem inspection of a completed epoch or for
read-only viewing of a run that some other process is driving. It is
the standalone counterpart to the dashboard that ``zicato evolve``
auto-spawns: ``evolve`` owns the dashboard for the lifetime of a loop,
whereas ``zicato dashboard`` lets an operator point at any workspace at
any time.

The dashboard service itself lives in :mod:`zicato.dashboard.server`
and exposes two entry points::

    create_app(workspace_root, static_dir, *, read_only=True) -> Starlette
    run(workspace_root, host, port, static_dir) -> None

This command resolves the bundled static asset directory and calls
``run(...)``. The static bundle is the same one the Rust supervisor
embeds — ``<repo_root>/supervisor/static/`` in a development checkout.
"""

from __future__ import annotations

from pathlib import Path

import click


def resolve_static_dir() -> Path:
    """Return the path to the bundled dashboard static asset directory.

    The dashboard front-end (``index.html`` / ``app.js`` / ``style.css``
    / ``icons.svg``) is shared with the Rust supervisor, which embeds it
    at compile time. For the Python dashboard we serve it straight off
    disk.

    Resolution order:

    1. Environment override ``ZICATO_DASHBOARD_STATIC_DIR`` — useful for
       tests and for installed wheels that relocate the bundle.
    2. The in-tree ``supervisor/static`` directory, computed relative to
       this source file. This file is at
       ``zicato/cli/commands/dashboard.py``; the bundle lives at
       ``<repo_root>/supervisor/static``.

    The path is returned even when it does not exist on disk — the
    dashboard service is responsible for reporting a missing bundle.
    """
    import os  # noqa: PLC0415

    env_override = os.environ.get("ZICATO_DASHBOARD_STATIC_DIR")
    if env_override:
        return Path(env_override)

    here = Path(__file__).resolve()
    # zicato/cli/commands/dashboard.py -> <repo_root>/supervisor/static
    return here.parent.parent.parent.parent / "supervisor" / "static"


@click.command(name="dashboard")
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

    # Lazy import: the dashboard service pulls in Starlette and is owned
    # by a parallel workstream. Importing it here (rather than at module
    # top level) keeps `zicato --help` fast and means a not-yet-present
    # ``zicato.dashboard.server`` does not break the rest of the CLI —
    # the discovery layer would otherwise drop this whole command.
    try:
        from zicato.dashboard import server as dashboard_server  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on parallel work
        raise click.ClickException(
            "the dashboard service (zicato.dashboard.server) is not "
            f"available in this build: {exc}"
        ) from exc

    click.echo(f"Dashboard: http://{host}:{port}")
    click.echo(f"Serving workspace {workspace_root}", err=True)
    dashboard_server.run(
        workspace_root=workspace_root,
        host=host,
        port=port,
        static_dir=static_dir,
    )


__all__ = ["dashboard_cmd", "resolve_static_dir"]
