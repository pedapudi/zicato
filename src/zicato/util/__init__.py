"""Small, dependency-free shared utilities for zicato.

This package holds helpers that are genuinely cross-cutting — used by
several otherwise-unrelated subsystems — and that carry no domain logic
of their own. Keeping a single definition here avoids the drift that
comes from each module growing its own private copy.
"""

from __future__ import annotations

from zicato.util.best_effort import (
    best_effort,
    best_effort_failures,
    reset_best_effort_failures,
)
from zicato.util.iso_time import now_iso

__all__ = [
    "best_effort",
    "best_effort_failures",
    "now_iso",
    "reset_best_effort_failures",
]
