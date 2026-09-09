"""Canonical cross-epoch ancestry, generation disposition, and settlement facts.

Epochs retain their predecessor coordinates. Each generation retains its parent,
birth round, and tri-state disposition: promoted, rejected, or unresolved.
Absent historical fields stay omitted in the encoded document; numeric zero is
an observed scalar, never a missing-value sentinel. Present malformed records
are refused before any mutation can discard history.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.types import EpochConfig, Generation
from zicato.epoch._storage import (
    RECORD_FORMAT_VERSION,
    RecordError,
    check_record_format,
    lineage_key,
)
from zicato.storage import workspace_backend
from zicato.workspace.projection import mark_epoch_changed


@dataclass(frozen=True, slots=True)
class LineageGeneration:
    """One generation's recorded ancestry and disposition, with absent facts explicit."""

    id: str
    parent_id: str | None
    promoted: bool | None
    created_at: str
    round_index: int | None
    rejection_reason: str | None
    parent_scalar: int | float | None
    child_scalar: int | float | None
    delta_scalar: int | float | None
    _json: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached projection with historical omissions preserved."""
        body: dict[str, Any] = json.loads(self._json)
        for key in (
            "id",
            "parent_id",
            "promoted",
            "created_at",
            "round_index",
            "rejection_reason",
            "parent_scalar",
            "child_scalar",
            "delta_scalar",
        ):
            value = getattr(self, key)
            if key in body or value != ("" if key == "created_at" else None):
                body[key] = value
        return body


@dataclass(frozen=True, slots=True)
class LineageEpoch:
    id: str
    name: str
    started_at: str
    closed_at: str
    v0_parent: str | None
    generations: tuple[LineageGeneration, ...]
    _json: str = field(repr=False)

    @property
    def parent_epoch_id(self) -> str | None:
        return self.v0_parent.split(":", 1)[0] if self.v0_parent else None

    def generation(self, generation_id: str) -> LineageGeneration | None:
        return next((row for row in self.generations if row.id == generation_id), None)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self._json)
        for key in ("id", "name", "started_at", "closed_at", "v0_parent"):
            value = getattr(self, key)
            if key in body or value != (None if key == "v0_parent" else ""):
                body[key] = value
        body["generations"] = [generation.to_dict() for generation in self.generations]
        return body


@dataclass(frozen=True, slots=True)
class Lineage:
    epochs: tuple[LineageEpoch, ...]
    _json: str = field(repr=False)
    exists: bool = True

    def epoch(self, epoch_id: str) -> LineageEpoch | None:
        return next((row for row in self.epochs if row.id == epoch_id), None)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self._json)
        body["epochs"] = [epoch.to_dict() for epoch in self.epochs]
        return body


def _string(row: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = row.get(key, "")
    if not isinstance(value, str) or (required and not value):
        raise RecordError(f"lineage.json: invalid {key}")
    return value


def _parent(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise RecordError(f"lineage.json: {key} must be a nonempty string or null")
    return value


def _scalar(row: dict[str, Any], key: str) -> int | float | None:
    value = row.get(key)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value)
    ):
        raise RecordError(f"lineage.json: {key} must be finite or null")
    return value


def decode_lineage(value: Any) -> Lineage:
    """Accept the version-1 graph without fabricating absent historical fields."""
    if not isinstance(value, dict) or not isinstance(value.get("epochs"), list):
        raise RecordError("lineage.json: expected an object with an epochs list")
    check_record_format(value, "lineage.json")
    epochs: list[LineageEpoch] = []
    epoch_ids: set[str] = set()
    for entry in value["epochs"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("generations"), list):
            raise RecordError("lineage.json: each epoch requires a generations list")
        epoch_id = _string(entry, "id", required=True)
        if epoch_id in epoch_ids:
            raise RecordError(f"lineage.json: duplicate epoch {epoch_id!r}")
        epoch_ids.add(epoch_id)
        generations: list[LineageGeneration] = []
        ids: set[str] = set()
        for row in entry["generations"]:
            if not isinstance(row, dict):
                raise RecordError(f"lineage.json: epoch {epoch_id!r} has a non-object generation")
            generation_id = _string(row, "id", required=True)
            if generation_id in ids:
                raise RecordError(
                    f"lineage.json: epoch {epoch_id!r} contains duplicate generation ids"
                )
            ids.add(generation_id)
            promoted = row.get("promoted")
            if promoted is not None and not isinstance(promoted, bool):
                raise RecordError(f"lineage generation {generation_id!r} has an invalid verdict")
            round_index = row.get("round_index")
            if round_index is not None and (
                isinstance(round_index, bool) or not isinstance(round_index, int) or round_index < 0
            ):
                raise RecordError(
                    f"lineage.json: generation {generation_id!r} has an invalid round_index"
                )
            reason = _string(row, "rejection_reason") if "rejection_reason" in row else None
            generations.append(
                LineageGeneration(
                    generation_id,
                    _parent(row, "parent_id"),
                    promoted,
                    _string(row, "created_at"),
                    round_index,
                    reason,
                    _scalar(row, "parent_scalar"),
                    _scalar(row, "child_scalar"),
                    _scalar(row, "delta_scalar"),
                    json.dumps(row, allow_nan=False),
                )
            )
        epochs.append(
            LineageEpoch(
                epoch_id,
                _string(entry, "name"),
                _string(entry, "started_at"),
                _string(entry, "closed_at"),
                _parent(entry, "v0_parent"),
                tuple(generations),
                json.dumps(entry, allow_nan=False),
            )
        )
    return Lineage(tuple(epochs), json.dumps(value, allow_nan=False))


def load_lineage(workspace_root: Path) -> Lineage:
    """Read candidate ancestry and apply the outcomes of committed rounds."""
    from zicato.epoch.settlement_receipt import iter_settlement_receipts

    if not workspace_backend(workspace_root, start=False).exists(lineage_key()):
        return Lineage((), '{"epochs": []}', exists=False)
    raw = _load_raw(workspace_root)
    for epoch in raw["epochs"]:
        by_id = {row["id"]: row for row in epoch["generations"]}
        for receipt in iter_settlement_receipts(workspace_root, epoch["id"]):
            if receipt.state != "committed":
                continue
            for candidate in receipt.candidates:
                row = by_id.get(candidate.generation_id)
                if row is None:
                    raise RecordError("round result names a candidate absent from ancestry")
                if row["round_index"] != receipt.round_index:
                    raise RecordError("round result disagrees with candidate birth round")
                outcome = candidate.outcome
                row.update(
                    promoted=outcome.tournament_decision == "promoted",
                    rejection_reason=outcome.rejection_reason,
                    parent_scalar=candidate.parent_scalar,
                    child_scalar=candidate.child_scalar,
                    delta_scalar=(
                        candidate.child_scalar - candidate.parent_scalar
                        if candidate.child_scalar is not None
                        and candidate.parent_scalar is not None
                        else None
                    ),
                )
    return decode_lineage(raw)


def _replace_lineage(workspace_root: Path, lineage: Lineage, text: str) -> None:
    before = {row.id: row.to_dict() for row in decode_lineage(_load_raw(workspace_root)).epochs}
    after = {row.id: row.to_dict() for row in lineage.epochs}
    for epoch_id in sorted(before.keys() | after.keys()):
        if json.dumps(before.get(epoch_id), sort_keys=True) != json.dumps(
            after.get(epoch_id), sort_keys=True
        ):
            mark_epoch_changed(workspace_root, epoch_id)
    workspace_backend(workspace_root, start=False).write_text(lineage_key(), text)


def write_lineage(workspace_root: Path, lineage: Lineage) -> None:
    """Mark changed epoch projections before atomically replacing the graph."""
    accepted = decode_lineage(lineage.to_dict())
    _replace_lineage(
        workspace_root, accepted, json.dumps(accepted.to_dict(), indent=2, sort_keys=True)
    )


def initialize_lineage(workspace_root: Path) -> None:
    """Write the empty graph with the initialization format's final newline."""
    empty = decode_lineage({"format_version": RECORD_FORMAT_VERSION, "epochs": []})
    _replace_lineage(
        workspace_root, empty, json.dumps(empty.to_dict(), indent=2, sort_keys=True) + "\n"
    )


