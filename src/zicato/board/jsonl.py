"""JSONL parser / serializer for boards of :class:`BoardEntry` rows.

The board file format is one JSON object per line, as specified in
``docs/design/BOARD-FORMAT.md``. The discriminated-union shape of
:class:`~zicato.core.BoardEntry` means the same row can carry different
field subsets depending on ``kind``; this module handles the asymmetry
on both the read and the write side so callers can treat the on-disk
format and the in-memory dataclass interchangeably.

Board-level metadata
--------------------

A board can carry board-level metadata that is not per-entry — currently
the :attr:`zicato.board.builder.Board.disable_drift` tuple. It is stored
as an optional **leading header line**: a JSON object carrying the
discriminant key ``"board_meta": true``. When a board has no metadata to
record, no header line is written, so simple boards stay header-free and
hand-editable. The header, when present, MUST be the first non-blank
line of the file.

Public surface
--------------

* :func:`load_board` — read a JSONL file, validate every row through
  :func:`zicato.core.validate_board_entry`, and reject duplicate ids.
* :func:`load_board_with_meta` — like :func:`load_board` but also returns
  the board-level ``disable_drift`` tuple parsed from the header.
* :func:`save_board` — serialize a list of :class:`BoardEntry` back to
  JSONL, emitting only the keys relevant to each entry's ``kind``, plus
  an optional ``board_meta`` header line.
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

Every enum-valued field — entry ``kind``, expectation ``kind`` / ``reads``,
judge ``mode`` / ``severity``, and board-level ``disable_drift`` — is
schema-validated on load: an unknown token raises a clear error listing
the valid values rather than silently constructing an out-of-domain
object. The board-authoring vocabulary renamed the old expectation
``fires_on`` field to ``reads``; an old-format board still carrying
``fires_on`` is rejected with an explicit migration error rather than
being silently accepted.

Serialization is also strict: only the discriminant-relevant fields are
written so the file does not accumulate noise from optional fields that
were never set.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from goldfive import DriftKind

from zicato.core.types import BoardEntry, validate_board_entry

#: Discriminant key that marks a JSONL line as the board-level metadata
#: header rather than a :class:`BoardEntry` row.
_BOARD_META_KEY = "board_meta"


def _coerce_disable_drift(raw: Any, where: str) -> tuple[DriftKind, ...]:
    """Coerce a raw ``disable_drift`` list into a tuple of :class:`DriftKind`.

    Raises :class:`ValueError` listing the valid drift kinds when a
    token is not a recognised :class:`goldfive.DriftKind`.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{where}: 'disable_drift' must be a list of drift-kind tokens")
    out: list[DriftKind] = []
    for token in raw:
        try:
            out.append(DriftKind(token))
        except ValueError:
            valid = ", ".join(repr(m.value) for m in DriftKind)
            raise ValueError(
                f"{where}: unknown drift kind {token!r} in 'disable_drift'; "
                f"valid values are: {valid}"
            ) from None
    return tuple(out)


def _coerce_judge_only(raw: Any, where: str) -> bool:
    """Coerce a raw ``judge_only`` header value into a bool.

    The wire form is a JSON boolean. A missing key is handled by the
    caller (defaults to ``False``); anything present that is not a real
    bool raises with a clear, line-anchored message so a typo'd header
    surfaces at load time rather than silently selecting/deselecting
    judge-only mode.
    """
    if not isinstance(raw, bool):
        raise ValueError(
            f"{where}: 'judge_only' must be a JSON boolean (true/false), "
            f"got {type(raw).__name__}"
        )
    return raw


def _reject_legacy_expectation(payload: Mapping[str, Any], where: str) -> None:
    """Raise a clear migration error if an entry carries the legacy schema.

    The board-authoring vocabulary renamed the expectation ``fires_on``
    field to ``reads``. An on-disk board still using ``fires_on`` is
    rejected here — explicitly, with a migration hint — rather than being
    silently accepted, so a stale board surfaces at load time.
    """
    expectation = payload.get("expectation")
    if isinstance(expectation, Mapping) and "fires_on" in expectation:
        raise ValueError(
            f"{where}: expectation uses the removed 'fires_on' field; "
            "rename it to 'reads' (values 'final_output' / 'conversation_end' "
            "are unchanged). This board predates the typed board-authoring API."
        )


