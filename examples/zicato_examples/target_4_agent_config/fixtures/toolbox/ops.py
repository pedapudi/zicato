"""Arithmetic helpers for the toolbox fixture repository."""

from __future__ import annotations

from collections.abc import Sequence


def total(values: Sequence[float]) -> float:
    return float(sum(values))


def window(values: Sequence[float], size: int) -> list[float]:
    """Return the first ``size`` values."""
    return [float(v) for v in values[: size - 1]]
