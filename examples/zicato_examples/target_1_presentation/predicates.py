"""Predicate functions for the target_1_presentation board.

These are referenced from ``board.jsonl`` entries via the
``expectation`` block when the entry uses ``kind: "predicate"``. The
dotted-path spec a board entry stores is, e.g.::

    zicato_examples.target_1_presentation.predicates:has_slide_titles

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
    bullet_lines = sum(1 for line in out.splitlines() if line.lstrip().startswith(("-", "*")))
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


#: The concrete Q3 metrics the picky-stakeholder persona HOLDS and reveals
#: when the agent asks for the numbers (see ``board.jsonl`` →
#: ``picky_stakeholder_emulated`` → ``user_persona``). The board is a
#: judge-only, no-fabrication test: the persona is picky about FRAMING but
#: NOT about withholding data, so a correct agent asks, receives these
#: figures, and builds a concrete deck WITHOUT fabricating anything beyond
#: the given set. The acceptance predicate :func:`addressed_picky_feedback`
#: checks the final deck actually used these GIVEN numbers (drawn from this
#: set) and reflects a feedback-driven revision.
#:
#: Each entry is the canonical string form the persona reveals; the
#: predicate matches normalised variants (with/without ``$``, ``%``, comma
#: grouping, and ``k``/``m`` shorthands) so a deck that writes "$4.2M" or
#: "4,200,000" still counts as having used the figure. Keep this in sync
#: with the persona's ``constraints`` block in ``board.jsonl``.
Q3_METRICS: dict[str, str] = {
    "revenue": "$4.2M",
    "qoq_growth": "12%",
    "churn": "3.1%",
    "nrr": "118%",
    "new_logos": "47",
}


def _normalise_numbers(text: str) -> set[str]:
    """Extract a normalised set of numeric tokens from ``text``.

    Each number is reduced to its bare digit/decimal form (``"$4.2m"`` →
    ``"4.2"``, ``"4,200,000"`` → ``"4200000"``, ``"12%"`` → ``"12"``) so a
    deck that writes a GIVEN figure in any reasonable surface form still
    matches. ``k`` / ``m`` / ``b`` magnitude suffixes are preserved as a
    trailing letter token (``"4.2m"`` → ``"4.2m"``) so ``"$4.2M"`` and a
    literal ``"4200000"`` BOTH resolve to a comparable key via
    :func:`_number_keys`.
    """
    import re  # noqa: PLC0415

    keys: set[str] = set()
    for raw in re.findall(r"\$?\d[\d,]*\.?\d*\s*[kmb%]?", text, flags=re.IGNORECASE):
        token = (
            raw.replace("$", "").replace(",", "").replace(" ", "").replace("%", "").strip().lower()
        )
        if token:
            keys.add(token)
    return keys


def _number_keys(value: str) -> set[str]:
    """Return the set of normalised forms a GIVEN figure may appear as.

    e.g. ``"$4.2M"`` → ``{"4.2m", "4200000"}``; ``"12%"`` → ``{"12"}``;
    ``"47"`` → ``{"47"}``. Used to test membership against the normalised
    token set extracted from the deck.
    """
    bare = value.replace("$", "").replace(",", "").strip().lower()
    forms: set[str] = set()
    pct = bare.rstrip("%")
    forms.add(pct)
    mag = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if bare and bare[-1] in mag:
        num = bare[:-1]
        forms.add(bare)  # "4.2m"
        try:
            expanded = int(float(num) * mag[bare[-1]])
            forms.add(str(expanded))  # "4200000"
        except ValueError:  # pragma: no cover - defensive
            pass
    return {f for f in forms if f}


def addressed_picky_feedback(result: Any) -> bool:
    """Acceptance for the picky-stakeholder emulated entry.

    Reads ``conversation_end`` (the final deck) and tests the TWO things
    the board exists to verify, rather than the old weak "contains the
    word 'revised'" heuristic:

    (a) **Uses the GIVEN Q3 numbers.** The persona reveals a small,
        concrete metrics set (:data:`Q3_METRICS`) when the agent asks for
        it; a passing deck must actually USE at least three of those
        figures. This is what makes the no-fabrication path satisfiable —
        the agent asks, gets the numbers, and builds a concrete deck
        WITHOUT inventing values beyond the given set. Numbers are matched
        in any reasonable surface form (``$4.2M`` / ``4,200,000`` /
        ``4.2m`` all count) via :func:`_normalise_numbers` /
        :func:`_number_keys`.

    (b) **Reflects a feedback-driven revision.** The final output carries
        at least one revision signal (``"revised"``, ``"updated"``,
        ``"v2"``, ``"as requested"``, ``"per your feedback"``,
        ``"incorporated"``) — the picky persona pushes for changes, so a
        passing run terminates with a revised deliverable, not the first
        draft.

    Both clauses must hold. Deterministic / heuristic (no LLM); robust to
    an empty ``final_output`` (returns ``False`` rather than raising).
    """
    out = _final_output(result)
    if not out:
        return False

    # (a) uses at least three of the GIVEN Q3 figures.
    deck_numbers = _normalise_numbers(out)
    used = sum(1 for value in Q3_METRICS.values() if _number_keys(value) & deck_numbers)
    if used < 3:
        return False

    # (b) reflects a feedback-driven revision.
    revised = any(
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
    return revised


__all__ = [
    "has_slide_titles",
    "mentions_waffles",
    "mentions_transformers",
    "mentions_quarterly_metrics",
    "has_structured_outline",
    "avoids_offtopic_raccoons",
    "stayed_coherent_across_turns",
    "addressed_picky_feedback",
    "Q3_METRICS",
]
