"""Pin: the dashboard's entry-kind vocabularies match the Python Literal.

``BoardEntryKind`` (``core/board.py``) is the closed five-member vocabulary
the builder writes, ``board/jsonl.py`` serializes, and ``board/builder.py``
infers. The board views each carry their OWN ``KIND_LABEL`` map, and both
had stopped at the first three: the two synthetic kinds rendered unlabelled,
and the ``boards`` overview's kind counters — which tested
``=== 'single_turn'`` and ``startsWith('multi')`` — reported an all-synthetic
board as "0 single-turn · 0 multi-turn" over N entries.

A hand-kept vocabulary in three places drifts the moment the Literal gains a
member, and drifts silently, because an unknown kind renders as a blank
label rather than an error. This derives the expected set from the Literal
and pins each JS map against it.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import zicato.dashboard as _dashboard_pkg
from zicato.core.board import BoardEntryKind

_VIEWS = Path(_dashboard_pkg.__file__).resolve().parent / "static" / "js" / "views"


def _kinds() -> frozenset[str]:
    return frozenset(typing.get_args(BoardEntryKind))


def _js_map_keys(path: Path, name: str) -> frozenset[str]:
    """The keys of a top-level ``const <name> = { … };`` object literal.

    Deliberately FAILS CLOSED, like the knob registry's scans: the
    extraction assumes a top-level const bound to a brace literal (true of
    both maps today). A refactor to a computed map reds this pin rather
    than passing silently — widen the extraction, don't drop the guard.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"^const {re.escape(name)} = \{{(.*?)^\}};", source, re.S | re.M)
    assert match, f"could not locate a top-level `const {name} = {{…}};` in {path.name}"
    return frozenset(re.findall(r"(\w+)\s*:", match.group(1)))


def test_the_kind_literal_still_has_five_members() -> None:
    """Sanity: the vocabulary resolves, so the pins below are not vacuous."""
    kinds = _kinds()
    assert len(kinds) == 5, f"BoardEntryKind changed size: {sorted(kinds)}"
    assert {"synthetic_adversarial", "synthetic_clean"} <= kinds


def test_board_views_label_every_entry_kind() -> None:
    """Every kind has a label in BOTH board views; an unknown kind is blank."""
    kinds = _kinds()
    for filename, mapname in (
        ("board.js", "KIND_LABEL"),
        ("boards.js", "KIND_LABEL"),
        ("boards.js", "KIND_ORDER"),
    ):
        keys = _js_map_keys(_VIEWS / filename, mapname)
        missing = kinds - keys
        unknown = keys - kinds
        assert not missing, (
            f"{filename}::{mapname} is missing entry kind(s) {sorted(missing)} — "
            "an entry of that kind renders unlabelled. Add them, or narrow "
            "BoardEntryKind."
        )
        assert not unknown, (
            f"{filename}::{mapname} names {sorted(unknown)}, which is not in "
            "BoardEntryKind — a stale key from a renamed or removed kind."
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
