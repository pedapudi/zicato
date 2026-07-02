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

from zicato.config import DashboardConfig, load_config


def resolve_static_dir(config: DashboardConfig | None = None) -> Path:
    """Return the path to the bundled dashboard static asset directory.

    Resolution order:

    1. The ``static_dir`` of :class:`~zicato.config.DashboardConfig`,
       sourced from the ``--static-dir`` flag — useful for tests and
       for installed wheels that relocate the bundle.
    2. The in-tree ``zicato/dashboard/static`` directory, next to this
       module.

    Parameters
    ----------
    config:
        The :class:`~zicato.config.DashboardConfig` carrying
        ``static_dir`` (the CLI commands build it from the
        ``--static-dir`` flag). When ``None`` it is loaded via
        :func:`zicato.config.load_config`.

    The path is returned even when it does not exist on disk — the
    dashboard service is responsible for reporting a missing bundle.
    """
    dashboard = config if config is not None else load_config().dashboard
    if dashboard.static_dir:
        return Path(dashboard.static_dir)

    # zicato/dashboard/static_assets.py -> zicato/dashboard/static
    return Path(__file__).resolve().parent / "static"


__all__ = ["resolve_static_dir"]
