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
      "format_version": 1,
      "epochs": [
        {
          "id": "2026-04-01_initial",
          "name": "initial",
          "started_at": "2026-04-01T10:00:00+00:00",
          "closed_at": "",
          "v0_parent": null,
          "generations": [
            {
              "id": "v2",
              "parent_id": "v1",
              "promoted": false,
              "created_at": "2026-04-01T11:00:00+00:00",
              "round_index": 3,
              "rejection_reason": "insufficient improvement: 0.7328 vs 0.7188 (margin 0.0200)",
              "parent_scalar": 0.7188,
              "child_scalar": 0.7328,
              "delta_scalar": 0.014
            },
            ...
          ]
        },
        ...
      ]
    }

Per-generation fields:

``id`` / ``parent_id``
    The generation and the one it was forked from (``null`` for ``v0``).
``promoted``
    TRI-STATE: ``true`` promoted, ``false`` a settled dead branch,
    ``null`` an applied-but-unresolved in-flight challenger.
``created_at``
    ISO-8601 UTC birth timestamp.
``round_index``
    The evolve round that MINTED the generation; once set, never
    re-stamped by a later write.
``rejection_reason``
    Why the gate cut it — non-empty ONLY when ``promoted`` is ``false``.
``parent_scalar`` / ``child_scalar`` / ``delta_scalar``
    The settling duel's two scalars and their difference; ``null`` when
    unrecorded (never ``0.0``, which is a legal measurement).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.core.types import EpochConfig, Generation