def _load_raw(workspace_root: Path) -> dict[str, Any]:
    """Read authored ancestry without copying round outcomes into it."""
    try:
        text = workspace_backend(workspace_root, start=False).read_text(lineage_key())
        return (
            decode_lineage(json.loads(text)).to_dict()
            if text is not None
            else {"format_version": RECORD_FORMAT_VERSION, "epochs": []}
        )
    except (OSError, ValueError) as exc:
        raise RecordError(f"lineage.json: {exc}") from exc


def _save_raw(workspace_root: Path, raw: dict[str, Any]) -> None:
    raw["format_version"] = RECORD_FORMAT_VERSION
    write_lineage(workspace_root, decode_lineage(raw))


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

    ``parent_epoch_id`` names the predecessor epoch, optionally followed by
    ``:generation`` when a retained generation seeds the epoch. The recorded
    coordinates are preserved verbatim in ``v0_parent``.
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


def discard_pending_generations(
    workspace_root: Path,
    epoch_id: str,
    generation_ids: set[str],
) -> tuple[str, ...]:
    """Remove unresolved lineage nodes during conservative crash cleanup.

    Candidate creation records an applied challenger with ``promoted=null``.
    If a multi-challenger process dies before it persists a settlement
    receipt, recovery has no trustworthy tournament decision to replay and
    must discard the whole field. This mutator removes only the named pending
    nodes in one atomic lineage rewrite. A named resolved node is a conflict,
    not cleanup material, so validation completes before anything is removed.
    """
    if not generation_ids:
        return ()
    raw = _load_raw(workspace_root)
    entry = _find_epoch(raw, epoch_id)
    if entry is None:
        return ()
    generations, existing = _indexed_generation_rows(entry, epoch_id)
    resolved = sorted(
        generation_id
        for generation_id in generation_ids
        if generation_id in existing and existing[generation_id].get("promoted") is not None
    )
    if resolved:
        raise RuntimeError(
            "refusing to discard resolved lineage generations: " + ", ".join(resolved)
        )
    removed = tuple(
        row["id"]
        for row in generations
        if isinstance(row, dict) and row.get("id") in generation_ids
    )
    if not removed:
        return ()
    entry["generations"] = [
        generation
        for generation in generations
        if not isinstance(generation, dict) or generation.get("id") not in generation_ids
    ]
    _save_raw(workspace_root, raw)
    return removed


