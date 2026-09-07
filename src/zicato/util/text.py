"""Text previews shared by workspace views and reports."""

from __future__ import annotations


def preview(text: str, limit: int = 120) -> str:
    """Strip surrounding whitespace and append an ellipsis after a capped prefix."""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."
