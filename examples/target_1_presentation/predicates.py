"""Predicate functions for the target_1_presentation board.

These are referenced from ``board.jsonl`` entries via the
``expectation`` block when the entry uses ``kind: "predicate"``. The
dotted-path spec a board entry stores is, e.g.::

    examples.target_1_presentation.predicates:has_slide_titles

Every predicate accepts a single :class:`zicato.core.types.RunResult`
positional argument and returns ``bool``. They are intentionally
defensive — production runs may abort partway and hand the predicate
an empty ``final_output``; predicates must never raise.

Operators add new predicates here and reference them from board
entries. The predicates module is itself NOT a zicato mutation point —
the proposer does not get to rewrite the operator's pass/fail
contract.
"""

from __future__ import annotations

from typing import Any


def _final_output(result: Any) -> str:
    """Return ``result.final_output`` as a lowercase string, or empty.

    Tolerates a missing attribute (returns ``""``) so predicates are
    robust to whatever shape the runner hands them. The real
    :class:`zicato.core.types.RunResult` carries a ``final_output: str``
    field; this helper is mostly belt-and-braces for the test path.
    """
    out = getattr(result, "final_output", "") or ""
    return out.lower()


def _transcript(result: Any) -> tuple[str, ...]:
    """Return ``result.transcript`` defensively as a tuple of strings."""
    t = getattr(result, "transcript", ()) or ()
    return tuple(str(s) for s in t)


def has_slide_titles(result: Any) -> bool:
    """At least three slide markers appear in the final output.

    The presentation tree's success shape is "the response describes
    multiple slides", which we approximate by counting the substring
    ``"slide "`` (case-insensitive) — covers both ``"Slide 1:"`` and
    ``"on this slide"`` phrasings. Three is the floor below which the
    output is too thin to qualify as a multi-slide deck.
    """
    return _final_output(result).count("slide ") >= 3


def mentions_waffles(result: Any) -> bool:
    """The final output mentions waffles.

    Paired with the canonical "make a presentation about waffles"
    single-turn entry. If the output drifts off-topic the predicate
    fails; this is the cheapest topical-fidelity check on the board.
    """
    return "waffle" in _final_output(result)


def mentions_transformers(result: Any) -> bool:
    """The final output discusses transformers in an ML sense.

    We accept either the bare word ``"transformer"`` or the standard
    architectural keywords ``"attention"`` / ``"self-attention"`` /
    ``"encoder"`` — the entry asks for a non-ML-audience deck, so the
    correct deck will use the lay term but explain the mechanism.
    """
    out = _final_output(result)
    if "transformer" in out:
        return True
    return any(k in out for k in ("attention", "encoder", "decoder"))


def mentions_quarterly_metrics(result: Any) -> bool:
    """The final output references quarterly metrics or Q3 specifically.

    Cheap topical check for the metrics deck entry; accepts ``"q3"``,
    ``"quarter"``, ``"quarterly"``, or the literal phrase ``"metrics"``.
    """
    out = _final_output(result)
    return any(k in out for k in ("q3", "quarter", "metrics"))


def has_structured_outline(result: Any) -> bool:
    """The output looks like a structured outline.

    Heuristic: at least three numbered list markers (``"1."``, ``"2."``,
    ``"3."``) OR at least three bullet markers (``"- "`` or ``"* "``)
    at line starts. Used by the metrics-deck entry where "outline"
    structure is the operator's preferred shape.
    """
    out = _final_output(result)
    numbered = sum(out.count(f"{i}.") for i in range(1, 4))
    if numbered >= 3:
        return True
    bullet_lines = sum(
        1 for line in out.splitlines() if line.lstrip().startswith(("-", "*"))
    )
    return bullet_lines >= 3


def avoids_offtopic_raccoons(result: Any) -> bool:
    """The output does NOT mention raccoons.

    The upstream presentation tree carries a deliberate drift-injection
    hook that asks the researcher to include raccoon facts regardless
    of the user's topic. A correctly steered run keeps raccoons out of
    the final deck. This predicate is the negative form: True iff the
    drift was suppressed.
    """
    return "raccoon" not in _final_output(result)


def stayed_coherent_across_turns(result: Any) -> bool:
    """For multi-turn entries, every assistant turn mentions the topic.

    Walks the ``transcript`` tuple and asserts each entry contains at
    least one of a small list of topical keywords. Cheap memory-
    failure check — if the agent forgot what it was supposed to be
    talking about by turn 3, this predicate fails.

    Currently keyed to the "transformers for a non-ML audience" multi-
    turn entry; new multi-turn entries that want a similar guard
    should add their own predicate rather than overloading this one.
    """
    transcript = _transcript(result)
    if not transcript:
        return False
    needles = ("transformer", "attention", "model", "neural", "ml")
    return all(any(n in turn.lower() for n in needles) for turn in transcript)


def addressed_picky_feedback(result: Any) -> bool:
    """For the picky-stakeholder emulated entry: final output revised.

    Heuristic: the final output contains at least one phrase that signals
    revision (``"revised"``, ``"updated"``, ``"v2"``, ``"as requested"``,
    ``"per your feedback"``). The picky-stakeholder persona keeps
    pushing for changes, so a passing run terminates with a revised
    deliverable rather than the original draft.
    """
    out = _final_output(result)
    return any(
        k in out
        for k in (
            "revised",
            "updated",
            "v2",
            "as requested",
            "per your feedback",
            "incorporated",
        )
    )


__all__ = [
    "has_slide_titles",
    "mentions_waffles",
    "mentions_transformers",
    "mentions_quarterly_metrics",
    "has_structured_outline",
    "avoids_offtopic_raccoons",
    "stayed_coherent_across_turns",
    "addressed_picky_feedback",
]
