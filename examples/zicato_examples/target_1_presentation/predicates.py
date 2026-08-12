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

What each predicate grades
--------------------------

This target's deliverable is a rendered webpage the agent writes to disk
through ``write_webpage``; its closing chat message is a report *about*
that page. The two are graded separately and deliberately:

* **Deliverable predicates** (:func:`wrote_presentation_file`,
  :func:`mentions_waffles`, :func:`mentions_transformers`,
  :func:`mentions_quarterly_metrics`, :func:`has_slide_titles`,
  :func:`has_structured_outline`, :func:`avoids_offtopic_raccoons`) read
  the deck on disk. A run that wrote no deck fails them, whatever its
  reply claimed.
* **Conversation predicates** (:func:`stayed_coherent_across_turns`,
  :func:`addressed_picky_feedback`) read the transcript, because
  cross-turn memory and feedback handling are properties of the
  conversation and of nothing else.

Grading the reply for a deliverable property is a blind spot in both
directions: a run that narrates slide titles without ever calling
``write_webpage`` passes, and a run that writes a good deck and confirms
it in one terse line fails. The ``final_output`` a live run scores is a
short planner summary the agent does not author, so it carries almost
none of the deck's content.

The ``regex``, ``expected_text`` and ``json_schema`` expectation kinds
match against ``final_output`` by construction (see
:mod:`zicato.board.matchers`); an entry that grades the artifact must
therefore use the ``predicate`` kind.
"""

from __future__ import annotations

# zicato:grading — operator-owned pass/fail contract; never a proposer mutation point.
import os
import re
from pathlib import Path
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


# ---------------------------------------------------------------------------
# The graded artifact — the deck the run wrote to disk.
#
# Resolution deliberately does NOT go through the agent module.
# ``_slugify_topic`` / ``_topic_output_dir`` there are proposer mutation
# points, so grading through them would let a patch redirect the grader to
# whatever path it chose. This module resolves the run's output ROOT
# independently and then searches beneath it for the deck.
#
# The root is the per-run scratch directory the tournament worker exports
# (the contract pinned in ``zicato/epoch/snapshot_scope.py`` as
# SCRATCH_DIR_ENV), duplicated here as a bare string so the operator's
# grading code keeps no import dependency on zicato internals. The worker
# hands every run a fresh scratch directory and evaluates the expectation
# before discarding it, so one run can never be graded on another's deck.
# Standing in for it outside a tournament is the ``output/`` directory next
# to the agent module, which is where a bare standalone run writes.
#
# The search is slug-agnostic. WHICH directory under the root the agent
# filed the deck in is the write/read slug question this board exists to
# pose, and it is graded as process drift by ``judges.FileFindabilityJudge``
# — grading it a second time here would double-count it and would make a
# perfectly good deck invisible for a naming reason.
# ---------------------------------------------------------------------------

#: Per-run scratch directory the tournament worker exports; mirrors
#: :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV`.
_SCRATCH_DIR_ENV = "ZICATO_RUN_SCRATCH_DIR"

#: Directory the agent's tools write run output under, relative to the
#: resolved base. Fixed by the agent's ``_output_base`` and not part of its
#: mutable surface.
_OUTPUT_DIRNAME = "output"

#: The deck's entry file. Its presence is what makes a directory a deck.
_DECK_ENTRY_FILE = "index.html"

#: The three files ``write_webpage`` emits, in read order.
_DECK_FILES = ("index.html", "styles.css", "script.js")

#: An element carrying ``slide`` in its class or id — how the developer is
#: instructed to structure a slideshow. Counted to size the deck.
_SLIDE_ELEMENT = re.compile(r"""(?:class|id)\s*=\s*["'][^"']*\bslide""", re.IGNORECASE)

#: A list item — the other shape an outlined deck takes.
_LIST_ITEM = re.compile(r"<li\b", re.IGNORECASE)


def _output_root() -> Path:
    """Return the directory this run's presentation output is written under."""
    scratch = os.environ.get(_SCRATCH_DIR_ENV)
    if scratch:
        return Path(scratch) / _OUTPUT_DIRNAME
    return Path(__file__).parent / "agent" / _OUTPUT_DIRNAME


def deck_files() -> dict[str, str]:
    """Return the deck this run wrote as ``{filename: contents}``.

    Empty when the run wrote no deck at all — no output root, or no
    ``index.html`` anywhere beneath it. A deck missing its stylesheet or
    script yields those entries as empty strings rather than omitting
    them, so callers can tell "absent file" from "absent deck".

    When a run wrote under more than one slug the NEWEST ``index.html``
    wins (ties broken by path, so the choice is deterministic). Grading the
    union would credit a run for a deck it had already abandoned; the
    deliverable is the one standing at the end of the run.

    Never raises: an unreadable tree yields ``{}``.
    """
    root = _output_root()
    try:
        entries = sorted(root.rglob(_DECK_ENTRY_FILE))
        newest = max(entries, key=lambda p: (p.stat().st_mtime, str(p)), default=None)
    except OSError:
        return {}
    if newest is None:
        return {}
    files: dict[str, str] = {}
    for name in _DECK_FILES:
        try:
            files[name] = (newest.parent / name).read_text(errors="replace")
        except OSError:
            files[name] = ""
    return files


def _usable_deck() -> dict[str, str]:
    """Return the deck's files, or ``{}`` unless they form a usable deck.

    A usable deck is real markup (an ``<html`` tag, not a stub) that
    references BOTH ``styles.css`` and ``script.js`` — the links
    ``web_developer_instruction`` requires "so the files are connected
    properly". A page that loads none of its own styling or navigation is
    not the deliverable, however good its markup.

    Every predicate below opens on this, so a run that wrote nothing
    usable fails them all rather than being graded on its reply.
    """
    files = deck_files()
    html = files.get(_DECK_ENTRY_FILE, "").lower()
    if "<html" not in html or "styles.css" not in html or "script.js" not in html:
        return {}
    return files


def _deck_text(files: dict[str, str]) -> str:
    """Return the deck's files as one lowercased searchable string."""
    return "\n".join(files.values()).lower()


def _slide_count(files: dict[str, str]) -> int:
    """Count the deck's slide elements — those naming ``slide`` in class or id."""
    return len(_SLIDE_ELEMENT.findall(files.get(_DECK_ENTRY_FILE, "")))


def wrote_presentation_file(result: Any) -> bool:
    """The run left a usable presentation file on disk.

    The base detectability check for this target. Ignores ``result``: the
    deliverable is the file, and the reply cannot substitute for it.
    """
    del result
    return bool(_usable_deck())


def has_slide_titles(result: Any) -> bool:
    """The deck carries at least three slides.

    Three is the floor below which the artifact is too thin to qualify as
    a deck.
    """
    del result
    return _slide_count(_usable_deck()) >= 3


def mentions_waffles(result: Any) -> bool:
    """The deck is about waffles.

    Paired with the canonical "make a presentation about waffles"
    single-turn entry — the cheapest end-to-end check on the board: if it
    fails, the tree is broken.
    """
    del result
    return "waffle" in _deck_text(_usable_deck())


def mentions_transformers(result: Any) -> bool:
    """The deck discusses transformers in an ML sense.

    Accepts either the bare word ``"transformer"`` or the standard
    architectural keywords ``"attention"`` / ``"encoder"`` /
    ``"decoder"`` — the entry asks for a non-ML-audience deck, so a
    correct deck may use the lay term but explain the mechanism.
    """
    del result
    text = _deck_text(_usable_deck())
    return any(k in text for k in ("transformer", "attention", "encoder", "decoder"))


def mentions_quarterly_metrics(result: Any) -> bool:
    """The deck references quarterly metrics or Q3 specifically.

    Topical check for the metrics-deck entry; accepts ``"q3"``,
    ``"quarter"``, ``"quarterly"``, or the literal word ``"metrics"``.
    """
    del result
    text = _deck_text(_usable_deck())
    return any(k in text for k in ("q3", "quarter", "metrics"))


def has_structured_outline(result: Any) -> bool:
    """The deck is structured rather than a wall of prose.

    Satisfied by at least three slide elements OR at least three list
    items — the two shapes an outlined deck takes. Used by the
    metrics-deck entry, where structure is what the entry asks for.
    """
    del result
    files = _usable_deck()
    if _slide_count(files) >= 3:
        return True
    return len(_LIST_ITEM.findall(files.get(_DECK_ENTRY_FILE, ""))) >= 3


def avoids_offtopic_raccoons(result: Any) -> bool:
    """The deck does NOT mention raccoons.

    The upstream presentation tree carries a deliberate drift-injection
    hook that asks the researcher to include raccoon facts regardless of
    the user's topic. A correctly steered run keeps them out of the
    delivered deck. A run that delivered no deck fails rather than passing
    vacuously — there is no suppression to credit when there is nothing on
    disk.
    """
    del result
    files = _usable_deck()
    return bool(files) and "raccoon" not in _deck_text(files)


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

    (a) **Uses the GIVEN Q3 numbers.** The persona provides a small,
        concrete metrics set (:data:`Q3_METRICS`) UP FRONT in its opening
        message; a passing reply must actually USE at least TWO of those
        figures. This is what makes the no-fabrication path satisfiable —
        the agent is handed the numbers and reports back a concrete
        deck WITHOUT inventing values beyond the given set. (The persona
        used to withhold the figures until asked, but this one-shot
        topic→deck agent has no clarifying-question step, so it could
        never elicit them and always fabricated → a constant critical
        failure with no score gradient; seeding the data up front makes
        the entry reachable and turns it into a framing/feedback test the
        agent can actually climb.) The bar is two (not three) figures
        because the graded artifact is the agent's final conversational
        reply (``conversation_end``), which typically *summarises* the
        deck rather than reprinting every slide — demanding three lands
        the entry in the all-fail band (observed: 0/19 passes) without
        measuring anything more. Two given figures present + a revision
        signal is enough to separate an agent that used the data and
        revised from one that fabricated or stalled.
        Numbers are matched in any reasonable surface form (``$4.2M`` /
        ``4,200,000`` / ``4.2m`` all count) via
        :func:`_normalise_numbers` / :func:`_number_keys`.

    (b) **Reflects a feedback-driven revision.** The final output carries
        at least one revision signal (``"revised"``, ``"updated"``,
        ``"v2"``, ``"as requested"``, ``"per your feedback"``,
        ``"incorporated"``, ``"adjusted"``, ``"reworked"``) — the picky
        persona pushes for changes, so a passing run terminates with a
        revised deliverable, not the first draft.

    Both clauses must hold. Deterministic / heuristic (no LLM); robust to
    an empty ``final_output`` (returns ``False`` rather than raising).
    """
    out = _final_output(result)
    if not out:
        return False

    # (a) uses at least two of the GIVEN Q3 figures.
    deck_numbers = _normalise_numbers(out)
    used = sum(1 for value in Q3_METRICS.values() if _number_keys(value) & deck_numbers)
    if used < 2:
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
            "adjusted",
            "reworked",
        )
    )
    return revised


def _precision_recall_f1(
    retrieved: set[str],
    relevant: set[str],
) -> tuple[float, float, float]:
    """Return ``(precision, recall, f1)`` for a retrieved-vs-relevant set pair.

    A small, dependency-free set-membership F1 — the canonical CONTINUOUS
    per-entry quality a retrieval / search board scores on. Over-retrieval
    (returning items that are not relevant) drives PRECISION down;
    under-retrieval (missing relevant items) drives RECALL down; F1 is their
    harmonic mean, so it penalises BOTH failure modes, unlike pure recall.

    Edge cases are defined so the score is always a finite ``[0, 1]`` value:

    * empty ``relevant`` AND empty ``retrieved`` — the agent correctly
      returned nothing for an entry with nothing to find: ``(1, 1, 1)``;
    * empty ``relevant`` but non-empty ``retrieved`` — pure over-retrieval
      against a no-op entry: precision ``0`` (nothing it returned was
      relevant), recall ``1`` (there was nothing to miss), F1 ``0``;
    * empty ``retrieved`` but non-empty ``relevant`` — the agent returned
      nothing when there was something: precision ``1`` (vacuously — it made
      no false claims), recall ``0``, F1 ``0``.
    """
    true_positives = len(retrieved & relevant)
    # Empty retrieved -> precision is vacuously 1.0 (no false claims made);
    # empty relevant -> recall is vacuously 1.0 (nothing to miss).
    precision = true_positives / len(retrieved) if retrieved else 1.0
    recall = true_positives / len(relevant) if relevant else 1.0
    if precision + recall == 0.0:
        f1 = 0.0
    else:
        f1 = 2.0 * precision * recall / (precision + recall)
    return precision, recall, f1


def search_f1_score(
    retrieved: set[str],
    relevant: set[str],
) -> tuple[float, float | dict[str, float]]:
    """SCORER form of :func:`_precision_recall_f1` for the PREDICATE seam.

    Returns the ``(score, metrics)`` 2-tuple the dotted-path scorer seam
    accepts (see :func:`zicato.board.matchers._predicate_outcome_to_result`):
    the F1 is the continuous ``score`` the scalar and gate run on, and
    ``precision`` / ``recall`` ride out in the ``metrics`` mapping so a later
    aggregation step can read the recall-vs-precision decomposition as
    numbers — the prerequisite for the outcome-marginal proposer feedback.

    Note: a board entry's dotted-path scorer takes a single
    :class:`~zicato.core.RunResult`; an operator wires this helper into such
    a callable by parsing the retrieved / relevant sets out of the run (e.g.
    the agent's returned ids vs the entry's ground-truth ids). Exposed
    standalone — and unit-tested directly — so the F1 logic is reusable
    without inventing a whole search board here.
    """
    precision, recall, f1 = _precision_recall_f1(retrieved, relevant)
    return f1, {"precision": precision, "recall": recall}


def _per_entry_metric(loss: Any, key: str) -> float | None:
    """Read a finite float ``loss.metrics[key]`` off a per-entry result.

    The reducer carries a scorer's optional decomposition out to
    ``loss.json`` as :attr:`~zicato.core.types.LossProfile.metrics`. This
    helper reads one key defensively — a missing mapping, a missing key, or
    a non-numeric value all yield ``None`` so the summarizer never raises.
    """
    metrics = getattr(loss, "metrics", None)
    if not isinstance(metrics, dict):
        return None
    raw = metrics.get(key)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN guard
        return None
    return val


def search_outcome_summary(losses: list[Any]) -> dict[str, float]:
    """OPERATOR outcome-summarizer hook (issue #18 cap 2, item 10).

    The GT-aware summarizer for a search / retrieval board. It receives the
    TRAIN-SLICE per-entry results (each a
    :class:`~zicato.core.types.LossProfile`, with Capability 1's
    ``precision`` / ``recall`` in :attr:`~zicato.core.types.LossProfile.metrics`
    when :func:`search_f1_score` scored the entry) and returns a STRUCTURED
    aggregate — a ``{marginal_name: numeric_rate}`` mapping, NOT prose. zicato
    sanitizes + bands every value before it reaches the proposer (see
    :func:`zicato.analyzer.outcome_marginals.run_operator_summarizer`), so
    this plug-in only has to compute the recall/precision-decomposition
    marginals; it cannot leak an entry id or a free-text note even if it
    tried, because non-numeric / identity-bearing returns are stripped.

    The marginals it contributes:

    * ``over_retrieval`` — fraction of runs whose precision fell below 0.5
      (the documented precision-collapse failure: the agent returned items
      that were not relevant);
    * ``misses`` — fraction of runs whose recall fell below 0.5 (the agent
      missed relevant items);
    * ``mean_recall`` / ``mean_precision`` — the board-wide means, so the
      proposer can read the decomposition's magnitude (banded by zicato into
      low / medium / high, never the exact mean).

    Every name is a short, lowercase, identifier-like label and every value
    is a finite float in ``[0, 1]`` — exactly the structured-aggregate shape
    the hook contract requires. An empty slice (or a slice with no
    precision/recall metrics) returns an empty mapping, contributing nothing.
    """
    precisions = [
        p for p in (_per_entry_metric(loss, "precision") for loss in losses) if p is not None
    ]
    recalls = [r for r in (_per_entry_metric(loss, "recall") for loss in losses) if r is not None]

    out: dict[str, float] = {}
    if precisions:
        out["over_retrieval"] = sum(1 for p in precisions if p < 0.5) / len(precisions)
        out["mean_precision"] = sum(precisions) / len(precisions)
    if recalls:
        out["misses"] = sum(1 for r in recalls if r < 0.5) / len(recalls)
        out["mean_recall"] = sum(recalls) / len(recalls)
    return out


__all__ = [
    "deck_files",
    "wrote_presentation_file",
    "has_slide_titles",
    "mentions_waffles",
    "mentions_transformers",
    "mentions_quarterly_metrics",
    "has_structured_outline",
    "avoids_offtopic_raccoons",
    "stayed_coherent_across_turns",
    "addressed_picky_feedback",
    "search_f1_score",
    "search_outcome_summary",
    "Q3_METRICS",
]
