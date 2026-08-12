"""The staged-recommendation queue — how an applied edit reaches an epoch record.

``zicato proposer apply-recommendation`` writes a drafted skill into the
proposer dir. That edit rolls the contract hash on its own (the proposer dir is
a hashed contract input, PROPOSER.md §4), so the NEXT ``evolve`` opens a fresh
epoch. This queue is the one-field handoff between those two moments: apply
parks the recommendation id, and the epoch that opens next drains it into its
own record, so proposer lineage says *why* the proposer changed.

Why a queue rather than a direct write: apply cannot know which epoch will pick
its edit up. The operator may apply three recommendations before rolling, or
apply one and never roll at all. A queue answers both cases correctly — the
epoch that actually runs under the edited proposer gets all the ids, and an
unrolled edit's id stays pending (and stays out of the pending-recommendation
listing, because it HAS been applied).

Kept in its own module, importing nothing from :mod:`zicato.epoch`, so the
epoch lifecycle can drain it at creation time without an import cycle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from zicato.core.workspace import proposer_staged_recommendations_path


def _read(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("recommendation_ids")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _write(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"recommendation_ids": ids}, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp, path)


def staged_recommendations(workspace_root: Path) -> tuple[str, ...]:
    """The ids applied into the proposer dir but not yet claimed by an epoch."""
    return tuple(_read(proposer_staged_recommendations_path(workspace_root)))


def stage_recommendation(workspace_root: Path, recommendation_id: str) -> tuple[str, ...]:
    """Park ``recommendation_id`` for the next epoch; return the queue after.

    Idempotent — re-applying the same recommendation does not double-stamp the
    epoch record, and the id's position (first-applied order) is preserved.
    """
    path = proposer_staged_recommendations_path(workspace_root)
    ids = _read(path)
    if recommendation_id not in ids:
        ids.append(recommendation_id)
        _write(path, ids)
    return tuple(ids)


def drain_staged_recommendations(workspace_root: Path) -> tuple[str, ...]:
    """Return the queue and clear it; ``()`` when nothing is staged.

    Called by :func:`zicato.epoch.lifecycle.new_epoch`. Clearing by REMOVING
    the file (rather than writing an empty list) keeps "nothing staged" a
    single on-disk state, so a workspace that never applied a recommendation
    and one that has since rolled read identically.
    """
    path = proposer_staged_recommendations_path(workspace_root)
    ids = _read(path)
    if path.exists():
        path.unlink(missing_ok=True)
    return tuple(ids)


__all__ = [
    "drain_staged_recommendations",
    "stage_recommendation",
    "staged_recommendations",
]