def validate_generation_resolutions(
    workspace_root: Path,
    epoch_id: str,
    resolutions: dict[str, dict[str, Any]],
    *,
    require_resolved: bool,
) -> None:
    """Validate settlement facts against lineage without changing the DAG."""
    _validated_resolution_rows(
        workspace_root, epoch_id, resolutions, require_resolved=require_resolved
    )


def _validated_resolution_rows(
    workspace_root: Path,
    epoch_id: str,
    resolutions: dict[str, dict[str, Any]],
    *,
    require_resolved: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    lineage = load_lineage(workspace_root)
    validate_generation_resolution_rows(
        lineage, epoch_id, resolutions, require_resolved=require_resolved
    )
    raw = lineage.to_dict()
    entry = _find_epoch(raw, epoch_id)
    assert entry is not None
    return raw, {row["id"]: row for row in entry["generations"]}


def validate_generation_resolution_rows(
    lineage: Lineage,
    epoch_id: str,
    resolutions: dict[str, dict[str, Any]],
    *,
    require_resolved: bool,
) -> dict[str, LineageGeneration]:
    """Compare settlement facts with loaded lineage rows without reading files."""
    if not resolutions:
        raise ValueError("lineage resolution requires at least one generation")
    entry = lineage.epoch(epoch_id)
    if entry is None:
        raise RuntimeError(f"lineage does not contain exactly one epoch {epoch_id!r}")
    by_id = {row.id: row for row in entry.generations}

    for generation_id, resolution in resolutions.items():
        current = by_id.get(generation_id)
        if current is None:
            raise RuntimeError(f"lineage lacks settlement generation {generation_id!r}")
        for key in ("parent_id", "created_at", "round_index"):
            if getattr(current, key) != resolution[key]:
                raise RuntimeError(f"lineage generation {generation_id!r} conflicts on {key}")
        promoted = current.promoted
        if promoted is not None and promoted is not resolution["promoted"]:
            raise RuntimeError(f"lineage generation {generation_id!r} has a different verdict")
        if promoted is not None:
            parent_scalar = resolution["parent_scalar"]
            child_scalar = resolution["child_scalar"]
            expected = {
                "rejection_reason": resolution["rejection_reason"] if promoted is False else "",
                "parent_scalar": parent_scalar,
                "child_scalar": child_scalar,
                "delta_scalar": (
                    child_scalar - parent_scalar
                    if child_scalar is not None and parent_scalar is not None
                    else None
                ),
            }
            if any(getattr(current, key) != value for key, value in expected.items()):
                raise RuntimeError(
                    f"lineage generation {generation_id!r} has different settlement facts"
                )
        if require_resolved and promoted is None:
            raise RuntimeError(f"lineage generation {generation_id!r} lacks its settlement verdict")
    return by_id


def _indexed_generation_rows(
    entry: dict[str, Any],
    epoch_id: str,
) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    """Index an already accepted epoch's detached mutation rows."""
    del epoch_id
    generations = entry["generations"]
    return generations, {row["id"]: row for row in generations}


# ---------------------------------------------------------------------------
# Read-side
# ---------------------------------------------------------------------------


def render_lineage_summary(workspace_root: Path) -> str:
    """Render the accepted graph as the epoch list's Markdown table."""
    lineage = load_lineage(workspace_root)
    if not lineage.epochs:
        return "# Lineage\n\n(no epochs recorded yet)\n"
    rows = [
        "# Lineage",
        "",
        "| epoch | started_at | closed_at | promoted | rejected | parent |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in lineage.epochs:
        promoted = sum(g.promoted is True for g in entry.generations)
        rejected = sum(g.promoted is False and g.id != "v0" for g in entry.generations)
        rows.append(
            f"| {entry.id} | {entry.started_at} | {entry.closed_at or '(open)'} | "
            f"{promoted} | {rejected} | {entry.v0_parent or '(root)'} |"
        )
    return "\n".join([*rows, ""])


__all__ = [
    "Lineage",
    "LineageEpoch",
    "LineageGeneration",
    "decode_lineage",
    "write_lineage",
    "initialize_lineage",
    "register_epoch",
    "mark_closed",
    "append_to_lineage",
    "discard_pending_generations",
    "validate_generation_resolutions",
    "load_lineage",
    "render_lineage_summary",
]
