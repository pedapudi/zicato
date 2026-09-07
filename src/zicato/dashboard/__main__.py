"""``python -m zicato.dashboard`` entry point.

Spawned by ``zicato evolve`` alongside the watchdog-only supervisor (and
usable directly) to serve the standalone Python dashboard against a
workspace. Thin shim: parse args, resolve the selected static directory,
hand off to :func:`zicato.dashboard.server.run`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from zicato.config import DashboardConfig
from zicato.dashboard.server import _resolve_workspace, run
from zicato.dashboard.static_assets import resolve_static_dir


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m zicato.dashboard")
    parser.add_argument("--workspace", type=Path, default=Path(".zicato"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7892)
    parser.add_argument("--static-dir", type=Path)
    args = parser.parse_args()
    workspace_root = _resolve_workspace(args.workspace).root
    config = (
        DashboardConfig(static_dir=str(args.static_dir.resolve()))
        if args.static_dir is not None
        else None
    )
    run(
        workspace_root=workspace_root,
        host=args.host,
        port=args.port,
        static_dir=resolve_static_dir(config, workspace_root=workspace_root),
    )


if __name__ == "__main__":
    main()
