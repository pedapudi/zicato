"""Behavioural checks for ``ops.py``. Run: ``python checks/verify_ops.py``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ops  # noqa: E402


def main() -> int:
    assert ops.total([1, 2, 3]) == 6.0
    assert ops.window([1, 2, 3, 4], 3) == [1.0, 2.0, 3.0], "window returned the wrong count"
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
