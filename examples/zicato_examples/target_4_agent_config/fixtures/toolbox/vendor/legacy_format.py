"""Vendored rendering helpers. Third-party — replaced wholesale on upgrade."""

from __future__ import annotations


def format_amount(amount: float) -> str:
    """Render ``amount`` with two decimal places and no currency symbol."""
    return f"{amount:.2f}"
