"""Persist optional outcome-summarizer failures for epoch health reports."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zicato.storage import atomic_write_json, read_json
from zicato.workspace import WorkspaceLayout

log = logging.getLogger(__name__)


def record_summarizer_failure(
    workspace_root: Path, epoch_id: str, round_index: int, spec: str, error: str
) -> None:
    """Retain one failure per round without affecting the round's measurement."""
    path = (
        WorkspaceLayout.from_root(workspace_root).health_dir(epoch_id)
        / f"summarizer-round-{round_index}.json"
    )
    try:
        atomic_write_json(
            path,
            {
                "epoch_id": epoch_id,
                "round_index": round_index,
                "spec": spec,
                "error": error,
            },
        )
    except OSError as exc:
        log.warning("outcome_summarizer_evidence_failed: %s: %s", path, exc)


def epoch_summarizer_failures(workspace_root: Path, epoch_id: str) -> tuple[dict[str, Any], ...]:
    """Read only the selected epoch's retained optional-hook failures."""
    records: list[dict[str, Any]] = []
    for path in sorted(
        WorkspaceLayout.from_root(workspace_root)
        .health_dir(epoch_id)
        .glob("summarizer-round-*.json")
    ):
        try:
            raw = read_json(path)
            if not isinstance(raw, dict) or raw.get("epoch_id") != epoch_id:
                raise ValueError("failure record does not name the selected epoch")
            if type(raw.get("round_index")) is not int or not all(
                isinstance(raw.get(key), str) for key in ("spec", "error")
            ):
                raise ValueError("failure record has invalid round, spec, or error fields")
            records.append(raw)
        except (OSError, ValueError) as exc:
            records.append({"path": str(path), "error": f"unreadable failure evidence: {exc}"})
    return tuple(records)
