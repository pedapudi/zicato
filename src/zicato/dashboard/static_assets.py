"""Resolution of the dashboard's bundled static asset directory.

The dashboard front-end (``index.html`` / ``app.js`` / ``style.css`` /
``icons.svg``) is the dashboard package's own asset bundle: it lives
beside this module at ``zicato/dashboard/static/`` and is served
straight off disk. The dashboard owns its assets, so it owns their
resolution — the CLI commands (``zicato dashboard`` / ``zicato
builder``) import :func:`resolve_static_dir` from here, a declared
cli→dashboard edge in the import-linter contracts.
"""

from __future__ import annotations

from pathlib import Path

from zicato.config import DashboardConfig, load_config, resolve_configuration
from zicato.workspace.config_io import read_workspace_config


def resolve_static_dir(
    config: DashboardConfig | None = None, *, workspace_root: Path | None = None
) -> Path:
    """Resolve selected assets, falling back to the bundled directory.

    A carried configuration takes precedence. Otherwise, resolve the workspace
    declaration when a root is supplied, or use defaults. Authored relative
    paths use the workspace parent as their base. CLI flags are made absolute
    by their entry point, preserving their meaning relative to the caller.

    The path is returned even when it does not exist on disk — the
    dashboard service is responsible for reporting a missing bundle.
    """
    if config is not None:
        dashboard = config
    elif workspace_root is not None:
        dashboard = resolve_configuration(
            read_workspace_config(workspace_root).raw
        ).values.dashboard
    else:
        dashboard = load_config().dashboard
    if dashboard.static_dir:
        path = Path(dashboard.static_dir)
        if workspace_root is not None:
            return (workspace_root.resolve().parent / path).resolve()
        return path

    # zicato/dashboard/static_assets.py -> zicato/dashboard/static
    return Path(__file__).resolve().parent / "static"


__all__ = ["resolve_static_dir"]
