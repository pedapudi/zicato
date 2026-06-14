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
  read. ``added`` counts the lines of replacement content the edit introduces,
  ``patches`` counts the patch records, and ``removed`` is reported as ``0``:
  the patch records carry only the NEW content (the old span lives in the
  parent snapshot, which is not threaded into scoring), so a line-accurate
  ``removed`` is not derivable at this layer. The structured shape leaves room
  for a future snapshot-diff backend to populate ``removed`` without changing
  the term's wiring.

Both proxies are PURE / deterministic / no-I/O — they read only the patch
records already on the experiment.
"""

from __future__ import annotations

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


def diff_size(experiment: Experiment) -> dict[str, int]:
    """Return the structured ``{added, removed, patches}`` diff size.

    The size the diff-complexity scoring term + the gate-surfaced evidence
    read:

    * ``patches`` — the number of patch records (every op counts, including a
      content-less ``set_numeric`` / ``set_enum``).
    * ``added`` — the total LINES of replacement content the edit introduces:
      for each ``new_content`` the number of ``"\\n"``-separated lines (a
      non-empty single-line replacement counts as ``1``; an empty replacement
      counts as ``0``). A content-less numeric/enum patch contributes no
      ``added`` lines — its presence is captured by ``patches``.
    * ``removed`` — always ``0`` at this layer: the patch records carry only
      the NEW content, so a line-accurate removed-line count is not derivable
      without the parent snapshot (not threaded into scoring). The key is kept
      so the gate evidence shape is stable and a future snapshot-diff backend
      can populate it without re-wiring the term.
    """
    added = 0
    patches = 0
    for patch in experiment.patches:
        patches += 1
        content = patch.new_content
        if content:
            # A multi-line replacement adds one line per "\n"-separated piece;
            # a single non-empty line adds 1. An empty string adds 0.
            added += content.count("\n") + 1
    return {"added": added, "removed": 0, "patches": patches}


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
    "diff_char_size",
    "diff_complexity",
    "diff_size",
]