def load_board_with_meta(
    path: Path,
) -> tuple[list[BoardEntry], tuple[DriftKind, ...], bool]:
    """Parse a JSONL board file into entries plus board-level metadata.

    Parameters
    ----------
    path:
        Filesystem path to the JSONL file.

    Returns
    -------
    tuple[list[BoardEntry], tuple[DriftKind, ...], bool]
        The validated entries (one per non-blank, non-header line), the
        board-level ``disable_drift`` tuple (empty when the file has no
        ``board_meta`` header), and the board-level ``judge_only`` flag
        (``False`` when the header is absent or omits the key).

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If any line is malformed JSON, any entry fails discriminant
        validation, an enum-valued field carries an unknown token, the
        ``board_meta`` header is not the first line, or two entries
        share an ``id``. The error message carries the offending line
        number (1-indexed) when applicable.
    """
    path = Path(path)
    entries: list[BoardEntry] = []
    seen_ids: set[str] = set()
    disable_drift: tuple[DriftKind, ...] = ()
    judge_only = False
    seen_any_row = False

    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}: line {line_no}: malformed JSON: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"{path}: line {line_no}: expected a JSON object, got {type(payload).__name__}"
                )

            # Board-level metadata header. Must be the first non-blank
            # line; anything later is a structural error.
            if payload.get(_BOARD_META_KEY) is True:
                if seen_any_row:
                    raise ValueError(
                        f"{path}: line {line_no}: 'board_meta' header must be the "
                        "first line of the board file"
                    )
                disable_drift = _coerce_disable_drift(
                    payload.get("disable_drift", []), f"{path}: line {line_no}"
                )
                if "judge_only" in payload:
                    judge_only = _coerce_judge_only(
                        payload["judge_only"], f"{path}: line {line_no}"
                    )
                seen_any_row = True
                continue

            seen_any_row = True
            # Back-compat alias: ``budget_s`` is the short field name
            # preferred by Python-builder boards (see
            # :class:`zicato.board.builder.Entry`). Promote it to the
            # canonical ``wall_clock_budget_seconds`` when only the
            # short form is present so older readers stay tolerant of
            # boards written by the builder API.
            if "budget_s" in payload and "wall_clock_budget_seconds" not in payload:
                payload["wall_clock_budget_seconds"] = payload.pop("budget_s")
            _reject_legacy_expectation(payload, f"{path}: line {line_no}")
            try:
                entry = validate_board_entry(payload)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path}: line {line_no}: invalid entry: {exc}") from exc
            if entry.id in seen_ids:
                raise ValueError(f"{path}: line {line_no}: duplicate entry id {entry.id!r}")
            seen_ids.add(entry.id)
            entries.append(entry)

    return entries, disable_drift, judge_only


def load_board(path: Path) -> list[BoardEntry]:
    """Parse a JSONL board file into validated :class:`BoardEntry` rows.

    Thin wrapper over :func:`load_board_with_meta` for the common case
    where the caller does not need the board-level ``disable_drift`` /
    ``judge_only`` metadata. See :func:`load_board_with_meta` for the
    full contract and the list of conditions that raise
    :class:`ValueError`.
    """
    entries, _disable_drift, _judge_only = load_board_with_meta(path)
    return entries


def _board_meta_to_dict(
    disable_drift: tuple[DriftKind, ...],
    judge_only: bool,
) -> dict[str, Any]:
    """Build the ``board_meta`` header object for a board's metadata.

    ``judge_only`` is emitted only when ``True`` so a board that sets
    only ``disable_drift`` (or that predates the judge-only flag) keeps
    a header that is byte-identical to the pre-judge_only format.
    """
    out: dict[str, Any] = {
        _BOARD_META_KEY: True,
        "disable_drift": [k.value for k in disable_drift],
    }
    if judge_only:
        out["judge_only"] = True
    return out


