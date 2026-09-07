"""The recorded mutation enumeration, independent of retained source trees."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zicato.core.mutation import MutationPoint
from zicato.epoch._storage import RecordError
from zicato.storage._atomic import atomic_write_text


def mutation_inventory_from_payload(raw: object) -> list[dict[str, Any]]:
    """Accept complete snapshot rows, retaining unknown extension fields."""
    if not isinstance(raw, list):
        raise ValueError("mutation inventory must be an array")
    seen = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("mutation inventory entry must be an object")
        for field in ("id", "kind", "file", "content", "content_hash"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"mutation inventory {field} must be a string")
        if not row["id"] or not row["file"] or row["id"] in seen:
            raise ValueError("mutation inventory requires nonempty unique ids and filenames")
        seen.add(row["id"])
        if row["kind"] not in ("span", "file", "code"):
            raise ValueError("mutation inventory kind must be span, file, or code")
        for field in ("line_start", "line_end"):
            if type(row.get(field)) is not int or row[field] < 1:
                raise ValueError(f"mutation inventory {field} must be a positive integer")
        if row["line_end"] < row["line_start"]:
            raise ValueError("mutation inventory line_end precedes line_start")
    return raw


def read_mutation_inventory(path: Path) -> list[dict[str, Any]] | None:
    """Preserve absence; refuse a malformed present inventory as a whole."""
    try:
        return mutation_inventory_from_payload(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"{path}: {exc}") from exc


def write_mutation_inventory(path: Path, entries: Sequence[MutationPoint | dict[str, Any]]) -> None:
    """Replace a complete inventory, preserving the producer's field order and bytes."""
    body = [
        {
            "id": point.id,
            "kind": point.kind,
            "file": str(point.file),
            "line_start": point.line_start,
            "line_end": point.line_end,
            "content": point.content,
            "content_hash": point.content_hash,
        }
        if isinstance(point, MutationPoint)
        else point
        for point in entries
    ]
    mutation_inventory_from_payload(body)
    atomic_write_text(path, json.dumps(body, indent=2, sort_keys=False) + "\n")
