"""``python -m zicato.tui`` — the console without going through the CLI group.

Useful when the CLI's discovery layer is unavailable (a partial install, a
debug session), and the direct counterpart to ``python -m zicato.dashboard``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zicato.tui")
    parser.add_argument("--workspace", default=".zicato")
    parser.add_argument("--url", default=None)
    parser.add_argument("--view", default=None)
    parser.add_argument("--port", type=int, default=7892)
    parser.add_argument("--ascii", dest="ascii_only", action="store_true")
    args = parser.parse_args(argv)

    from zicato.tui import MISSING_EXTRA, ServiceError, run_tui

    try:
        run_tui(
            url=args.url,
            workspace=Path(args.workspace).resolve() if not args.url else None,
            view=args.view,
            port=args.port,
            ascii_only=True if args.ascii_only else None,
        )
    except ImportError:
        print(MISSING_EXTRA, file=sys.stderr)
        return 2
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
