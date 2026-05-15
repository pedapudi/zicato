"""Programmatic builder API for boards of :class:`~zicato.core.BoardEntry`.

The board JSONL format is hand-editable, but operators authoring boards
in Python deserve a less ceremonial entry point than constructing
:class:`~zicato.core.BoardEntry` directly with every discriminant field
spelled out. This module exposes two surfaces:

* :class:`Entry` — a factory-style class that auto-detects the entry
  kind from the supplied keyword arguments. Calling ``Entry(...)``
  returns a fully-validated :class:`~zicato.core.BoardEntry`; the class
  is never actually instantiated.
* :class:`Board` — a thin list-of-entries container with
  :meth:`Board.save` / :meth:`Board.load` JSONL methods so the in-Python
  builder can hand off to the same on-disk format used by the CLI.

The friendly ``budget_s`` name is the alias preferred by Python authors;
the on-the-dataclass field stays ``wall_clock_budget_seconds`` so the
core type doesn't have to change. The JSONL writer prefers the short
form on output and accepts both on input (see
:mod:`zicato.board.jsonl`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.types import (
    BoardEntry,
    BoardEntryKind,
    Expectation,
    ScriptedTurn,
    UserPersona,
)


# ---------------------------------------------------------------------------
# Entry: the friendly factory facade
# ---------------------------------------------------------------------------


def _coerce_turns(
    turns: list[str] | list[ScriptedTurn] | tuple[Any, ...] | None,
) -> tuple[ScriptedTurn, ...] | None:
    """Coerce a user-supplied ``turns`` argument into a tuple of :class:`ScriptedTurn`.

    Accepts a list/tuple of strings (treated as user messages) or a
    list/tuple of already-built :class:`ScriptedTurn` instances. Empty
    sequences are rejected — :class:`~zicato.core.BoardEntry.validate`
    would reject them too, but doing it here gives a clearer error
    message rooted at the builder call site.
    """
    if turns is None:
        return None
    if not isinstance(turns, (list, tuple)):
        raise ValueError(
            f"Entry: 'turns' must be a list or tuple, got {type(turns).__name__}"
        )
    if len(turns) == 0:
        raise ValueError("Entry: 'turns' must be non-empty when provided")
    out: list[ScriptedTurn] = []
    for i, t in enumerate(turns):
        if isinstance(t, ScriptedTurn):
            out.append(t)
        elif isinstance(t, str):
            out.append(ScriptedTurn(user=t))
        else:
            raise ValueError(
                f"Entry: 'turns[{i}]' must be str or ScriptedTurn, got "
                f"{type(t).__name__}"
            )
    return tuple(out)


def _infer_kind(
    *,
    input: str | None,
    turns: tuple[ScriptedTurn, ...] | None,
    persona: UserPersona | None,
    adversarial_agent_spec: str | None,
    explicit_kind: str | None,
) -> BoardEntryKind:
    """Infer the entry kind from the supplied discriminant arguments.

    Resolution order:

    1. If ``explicit_kind`` is set, use it. This is how operators reach
       ``"synthetic_clean"`` (the only kind that does not have a unique
       discriminant field — ``input`` alone could also mean
       ``"single_turn"``).
    2. Otherwise, exactly one of the four discriminants — ``turns``,
       ``persona``, ``adversarial_agent_spec``, ``input`` — must be
       present, and the inferred kind follows from which one.

    Raises :class:`ValueError` with a precise message when multiple
    conflicting discriminants are supplied or none are.
    """
    if explicit_kind is not None:
        if explicit_kind not in (
            "single_turn",
            "multi_turn_scripted",
            "multi_turn_emulated",
            "synthetic_adversarial",
            "synthetic_clean",
        ):
            raise ValueError(
                f"Entry: kind={explicit_kind!r} is not a recognized BoardEntryKind"
            )
        return explicit_kind  # type: ignore[return-value]

    discriminants_present: list[str] = []
    if turns is not None:
        discriminants_present.append("turns")
    if persona is not None:
        discriminants_present.append("persona")
    if adversarial_agent_spec is not None:
        discriminants_present.append("adversarial_agent_spec")

    if len(discriminants_present) > 1:
        raise ValueError(
            "Entry: cannot supply more than one of "
            f"{discriminants_present!r}; pick exactly one or set "
            "'kind' explicitly"
        )

    if turns is not None:
        if input is not None:
            raise ValueError(
                "Entry: 'input' and 'turns' are mutually exclusive; "
                "scripted multi-turn entries express the first user "
                "message as turns[0]"
            )
        return "multi_turn_scripted"

    if persona is not None:
        if input is not None:
            raise ValueError(
                "Entry: 'input' and 'persona' are mutually exclusive; "
                "emulated entries derive user turns from the persona"
            )
        return "multi_turn_emulated"

    if adversarial_agent_spec is not None:
        if input is None:
            raise ValueError(
                "Entry: synthetic_adversarial requires 'input' alongside "
                "'adversarial_agent_spec'"
            )
        return "synthetic_adversarial"

    if input is not None:
        return "single_turn"

    raise ValueError(
        "Entry: must supply one of 'input', 'turns', 'persona', or "
        "'adversarial_agent_spec' (or set 'kind' explicitly)"
    )


class Entry:
    """Friendly factory that returns a validated :class:`~zicato.core.BoardEntry`.

    ``Entry`` is not a dataclass — it is a class whose ``__new__``
    inspects the supplied keyword arguments, infers the
    :class:`~zicato.core.BoardEntryKind`, and constructs a
    :class:`~zicato.core.BoardEntry` directly. The class is never
    actually instantiated; ``isinstance(x, Entry)`` is meaningless. This
    keeps the call site as compact as possible::

        Entry(id="e1", input="What is 2+2?",
              evaluate=Predicate.contains("4"), budget_s=60)

    Auto-detection rules
    --------------------

    * ``input`` only → ``single_turn``
    * ``turns=[...]`` → ``multi_turn_scripted`` (auto-fills ``max_turns``
      to ``len(turns)`` when not given)
    * ``persona=UserPersona(...)`` → ``multi_turn_emulated``
    * ``input`` + ``adversarial_agent_spec`` → ``synthetic_adversarial``
    * ``input`` + ``kind="synthetic_clean"`` → ``synthetic_clean``

    Any combination that does not match exactly one of these rules
    raises :class:`ValueError` with a message pointing at the conflict.
    """

    def __new__(  # type: ignore[misc] — intentional factory facade
        cls,
        *,
        id: str,
        input: str | None = None,
        turns: list[str] | list[ScriptedTurn] | tuple[Any, ...] | None = None,
        persona: UserPersona | None = None,
        evaluate: Expectation | None = None,
        budget_s: int = 300,
        weight: float = 1.0,
        tags: tuple[str, ...] | list[str] = (),
        context: dict[str, str] | None = None,
        kind: str | None = None,
        adversarial_agent_spec: str | None = None,
        required_drift_kinds: tuple[str, ...] | list[str] | None = None,
        max_turns: int | None = None,
    ) -> BoardEntry:  # type: ignore[override]
        """Build and return a validated :class:`~zicato.core.BoardEntry`."""
        coerced_turns = _coerce_turns(turns)
        inferred_kind = _infer_kind(
            input=input,
            turns=coerced_turns,
            persona=persona,
            adversarial_agent_spec=adversarial_agent_spec,
            explicit_kind=kind,
        )

        # Default max_turns when the kind needs one. We pick something
        # sane rather than forcing the caller to spell it for the common
        # case where the scripted entry has N turns and 5 retries-worth
        # of agent slack feels excessive.
        resolved_max_turns: int | None = max_turns
        if inferred_kind == "multi_turn_scripted":
            if resolved_max_turns is None and coerced_turns is not None:
                resolved_max_turns = len(coerced_turns)
        elif inferred_kind == "multi_turn_emulated":
            if resolved_max_turns is None:
                resolved_max_turns = 5

        coerced_required_drift: tuple[str, ...] | None
        if required_drift_kinds is None:
            coerced_required_drift = None
        else:
            coerced_required_drift = tuple(required_drift_kinds)

        entry = BoardEntry(
            id=id,
            kind=inferred_kind,
            wall_clock_budget_seconds=int(budget_s),
            weight=float(weight),
            tags=tuple(tags),
            context=dict(context) if context else {},
            expectation=evaluate,
            input=input,
            turns=coerced_turns,
            user_persona=persona,
            max_turns=resolved_max_turns,
            adversarial_agent_spec=adversarial_agent_spec,
            required_drift_kinds=coerced_required_drift,
        )
        entry.validate()
        return entry


# ---------------------------------------------------------------------------
# Board: thin container with save/load
# ---------------------------------------------------------------------------


@dataclass
class Board:
    """A mutable container for a list of :class:`~zicato.core.BoardEntry` rows.

    The dataclass is intentionally bare — ``Board`` is convenience over
    the existing JSONL machinery, not a new abstraction. Operators
    build entries with :class:`Entry`, append them with :meth:`add`,
    and persist with :meth:`save`; :classmethod:`load` reads a JSONL
    file back into a fresh container.
    """

    entries: list[BoardEntry] = field(default_factory=list)

    def add(self, entry: BoardEntry) -> "Board":
        """Append ``entry`` and return ``self`` for chaining.

        Accepts a raw :class:`~zicato.core.BoardEntry` (which is also
        what :class:`Entry` returns). The id is checked for uniqueness
        against the existing entries; appending a duplicate id raises
        :class:`ValueError` so the operator hears about the collision
        at construction time rather than at :meth:`save` time.
        """
        if not isinstance(entry, BoardEntry):
            raise TypeError(
                f"Board.add expects a BoardEntry, got {type(entry).__name__}"
            )
        if any(e.id == entry.id for e in self.entries):
            raise ValueError(
                f"Board.add: entry id {entry.id!r} already present in board"
            )
        self.entries.append(entry)
        return self

    def save(self, path: Path) -> None:
        """Serialize the board to ``path`` as JSONL.

        Delegates to :func:`zicato.board.jsonl.save_board` so the on-disk
        format stays in sync with the rest of the system. The parent
        directory must already exist; the writer is atomic (sibling
        ``.tmp`` + rename).
        """
        from zicato.board.jsonl import save_board  # noqa: PLC0415

        save_board(self.entries, Path(path))

    @classmethod
    def load(cls, path: Path) -> "Board":
        """Build a :class:`Board` from the JSONL file at ``path``.

        Delegates to :func:`zicato.board.jsonl.load_board` for parsing,
        validation, and duplicate-id detection.
        """
        from zicato.board.jsonl import load_board  # noqa: PLC0415

        entries = load_board(Path(path))
        return cls(entries=list(entries))


__all__ = ["Board", "Entry"]
