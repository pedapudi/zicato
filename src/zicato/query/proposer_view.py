"""proposer_view — the Instrument-lens read surface over the proposer scorecard.

The server-authority half of the proposer panel: it projects the scorecard
trend and the pending-recommendation queue into the two JSON shapes the panel
renders. The view computes every rate, band, and count; the client renders them
and derives nothing — the same division of labour ``reflection_view`` keeps
with the Instrument lens.

Absent epochs, round logs, and reflection records produce empty collections.
A malformed recommendation collection returns the record owner's refusal.

Stays dashboard-free. Scorecard aggregation and pending-queue selection remain
in their domain modules; this module projects their accepted values.
"""

from __future__ import annotations

from typing import Any

from zicato.epoch._storage import RecordError
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


def build_proposer_recommendations(paths: WorkspacePaths) -> dict[str, Any]:
    """The pending queue — drafted, carries a remedy, not yet applied.

    Each row includes the finding's evidence and the stored remedy, including
    its proposed text and diff. Review clients can inspect the exact proposal
    before presenting an apply command. Summary fields remain available for
    compact panels that do not display the full remedy.
    """
    from zicato.proposer.reflection import pending_recommendations  # noqa: PLC0415

    try:
        pending = pending_recommendations(paths.root)
    except (RecordError, OSError) as exc:
        return {"found": False, "pending": [], "count": 0, "unreadable": str(exc)}

    rows: list[dict[str, Any]] = []
    for item in pending:
        remedy = item.get("remedy") or {}
        rows.append(
            {
                "finding_id": item.get("finding_id", ""),
                "epoch_id": item.get("epoch_id", ""),
                "reflection_id": item.get("reflection_id", ""),
                "severity": item.get("severity", ""),
                "title": item.get("title", ""),
                "detail": item.get("detail", ""),
                "population": item.get("population", ""),
                "measured": item.get("measured", []),
                "compared_against": item.get("compared_against", ""),
                "remedy_safety": item.get("remedy_safety", ""),
                "remedy_kind": remedy.get("kind", ""),
                "remedy_path": remedy.get("relative_path", ""),
                "remedy_sha256": remedy.get("sha256", ""),
                "remedy": remedy,
            }
        )
    return {"found": True, "pending": rows, "count": len(rows)}


__all__ = ["build_proposer_recommendations", "build_proposer_scorecard"]
