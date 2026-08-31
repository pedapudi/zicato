"""Pin: the dashboard's entry-kind vocabulary matches the Python Literal.

``BoardEntryKind`` (``core/board.py``) is the closed five-member vocabulary
the builder writes, ``board/jsonl.py`` serializes, and ``board/builder.py``
infers. The console spells it twice more: ``ui.js``'s ``ENTRY_KIND_LABEL``,
the labels every board surface prints, and ``boards.js``'s ``KIND_ORDER``,
the trellis sort key over the same members.

A vocabulary kept by hand on the far side of a language boundary drifts the
moment the Literal gains a member, and drifts silently, because an unknown
kind renders as a blank label rather than an error. This derives the
expected set from the Literal and pins each JS map against it.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import zicato.dashboard as _dashboard_pkg
from zicato.core.board import BoardEntryKind

_JS = Path(_dashboard_pkg.__file__).resolve().parent / "static" / "js"
_VIEWS = _JS / "views"


def _kinds() -> frozenset[str]:
    return frozenset(typing.get_args(BoardEntryKind))


def _js_map_keys(path: Path, name: str) -> frozenset[str]:
    """The keys of a top-level ``const <name> = { … };`` object literal.

    Deliberately FAILS CLOSED, like the knob registry's scans: the
    extraction assumes a top-level const bound to a brace literal, which
    both maps are. A refactor to a computed map reds this pin rather than
    passing silently — widen the extraction, don't drop the guard.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"^(?:export )?const {re.escape(name)} = \{{(.*?)^\}};", source, re.S | re.M)
    assert match, f"could not locate a top-level `const {name} = {{…}};` in {path.name}"
    return frozenset(re.findall(r"(\w+)\s*:", match.group(1)))


def test_the_kind_literal_still_has_five_members() -> None:
    """Sanity: the vocabulary resolves, so the pins below are not vacuous."""
    kinds = _kinds()
    assert len(kinds) == 5, f"BoardEntryKind changed size: {sorted(kinds)}"
    assert {"synthetic_adversarial", "synthetic_clean"} <= kinds


def test_the_console_names_every_entry_kind() -> None:
    """Both JS maps carry every kind and no kind the Literal has dropped."""
    kinds = _kinds()
    for path, mapname in (
        (_JS / "ui.js", "ENTRY_KIND_LABEL"),
        (_VIEWS / "boards.js", "KIND_ORDER"),
    ):
        keys = _js_map_keys(path, mapname)
        missing = kinds - keys
        unknown = keys - kinds
        assert not missing, (
            f"{path.name}::{mapname} is missing entry kind(s) {sorted(missing)} — "
            "an entry of that kind renders unlabelled. Add them, or narrow "
            "BoardEntryKind."
        )
        assert not unknown, (
            f"{path.name}::{mapname} names {sorted(unknown)}, which is not in "
            "BoardEntryKind — a stale key from a renamed or removed kind."
        )


def test_the_board_surfaces_read_the_shared_label_map() -> None:
    """No board surface may reintroduce a label map of its own.

    The three surfaces that print an entry kind each held their own copy
    once, and only two of the three were pinned above, so the third could
    drift unobserved. They read one export now, and this keeps it that way:
    a local map would be a second vocabulary this file does not check.
    """
    for filename in ("board.js", "boards.js", "boardstatus.js"):
        source = (_VIEWS / filename).read_text(encoding="utf-8")
        assert "ENTRY_KIND_LABEL" in source, (
            f"{filename} no longer reads ui.js::ENTRY_KIND_LABEL — if it stopped "
            "printing entry kinds, drop it from this list."
        )
        assert not re.search(r"^(?:export )?const \w*KIND_LABEL = \{", source, re.M), (
            f"{filename} declares its own kind-label map again; the labels live "
            "in ui.js::ENTRY_KIND_LABEL so one entry cannot read two ways."
        )


def test_the_boards_overview_counts_every_kind() -> None:
    """The overview's kind counters partition the whole vocabulary.

    The labels can be complete while the COUNTERS still silently ignore a
    kind — which is the shape the synthetic entries actually hit. Each kind
    must be reachable by one of the counter predicates.
    """
    source = (_VIEWS / "boards.js").read_text(encoding="utf-8")
    predicates = {
        "single_turn": "b.kind === 'single_turn'",
        "multi": "b.kind.startsWith('multi')",
        "synthetic": "b.kind.startsWith('synthetic')",
    }
    for label, predicate in predicates.items():
        assert predicate in source, (
            f"the {label} counter predicate {predicate!r} is gone from "
            "boards.js — the kind counters no longer partition the vocabulary."
        )
    # …and every kind is matched by exactly ONE of them, so the counters
    # neither drop a kind nor double-count one.
    for kind in _kinds():
        matched = [
            label
            for label in ("single_turn", "multi", "synthetic")
            if kind == label or kind.startswith(label)
        ]
        assert len(matched) == 1, (
            f"entry kind {kind!r} is matched by {matched} counter predicate(s), "
            "expected exactly one — the overview would drop or double-count it."
        )