from zicato.epoch._storage import (
    RECORD_FORMAT_VERSION,
    backend_for,
    check_record_format,
    lineage_key,
)


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

    One deliberate exception: a lineage stamped with a FUTURE
    ``format_version`` is an INTACT record this build cannot promise to
    interpret — collapsing it to the empty DAG would silently drop
    history, so it refuses loudly instead.
    """
    backend = backend_for(workspace_root)
    try:
        d = backend.read_json(lineage_key())
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(d, dict) or "epochs" not in d:
        return _empty()
    check_record_format(d, "lineage.json")
    return d


def _save_raw(workspace_root: Path, raw: dict[str, Any]) -> None:
    """Atomically write ``lineage.json`` through the storage backend.

    Every save (re)stamps the record-format version — the whole document
    is rewritten atomically on each mutation, so the stamp rides along.
    """
    raw["format_version"] = RECORD_FORMAT_VERSION
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
    *,
    pending: bool = False,
    rejection_reason: str = "",
    parent_scalar: float | None = None,
    child_scalar: float | None = None,
) -> None:
    """Record a generation under its epoch.

    ``parent_id`` is the generation id this one was forked from
    (``None`` for ``v0``). We trust the caller's value rather than
    re-deriving from ``Generation.parent_id`` so the runner can record
    cross-epoch parents (e.g. a fresh epoch's ``v0`` whose parent is
    ``initial:v7``) if it ever needs to.

    ``pending`` records an APPLIED-BUT-UNRESOLVED generation: an in-flight
    challenger that has landed a snapshot (so it exists in the lineage DAG
    with its parent + birth round) but has NOT yet been crowned or cut by a
    tournament. Its ``promoted`` is persisted as ``null`` rather than the
    ``Generation.promoted`` default of ``False`` — ``False`` reads as a
    REJECTED dead branch, so an in-flight racer would otherwise render as
    rejected while it is still racing. The settle-time append (with
    ``pending=False``, the default) upserts the same node to its resolved
    ``True`` / ``False`` state; the two writes compose because the upsert
    is an idempotent update-in-place.

    ``rejection_reason`` / ``parent_scalar`` / ``child_scalar`` are the
    SETTLE event's own facts — why the gate cut this generation and the
    two numbers it compared (issue #124). They are recorded on the node
    so the DAG answers "why" without a join against every generation's
    ``experiment.json``, and they follow two rules:

    * The reason is persisted ONLY on a settled REJECTION. A caller that
      passes one for a promoted or a pending node gets ``""`` — five
      persisted surfaces already read an empty reason as "promoted", and
      a pending node that grew a reason would render as rejected, which
      is the exact ambiguity ``pending`` exists to remove. The guard is
      here rather than at the call sites so no future caller can break it.
    * The scalars use ``None`` — never ``0.0`` — for absent. A scalar of
      zero is a legal measurement, so a numeric default would make "this
      record predates the field" indistinguishable from "both sides
      scored zero" (the argument ``GateEvaluated`` already settled).
      ``delta_scalar`` is derived (child minus parent) and is ``None``
      whenever either side is.

    All three belong to the settle-time write and, like ``round_index``,
    are not blanked by a later upsert: a defence that re-records an
    already-settled generation keeps the verdict that settled it.
    """
    raw = _load_raw(workspace_root)
    # Annotated at the FIRST binding rather than on the fallback literal below: the
    # declared type then governs both branches. Annotating the literal instead
    # narrows the type to the literal's own value union (``str | list | None``)
    # and every downstream ``entry[...]`` stops checking (issue #133).
    entry: dict[str, Any] | None = _find_epoch(raw, epoch_id)
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
    # ``None`` (pending) for an applied-but-unresolved in-flight challenger;
    # the resolved ``True`` / ``False`` for the settle-time upsert.
    promoted: bool | None = None if pending else generation.promoted
    # A reason belongs to a settled REJECTION and nowhere else — see the
    # docstring. Enforced here so a caller cannot make a promoted or a
    # pending node read as rejected.
    reason = rejection_reason if promoted is False else ""
    delta_scalar = (
        child_scalar - parent_scalar
        if child_scalar is not None and parent_scalar is not None
        else None
    )
    # Update-in-place if the generation already exists.
    for g in entry["generations"]:
        if g.get("id") == generation.id:
            g["parent_id"] = parent_id
            g["promoted"] = promoted
            g["created_at"] = generation.created_at
            # ``round_index`` is the BIRTH round of the generation; once
            # set it never changes, so re-recording the same generation
            # (e.g. a later defence) keeps the original value rather than
            # re-stamping it with whatever the caller passes.
            g["round_index"] = generation.round_index
            # The settle-time facts follow the same once-set discipline:
            # the pending write has none of them, the settle write lands
            # them, and a later defence's upsert (which passes nothing)
            # must not blank the verdict that settled the generation.
            if reason:
                g["rejection_reason"] = reason
            elif promoted is not False:
                # The reason is once-set only WITHIN the rejected state: a
                # re-record of a settled rejection that passes no reason keeps
                # the verdict that settled it (``promoted`` is still False, so
                # this branch is not taken). But a node whose ``promoted`` moves
                # OFF False must not keep the reason — ``promoted`` is rewritten
                # unconditionally two lines up, and five persisted surfaces read
                # a non-empty reason as "rejected", so a stale one would make the
                # node render as rejected while its own flag says otherwise. That
                # is the ambiguity #124 exists to remove, so the invariant is
                # enforced on the RECORD rather than only on the write that
                # created it.
                g["rejection_reason"] = ""
            g.setdefault("rejection_reason", "")
            if parent_scalar is not None:
                g["parent_scalar"] = parent_scalar
            if child_scalar is not None:
                g["child_scalar"] = child_scalar
            if delta_scalar is not None:
                g["delta_scalar"] = delta_scalar
            for key in ("parent_scalar", "child_scalar", "delta_scalar"):
                g.setdefault(key, None)
            _save_raw(workspace_root, raw)
            return
    entry["generations"].append(
        {
            "id": generation.id,
            "parent_id": parent_id,
            "promoted": promoted,
            "created_at": generation.created_at,
            "round_index": generation.round_index,
            "rejection_reason": reason,
            "parent_scalar": parent_scalar,
            "child_scalar": child_scalar,
            "delta_scalar": delta_scalar,
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
        promoted = sum(1 for g in gens if g.get("promoted") is True)
        # A pending (in-flight) generation has ``promoted=None`` — it is
        # neither promoted nor rejected yet, so it counts toward neither
        # column until its tournament settles.
        rejected = sum(1 for g in gens if g.get("promoted") is False and g.get("id") != "v0")
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
