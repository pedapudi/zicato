"""Read proposal quality and its trend from recorded rounds."""

from __future__ import annotations

from typing import Any

from zicato.query.paths import WorkspacePaths


def build_proposer_scorecard(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """The scorecard trend, plus the one epoch's card when ``epoch_id`` is given.

    ``epochs`` is oldest-first so the panel's trend reads left to right without
    the client reversing anything. ``found`` is false only when the workspace
    has no epochs at all — an epoch that simply never ran a round is FOUND, and
    its card is a row of honest nulls.
    """
    from zicato.proposer.scorecard import (  # noqa: PLC0415
        MIN_SAMPLE_N,
        read_epoch_scorecard,
        read_scorecard_trend,
    )

    try:
        trend = read_scorecard_trend(paths.root, limit=12)
    except Exception:  # noqa: BLE001 - a read surface never raises at the endpoint
        trend = []

    card: dict[str, Any] | None = None
    if epoch_id:
        try:
            card = read_epoch_scorecard(paths.root, epoch_id).to_json()
        except Exception:  # noqa: BLE001
            card = None

    return {
        "found": bool(trend) or card is not None,
        "epoch_id": epoch_id,
        "min_sample_n": MIN_SAMPLE_N,
        "epochs": [c.to_json() for c in trend],
        "card": card,
    }


__all__ = ["build_proposer_scorecard"]
