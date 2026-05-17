"""Cross-epoch lineage DAG persisted as a single ``lineage.json`` file.

The DAG is shallow by construction:

* Epochs are linear — at any time exactly one epoch is "current".
* Generations within an epoch form a linear chain (``v0``, ``v1``, ...).
* The cross-cutting edge is ``epoch.v0_parent`` pointing at
  ``previous_epoch:final_generation``, recording how a fresh epoch's
  baseline relates to the closed predecessor's lineage head.

We persist the whole DAG as one JSON document. The size never grows
faster than the number of generations across the lifetime of the
workspace; for the foreseeable scale (hundreds of generations per
epoch, low single digits of epochs per week) a single file with atomic
rewrites is the simplest correct thing.

File shape::

    {
      "epochs": [
        {
          "id": "2026-04-01_initial",
          "name": "initial",
          "started_at": "2026-04-01T10:00:00+00:00",
          "closed_at": "",
          "v0_parent": null,
          "generations": [
            {"id": "v0", "parent_id": null, "promoted": true,  "created_at": "..."},
            {"id": "v1", "parent_id": "v0", "promoted": true,  "created_at": "..."},
            {"id": "v2", "parent_id": "v1", "promoted": false, "created_at": "..."}
          ]
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.core.types import EpochConfig, Generation
from zicato.epoch._storage import backend_for, lineage_key


def _empty() -> dict[str, Any]:
    return {"epochs": []}


def _load_raw(workspace_root: Path) -> dict[str, Any]:
    """Read ``lineage.json`` through the storage backend.

    A missing file, an unreadable file, or a malformed document all
    collapse to the empty DAG — the lineage file is rebuilt forward by
    the mutators, so a tolerant read keeps a one-off corruption from
    wedging the loop. (This is intentionally more forgiving than the
    storage backend's default ``read_json``, which surfaces a decode
    error; lineage is reconstructible, so it absorbs the error here.)
    """
    backend = backend_for(workspace_root)
    try:
        d = backend.read_json(lineage_key())
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(d, dict) or "epochs" not in d:
        return _empty()
    return d


def _save_raw(workspace_root: Path, raw: dict[str, Any]) -> None:
    """Atomically write ``lineage.json`` through the storage backend."""
    backend_for(workspace_root).write_json(lineage_key(), raw)


def _find_epoch(raw: dict[str, Any], epoch_id: str) -> dict[str, Any] | None:
    for entry in raw["epochs"]:
        if entry.get("id") == epoch_id:
            result: dict[str, Any] = entry
            return result
    return None


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


def register_epoch(
    workspace_root: Path,
    cfg: EpochConfig,
    parent_epoch_id: str | None,
) -> None:
    """Append a new epoch entry to ``lineage.json``.

    ``parent_epoch_id`` is the id of the epoch this one was forked off of
    (commonly the immediately previous epoch). Stored verbatim as
    ``v0_parent`` for now; we will populate the ``{epoch}:{gen}`` form
    once the runner registers ``v0`` for the new epoch via
    :func:`append_to_lineage`.
    """
    raw = _load_raw(workspace_root)
    if _find_epoch(raw, cfg.id) is not None:
        # Idempotent — re-registering the same epoch is a no-op.
        return
    raw["epochs"].append(
        {
            "id": cfg.id,
            "name": cfg.name,
            "started_at": cfg.created_at,
            "closed_at": cfg.closed_at,
            "v0_parent": parent_epoch_id,
            "generations": [],
        }
    )
    _save_raw(workspace_root, raw)


def mark_closed(workspace_root: Path, epoch_id: str, closed_at: str) -> None:
    """Stamp an epoch's ``closed_at`` field."""
    raw = _load_raw(workspace_root)
    entry = _find_epoch(raw, epoch_id)
    if entry is None:
        return
    entry["closed_at"] = closed_at
    _save_raw(workspace_root, raw)


def append_to_lineage(
    workspace_root: Path,
    epoch_id: str,
    generation: Generation,
    parent_id: str | None,
) -> None:
    """Record a generation under its epoch.

    ``parent_id`` is the generation id this one was forked from
    (``None`` for ``v0``). We trust the caller's value rather than
    re-deriving from ``Generation.parent_id`` so the runner can record
    cross-epoch parents (e.g. a fresh epoch's ``v0`` whose parent is
    ``initial:v7``) if it ever needs to.
    """
    raw = _load_raw(workspace_root)
    entry = _find_epoch(raw, epoch_id)
    if entry is None:
        # Auto-create a thin entry — the runner sometimes lands a
        # generation before lineage knows about its epoch (tests).
        entry = {
            "id": epoch_id,
            "name": "",
            "started_at": generation.created_at,
            "closed_at": "",
            "v0_parent": None,
            "generations": [],
        }
        raw["epochs"].append(entry)
    # Update-in-place if the generation already exists.
    for g in entry["generations"]:
        if g.get("id") == generation.id:
            g["parent_id"] = parent_id
            g["promoted"] = generation.promoted
            g["created_at"] = generation.created_at
            _save_raw(workspace_root, raw)
            return
    entry["generations"].append(
        {
            "id": generation.id,
            "parent_id": parent_id,
            "promoted": generation.promoted,
            "created_at": generation.created_at,
        }
    )
    _save_raw(workspace_root, raw)


# ---------------------------------------------------------------------------
# Read-side
# ---------------------------------------------------------------------------


def load_lineage(workspace_root: Path) -> dict[str, Any]:
    """Return the full lineage DAG as a nested dict (a deep copy)."""
    result: dict[str, Any] = json.loads(json.dumps(_load_raw(workspace_root)))
    return result


def render_lineage_summary(workspace_root: Path) -> str:
    """Format the lineage as a human-friendly markdown table.

    Operators read this via ``zicato epoch list``; downstream code reads
    structured data via :func:`load_lineage`.
    """
    raw = _load_raw(workspace_root)
    epochs = raw.get("epochs", [])
    if not epochs:
        return "# Lineage\n\n(no epochs recorded yet)\n"

    rows: list[str] = []
    rows.append("# Lineage")
    rows.append("")
    rows.append("| epoch | started_at | closed_at | promoted | rejected | parent |")
    rows.append("| --- | --- | --- | --- | --- | --- |")
    for entry in epochs:
        gens = entry.get("generations", [])
        promoted = sum(1 for g in gens if g.get("promoted"))
        rejected = sum(1 for g in gens if not g.get("promoted") and g.get("id") != "v0")
        parent = entry.get("v0_parent") or "(root)"
        closed = entry.get("closed_at") or "(open)"
        rows.append(
            f"| {entry.get('id', '')} | "
            f"{entry.get('started_at', '')} | "
            f"{closed} | "
            f"{promoted} | "
            f"{rejected} | "
            f"{parent} |"
        )
    rows.append("")
    return "\n".join(rows)


__all__ = [
    "register_epoch",
    "mark_closed",
    "append_to_lineage",
    "load_lineage",
    "render_lineage_summary",
]