def _entry_to_dict(entry: BoardEntry) -> dict[str, Any]:
    """Serialize one entry, emitting only the keys relevant to its kind.

    The wall-clock budget is written as ``budget_s`` (the short form)
    rather than the dataclass-canonical ``wall_clock_budget_seconds``.
    The reader in :func:`load_board_with_meta` accepts both names — long
    form for legacy boards and operator-written JSONL, short form for
    boards produced by :class:`zicato.board.builder.Board.save` — so this
    asymmetry is invisible to round-trip callers.

    Enum-valued fields (expectation ``kind`` / ``reads``, judge ``mode`` /
    ``severity``) are written as their bare wire token: the enums all
    subclass ``str``, so ``json.dumps`` does the right thing without an
    explicit ``.value`` for each — but we resolve them explicitly here
    for clarity and to stay correct even if a caller stored a raw token.
    """
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "budget_s": entry.wall_clock_budget_seconds,
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
        exp = entry.expectation
        exp_dict: dict[str, Any] = {
            "kind": str(exp.kind.value if hasattr(exp.kind, "value") else exp.kind),
            "spec": exp.spec,
        }
        # Only emit reads when non-default so JSON round-trips cleanly.
        reads_value = exp.reads.value if hasattr(exp.reads, "value") else exp.reads
        if reads_value != "final_output":
            exp_dict["reads"] = str(reads_value)
        out["expectation"] = exp_dict
    if entry.judges:
        out["judges"] = [
            {
                "name": j.name,
                "mode": j.mode.value if hasattr(j.mode, "value") else j.mode,
                "body": j.body,
                "severity": j.severity.value if hasattr(j.severity, "value") else j.severity,
            }
            for j in entry.judges
        ]

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


def save_board(
    entries: list[BoardEntry],
    path: Path,
    *,
    disable_drift: tuple[DriftKind, ...] = (),
    judge_only: bool = False,
) -> None:
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
    disable_drift:
        Board-level drift kinds to suppress.
    judge_only:
        Board-level judge-only flag (goldfive judges without steering).

        The ``board_meta`` header line is written only when *something*
        is non-default — ``disable_drift`` is non-empty OR ``judge_only``
        is ``True``. A fully-default board (no suppressed drift, steering
        on) writes NO header line at all, byte-identical to a board saved
        before the ``judge_only`` flag existed.

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
        if disable_drift or judge_only:
            fh.write(json.dumps(_board_meta_to_dict(disable_drift, judge_only), ensure_ascii=False))
            fh.write("\n")
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
    the file is touched. Any board-level ``board_meta`` header already
    present in the file is left untouched.

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
            raise ValueError(f"{path}: entry id {entry.id!r} already exists in board")

    row = _entry_to_dict(entry)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False))
        fh.write("\n")


def remove_entry(path: Path, entry_id: str) -> None:
    """Remove an entry by id from a JSONL board file.

    The file is rewritten without the matching row. Raises if no row
    has the given id (rather than silently no-op'ing) so the CLI can
    surface a clear error. Any board-level ``disable_drift`` /
    ``judge_only`` header is preserved across the rewrite.

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
    entries, disable_drift, judge_only = load_board_with_meta(path)
    new_entries = [e for e in entries if e.id != entry_id]
    if len(new_entries) == len(entries):
        raise ValueError(f"{path}: no entry with id {entry_id!r} to remove")
    save_board(new_entries, path, disable_drift=disable_drift, judge_only=judge_only)


__all__ = [
    "load_board",
    "load_board_with_meta",
    "save_board",
    "append_entry",
    "remove_entry",
]


# ``asdict`` and ``Mapping`` referenced for forward-compat with future
# adapters that want to lean on the dataclass machinery directly.
_asdict_ref: Any = asdict
_mapping_ref: Any = Mapping
