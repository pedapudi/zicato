"""``python -m zicato.dashboard`` entry point.

Spawned by ``zicato evolve`` alongside the watchdog-only supervisor (and
usable directly) to serve the standalone Python dashboard against a
workspace. Thin shim: parse args, resolve the bundled static directory,
hand off to :func:`zicato.dashboard.server.run`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from zicato.cli.commands.dashboard import resolve_static_dir
from zicato.dashboard.server import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m zicato.dashboard")
    parser.add_argument("--workspace", type=Path, default=Path(".zicato"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7892)
    args = parser.parse_args()
    run(
        workspace_root=args.workspace,
        host=args.host,
        port=args.port,
        static_dir=resolve_static_dir(),
    )


if __name__ == "__main__":
    main()
