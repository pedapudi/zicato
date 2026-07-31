"""Diff-complexity (MDL / parsimony) proxies over a proposed experiment.

OVERFITTING.md §5 / §12 #4 — a challenger is a *diff* against the champion, and
a SHORT-description edit provably overfits the board less than a long one
([Dwork et al.]: bounding the description length of the analyst's outputs
bounds overfitting under adaptivity). This module turns one
:class:`~zicato.core.types.Experiment`'s patch records into the two cheap,
deterministic proxies the scoring scalar and the proposer's parsimony
tie-break consume:

* :func:`diff_char_size` — the historical character-count proxy lifted
  verbatim from ``proposer/best_of_n.py`` (a per-patch constant + the length
  of each patch's replacement content). Used by the best-of-N self-critique
  tie-break to prefer the smaller edit.
* :func:`diff_size` — the structured ``{added, removed, patches}`` size the
  diff-complexity scoring term + the gate-surfaced ``diff_size:...`` evidence
  read. Given the PARENT-side content of each patched mutation point it
  reports the real line delta of the edit; without it, it falls back to the
  historical whole-replacement count (see the function docstring).

Both proxies are PURE / deterministic / no-I/O — they read only the patch
records already on the experiment plus, optionally, parent-side CONTENT the
caller already holds. Parent content is passed as text, never as a path: this
module never touches the filesystem, so the caller (which enumerated the
parent's mutation points and therefore already has their content in memory)
supplies the strings.

Calibration note (issue #120)
----------------------------
Before real delta accounting, a ``kind="file"`` patch was charged for EVERY
line of the file it re-emitted — a byte-identical re-emit of a 37-line
template scored complexity 38. With parent content threaded, the same edit
scores 0, and a genuine three-line change scores ~4 instead of ~38. Both
halves of the diff-complexity regularizer read this measure — the loss-term
:attr:`~zicato.core.types.ScoringWeights.diff_complexity_weight` and the
gate's Rule 0 :attr:`~zicato.core.types.ScoringWeights.diff_complexity_ceiling`
— so any weight or ceiling calibrated against the old file-charging numbers is
now roughly an order of magnitude too loose on a whole-file mutation surface
and should be re-tuned against a measured round.

The differencing is exact up to :data:`EXACT_DIFF_MAX_LINES` and bounded above
it; see that constant for the measured reason a size cap exists at all.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping

from zicato.core.types import Experiment

#: Per-patch constant folded into :func:`diff_char_size` so an extra patch is
#: never "free" (a candidate that splits one edit across two patches should not
#: score as cheaper than the single-patch form). Lifted verbatim from the
#: historical ``proposer/best_of_n._diff_size`` proxy.
_PER_PATCH_CHAR_CONSTANT: int = 16


def diff_char_size(experiment: Experiment) -> int:
    """A cheap character-count proxy for the description length of an edit.

    Counts the total characters of replacement content plus a small constant
    per patch, so a parsimony tie-break prefers the SMALLER edit (MDL /
    OVERFITTING.md §5: a shorter-description edit provably overfits the board
    less). A ``set_numeric`` / ``set_enum`` patch has no ``new_content`` but
    still counts its per-patch constant.

    This is the proxy the best-of-N self-critique tie-break reads; it is kept
    character-based (not line-based) so that historical selection behaviour is
    byte-identical to the inline ``_diff_size`` it replaced.
    """
    total = 0
    for patch in experiment.patches:
        total += _PER_PATCH_CHAR_CONSTANT
        if patch.new_content is not None:
            total += len(patch.new_content)
    return total


def _lines(text: str) -> list[str]:
    """Split replacement text into comparable lines (empty text ⇒ no lines).

    ``"".split("\\n")`` yields ``[""]`` — one empty line — which would make an
    empty-to-one-line edit read as ``added=1, removed=1``. Mapping empty text
    to no lines keeps the fallback count (``content.count("\\n") + 1`` for
    non-empty content, ``0`` for empty) and the differenced count in agreement.
    """
    return text.split("\n") if text else []


#: Line count above which :func:`_line_delta` re-enables ``difflib``'s
#: ``autojunk`` heuristic. Disabling ``autojunk`` is what makes the measure
#: exact, but it also removes the only bound on ``SequenceMatcher``'s running
#: time: with a line that repeats often (a blank line — every source file has
#: hundreds) and two sides that genuinely differ, the match search degrades
#: badly. Measured on this machine for a whole-file rewrite, ``autojunk=False``
#: against ``autojunk=True`` (sub-millisecond at every size):
#:
#: ===========  ==============  ==============  ==============
#: blank lines   1000 lines      2000 lines      4000 lines
#: ===========  ==============  ==============  ==============
#: 10%           0.13 s          0.60 s          —
#: 25%           1.4 s           5.8 s           —
#: 50%           3.7 s           29 s            157 s
#: ===========  ==============  ==============  ==============
#:
#: This runs once per round inside scoring, with no timeout above it, so an
#: unlucky whole-file mutation point could stall a round for minutes. The cap
#: keeps the exact path where it is cheap (worst measured case at 1000 lines:
#: 1.4 s) and bounded above it.
EXACT_DIFF_MAX_LINES: int = 1000


def _line_delta(parent: str, child: str) -> tuple[int, int]:
    """Return ``(added, removed)`` for one replacement against its parent text.

    A line-level :class:`difflib.SequenceMatcher` diff: every non-``equal``
    opcode contributes the lines it introduced to ``added`` and the lines it
    destroyed to ``removed`` (a ``replace`` opcode contributes to both, which
    is the honest MDL reading — the edit must describe both the deletion and
    the insertion). Identical texts yield ``(0, 0)``.

    ``autojunk`` is disabled up to :data:`EXACT_DIFF_MAX_LINES`: its heuristic
    drops lines appearing in more than 1% of a long sequence, which on a large
    generated file silently changes the measured size of an edit that touches
    those lines (measured: a 1000-line whole-file rewrite scores 800 exactly
    and 998 with the heuristic on). Beyond the cap the heuristic comes back —
    see the constant for why, and read the overcount honestly: it is exact for
    a TARGETED edit at any file size (a one-line change in a 3000-line file
    measures ``(1, 1)`` either way, because the popular lines fall in the
    matched run), and it overstates only a near-total rewrite, where the
    parsimony toll is large under any accounting. Deterministic either way:
    the cap is a size threshold, not a timeout.
    """
    parent_lines = _lines(parent)
    child_lines = _lines(child)
    autojunk = max(len(parent_lines), len(child_lines)) > EXACT_DIFF_MAX_LINES
    matcher = difflib.SequenceMatcher(a=parent_lines, b=child_lines, autojunk=autojunk)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
    return added, removed


def diff_size(
    experiment: Experiment,
    parent_contents: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Return the structured ``{added, removed, patches}`` diff size.

    The size the diff-complexity scoring term + the gate-surfaced evidence
    read. ``parent_contents`` maps ``mutation_id`` to the PARENT-side text of
    that mutation point — for a ``kind="span"`` point the span body, for a
    ``kind="file"`` point the whole file, exactly as
    :attr:`~zicato.core.mutation.MutationPoint.content` carries it. The caller
    enumerated those points against the parent snapshot and already holds the
    text, so it passes CONTENT (never a path) and this function stays pure.

    With the parent text in hand a patch is measured as the edit it really is:

    * ``added`` / ``removed`` — the line delta between the parent text and the
      replacement (:func:`_line_delta`). A re-emit of the parent verbatim —
      what every proposal against a ``kind="file"`` point does to the template
      it was required to preserve — measures ``0`` / ``0``, and a patch that
      drops 20 of 37 lines measures ``added=0, removed=20``.
    * ``patches`` — the number of patch records that actually CHANGED
      something. A no-op replacement is not an edit; charging its per-patch
      constant would reintroduce the same flat toll on proposing at all that
      counting its unchanged lines did (issue #120).

    Without ``parent_contents`` — or for a patch whose mutation point is
    missing from it — the historical count applies unchanged: ``added`` is the
    line count of the whole replacement (a non-empty single-line replacement
    counts ``1``, an empty one ``0``), ``removed`` contributes nothing, and the
    patch always counts toward ``patches``. This keeps every legacy caller
    byte-identical; it is a fallback, not a second policy, and it overstates
    a whole-file edit exactly as described in the module's calibration note.

    A content-less ``set_numeric`` / ``set_enum`` patch contributes no lines on
    either path and always counts toward ``patches``: the parent text of a
    numeric point is not comparable to the number replacing it, so there is no
    delta to measure and its presence is all the measure can honestly carry.
    """
    added = 0
    removed = 0
    patches = 0
    for patch in experiment.patches:
        content = patch.new_content
        if content is None:
            patches += 1
            continue
        parent = None if parent_contents is None else parent_contents.get(patch.mutation_id)
        if parent is None:
            # Fallback: no parent text for this point — the whole replacement
            # counts as added, exactly as before parent content was threaded.
            patches += 1
            if content:
                added += content.count("\n") + 1
            continue
        patch_added, patch_removed = _line_delta(parent, content)
        if patch_added == 0 and patch_removed == 0:
            continue
        patches += 1
        added += patch_added
        removed += patch_removed
    return {"added": added, "removed": removed, "patches": patches}


def diff_complexity(diff_size_dict: Mapping[str, int] | None) -> float:
    """Collapse a ``{added, removed, patches}`` size into one complexity scalar.

    ``complexity = added + removed + patches`` — the MDL description-length
    proxy the diff-complexity scoring term multiplies by
    :attr:`~zicato.core.types.ScoringWeights.diff_complexity_weight`. The patch
    count is folded in (not just the line delta) so an edit spread across more
    patch records is penalised even when each individual replacement is short,
    matching OVERFITTING.md §5's "mutation points touched / characters changed"
    complexity notion.

    Returns ``0.0`` for ``None`` (no diff size available — e.g. a champion side
    with no challenger experiment), so the caller's term vanishes exactly.
    """
    if diff_size_dict is None:
        return 0.0
    added = int(diff_size_dict.get("added", 0))
    removed = int(diff_size_dict.get("removed", 0))
    patches = int(diff_size_dict.get("patches", 0))
    return float(added + removed + patches)


__all__ = [
    "EXACT_DIFF_MAX_LINES",
    "diff_char_size",
    "diff_complexity",
    "diff_size",
]
