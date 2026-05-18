"""Test-suite configuration root.

Pins the repository root on ``sys.path`` explicitly rather than
relying on pytest's implicit ``rootdir`` insertion. ``tests/`` is an
importable package (``tests._subprocess_worker_support`` is loaded by
directly-spawned worker subprocesses), and once the ``zicato`` package
moved under a ``src/`` root the implicit path handling is no longer
something to lean on — making the repo root explicit here keeps
``import tests.*`` resolvable from both the in-process test session
and any worker subprocess that inherits the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/conftest.py -> repository root.
_REPO_ROOT = Path(__file__).resolve().parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
