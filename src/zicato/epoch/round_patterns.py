"""The per-round pattern record: the detector findings one evolve round computed.

Each round runs the pattern detectors over its parent generation's
training-slice loss profiles and passes the findings to the proposer. The
round also stores them as ``epochs/{epoch}/rounds/{round}/patterns.json``
so the close-of-epoch retrospective can report which failure patterns each
round observed and which persisted to the end of the epoch.

The record holds the unrestricted :class:`~zicato.core.patterns.Pattern`
fields, including entry ids. Storing them exposes nothing new: the loss and
event records the detectors read are already in the same workspace, and the
retrospective prompt goes to the evaluation model, never to the proposer.

This module is the one declaration of the record's shape. The writer encodes
through the same validation the reader applies, so a pattern that could not
be read back is never written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core.patterns import Pattern
from zicato.epoch._storage import RecordError
from zicato.storage._atomic import atomic_write_text

#: Version of the record layout. A reader refuses any other value.
FORMAT_VERSION = 1

_SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class RoundPatterns:
    """The patterns one round handed its proposer, and the generation they describe."""

    parent_generation_id: str
    patterns: tuple[Pattern, ...]


def _pattern_to_dict(pattern: Pattern) -> dict[str, Any]:
    return {
        "id": pattern.id,
        "kind": pattern.kind,
        "summary": pattern.summary,
        "detail": dict(pattern.detail),
        "affected_mutation_ids": list(pattern.affected_mutation_ids),
        "severity": pattern.severity,
    }


def pattern_from_dict(raw: object) -> Pattern:
    """Decode one pattern object; raise :class:`ValueError` for a malformed one.

    The record and ``zicato proposer propose --patterns-from`` share this
    shape. ``id`` and ``kind`` are required. The other fields default to the
    :class:`Pattern` defaults when absent, so a hand-written pattern file may
    omit them.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("pattern must be an object")
    for name in ("id", "kind"):
        if not isinstance(raw.get(name), str) or not raw[name]:
            raise ValueError(f"pattern {name} must be a nonempty string")
    summary = raw.get("summary", "")
    detail = raw.get("detail", {})
    mutation_ids = raw.get("affected_mutation_ids", [])
    severity = raw.get("severity", "info")
    if not isinstance(summary, str):
        raise ValueError(f"pattern {raw['id']!r} summary must be a string")
    if not isinstance(detail, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in detail.items()
    ):
        raise ValueError(f"pattern {raw['id']!r} detail must map strings to strings")
    if not isinstance(mutation_ids, list) or not all(isinstance(m, str) for m in mutation_ids):
        raise ValueError(f"pattern {raw['id']!r} affected_mutation_ids must be a string list")
    if severity not in _SEVERITIES:
        raise ValueError(f"pattern {raw['id']!r} severity must be one of {_SEVERITIES}")
    return Pattern(
        id=raw["id"],
        kind=raw["kind"],
        summary=summary,
        detail=dict(detail),
        affected_mutation_ids=tuple(mutation_ids),
        severity=severity,
    )


def _from_payload(raw: object) -> RoundPatterns:
    if not isinstance(raw, Mapping):
        raise ValueError("round patterns record must be an object")
    if raw.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"round patterns format_version must be {FORMAT_VERSION}")
    parent = raw.get("parent_generation_id")
    if not isinstance(parent, str) or not parent:
        raise ValueError("round patterns parent_generation_id must be a nonempty string")
    patterns = raw.get("patterns")
    if not isinstance(patterns, list):
        raise ValueError("round patterns patterns must be an array")
    return RoundPatterns(parent, tuple(pattern_from_dict(p) for p in patterns))


def write_round_patterns(
    path: Path, *, parent_generation_id: str, patterns: Sequence[Pattern]
) -> None:
    """Replace one round's pattern record atomically.

    Raises :class:`ValueError` for a pattern the reader would refuse, such as
    a detector detail value that is not a string; nothing is written then.
    """
    body = {
        "format_version": FORMAT_VERSION,
        "parent_generation_id": parent_generation_id,
        "patterns": [_pattern_to_dict(p) for p in patterns],
    }
    _from_payload(body)
    atomic_write_text(path, json.dumps(body, indent=2, sort_keys=True) + "\n")


def read_round_patterns(path: Path) -> RoundPatterns | None:
    """Read one round's pattern record; ``None`` when the round wrote none.

    Raises :class:`RecordError` for a record that is present but unreadable.
    """
    try:
        return _from_payload(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"{path}: {exc}") from exc


__all__ = [
    "FORMAT_VERSION",
    "RoundPatterns",
    "pattern_from_dict",
    "read_round_patterns",
    "write_round_patterns",
]
