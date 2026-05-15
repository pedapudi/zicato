"""JSONL parser / serializer for boards of :class:`BoardEntry` rows.

The board file format is one JSON object per line, as specified in
``docs/design/BOARD-FORMAT.md``. The discriminated-union shape of
:class:`~zicato.core.BoardEntry` means the same row can carry different
field subsets depending on ``kind``; this module handles the asymmetry
on both the read and the write side so callers can treat the on-disk
format and the in-memory dataclass interchangeably.

Public surface
--------------

* :func:`load_board` — read a JSONL file, validate every row through
  :func:`zicato.core.validate_board_entry`, and reject duplicate ids.
* :func:`save_board` — serialize a list of :class:`BoardEntry` back to
  JSONL, emitting only the keys relevant to each entry's ``kind``.
* :func:`append_entry` — append one entry without re-reading the whole
  file (used by ``zicato board add``).
* :func:`remove_entry` — remove one entry by id, rewriting the file
  (used by ``zicato board remove``).

Design notes
------------

JSONL is parsed strictly: trailing whitespace and blank lines are
tolerated, but a malformed JSON object on any line raises with that
line's number for operator-facing diagnosability. Duplicate ids are
caught at parse time rather than at run time so the operator sees the
failure as soon as ``zicato board add`` rejects a write.

Serialization is also strict: only the discriminant-relevant fields are
written so the file does not accumulate noise from optional fields that
were never set. The exact key set per kind matches the documented
schema in ``BOARD-FORMAT.md`` so the file remains hand-editable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core.types import BoardEntry, validate_board_entry


def load_board(path: Path) -> list[BoardEntry]:
    """Parse a JSONL board file into validated :class:`BoardEntry` rows.

    Parameters
    ----------
    path:
        Filesystem path to the JSONL file.

    Returns
    -------
    list[BoardEntry]
        One entry per non-blank line in the input.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If any line is malformed JSON, any entry fails discriminant
        validation, or two entries share an ``id``. The error message
        carries the offending line number (1-indexed) when applicable.
    """
    path = Path(path)
    entries: list[BoardEntry] = []
    seen_ids: set[str] = set()

    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}: line {line_no}: malformed JSON: {exc.msg}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"{path}: line {line_no}: expected a JSON object, got "
                    f"{type(payload).__name__}"
                )
            try:
                entry = validate_board_entry(payload)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"{path}: line {line_no}: invalid entry: {exc}"
                ) from exc
            if entry.id in seen_ids:
                raise ValueError(
                    f"{path}: line {line_no}: duplicate entry id {entry.id!r}"
                )
            seen_ids.add(entry.id)
            entries.append(entry)

    return entries


def _entry_to_dict(entry: BoardEntry) -> dict[str, Any]:
    """Serialize one entry, emitting only the keys relevant to its kind."""
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
    }
    # Optional envelope fields — only emit if they carry signal.
    if entry.weight != 1.0:
        out["weight"] = entry.weight
    if entry.tags:
        out["tags"] = list(entry.tags)
    if entry.context:
        # Mapping → plain dict for json.dumps.
        out["context"] = dict(entry.context)
    if entry.expectation is not None:
        exp_dict: dict[str, Any] = {
            "kind": entry.expectation.kind,
            "spec": entry.expectation.spec,
        }
        # Only emit fires_on when non-default so JSON round-trips cleanly.
        if entry.expectation.fires_on != "final_output":
            exp_dict["fires_on"] = entry.expectation.fires_on
        out["expectation"] = exp_dict

    # Per-kind discriminant fields.
    if entry.kind == "single_turn":
        out["input"] = entry.input
    elif entry.kind == "multi_turn_scripted":
        out["turns"] = [{"user": t.user} for t in (entry.turns or ())]
        out["max_turns"] = entry.max_turns
    elif entry.kind == "multi_turn_emulated":
        persona = entry.user_persona
        # validate() guarantees persona is set for this kind.
        assert persona is not None
        out["user_persona"] = {
            "goal": persona.goal,
            "constraints": persona.constraints,
            "stop_when": persona.stop_when,
        }
        out["max_turns"] = entry.max_turns
    elif entry.kind == "synthetic_adversarial":
        out["input"] = entry.input
        out["adversarial_agent_spec"] = entry.adversarial_agent_spec
        out["required_drift_kinds"] = list(entry.required_drift_kinds or ())
    elif entry.kind == "synthetic_clean":
        out["input"] = entry.input
    else:  # pragma: no cover — kind is Literal-typed
        raise ValueError(f"unknown entry kind {entry.kind!r}")

    return out


def save_board(entries: list[BoardEntry], path: Path) -> None:
    """Serialize a list of :class:`BoardEntry` to ``path`` as JSONL.

    The output is overwritten atomically: a sibling ``.tmp`` file is
    written first, then renamed over the target. This keeps a partial
    write from corrupting an existing board.

    Each row contains only the keys relevant to that entry's ``kind`` —
    optional fields with default values are omitted for cleanliness.

    Parameters
    ----------
    entries:
        The entries to write, in order.
    path:
        Destination path. Parent directory must already exist.

    Raises
    ------
    ValueError
        If two entries in ``entries`` share an id.
    """
    path = Path(path)
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.id in seen_ids:
            raise ValueError(f"duplicate entry id {entry.id!r} in entries to save")
        seen_ids.add(entry.id)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            row = _entry_to_dict(entry)
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
    tmp_path.replace(path)


def append_entry(path: Path, entry: BoardEntry) -> None:
    """Append a single validated entry to a JSONL board file.

    The entry's id is checked against the existing file; appending an
    id that already exists raises. The validate-then-append flow is
    explicit so ``zicato board add`` can surface schema errors before
    the file is touched.

    Parameters
    ----------
    path:
        Destination board file. Created if it does not exist; the
        parent directory must already exist.
    entry:
        The validated :class:`BoardEntry` to append.

    Raises
    ------
    ValueError
        If ``entry.id`` is already present in the existing file or if
        the in-memory entry fails ``validate``.
    """
    path = Path(path)
    entry.validate()
    if path.exists():
        existing = load_board(path)
        if any(e.id == entry.id for e in existing):
            raise ValueError(
                f"{path}: entry id {entry.id!r} already exists in board"
            )

    row = _entry_to_dict(entry)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def remove_entry(path: Path, entry_id: str) -> None:
    """Remove an entry by id from a JSONL board file.

    The file is rewritten without the matching row. Raises if no row
    has the given id (rather than silently no-op'ing) so the CLI can
    surface a clear error.

    Parameters
    ----------
    path:
        Board file to mutate.
    entry_id:
        The id to remove.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If no entry with ``entry_id`` is present.
    """
    path = Path(path)
    entries = load_board(path)
    new_entries = [e for e in entries if e.id != entry_id]
    if len(new_entries) == len(entries):
        raise ValueError(f"{path}: no entry with id {entry_id!r} to remove")
    save_board(new_entries, path)


__all__ = [
    "load_board",
    "save_board",
    "append_entry",
    "remove_entry",
]


# ``asdict`` and ``Mapping`` referenced for forward-compat with future
# adapters that want to lean on the dataclass machinery directly.
_asdict_ref: Any = asdict
_mapping_ref: Any = Mapping
