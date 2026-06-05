"""Train/holdout split of an evaluation board (OVERFITTING.md §3, §12 #1).

The board is a finite proxy for true harness quality. Reused every round
of an epoch and queried adaptively by the proposer, it is exactly the
adaptive-data-analysis setting where a single fixed slice "gets used up"
and the measured loss becomes an optimistically-biased estimate of true
quality. The standard discipline is a train/holdout split: the proposer
and the pattern detectors see only the *train* slice; a held-out slice is
touched only to *confirm* a promotion (the train-measured win must also
hold on the holdout) and is never shown to the proposer, never used to
pick the edit.

This module owns the *pure* split rule. It does not run anything, read
the filesystem, or touch randomness or the clock — given the same board
and config it always returns the same partition, so the split is stable
within an epoch and reproducible across processes.

The rule (in priority order):

1. **Explicit ``holdout`` tag wins.** If *any* entry carries the
   ``"holdout"`` tag, the tagged entries are the holdout and the rest are
   train — regardless of :attr:`OverfittingConfig.enabled` or board size.
   This is the zero-schema-change way an operator declares the split by
   hand (``BoardEntry.tags`` already exists).
2. **Hash-derived split** when no entry is tagged, the config is enabled,
   and the board is large enough (``len >= min_board_size_for_split``):
   a deterministic, id-stable threshold over ``sha256(entry.id)`` selects
   approximately :attr:`OverfittingConfig.holdout_fraction` of the board.
   No ``random`` module, no time — the same ids always map the same way.
3. **Empty holdout (degrade)** otherwise — disabled, or a board too small
   to afford giving up train entries. The caller then behaves exactly as
   it did before the split existed (byte-identical), because an empty
   holdout skips every holdout-gated step.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from zicato.core.types import BoardEntry, OverfittingConfig

#: The reserved tag an operator sets on a :class:`BoardEntry` to declare
#: it part of the holdout slice by hand. Presence of *any* such tag on the
#: board switches the split into explicit mode (see the module docstring).
HOLDOUT_TAG = "holdout"

#: Resolution of the id-hash threshold. ``sha256(id)`` is reduced to an
#: integer in ``[0, _HASH_BUCKETS)``; an id lands in the holdout when that
#: integer is below ``holdout_fraction * _HASH_BUCKETS``. A large modulus
#: keeps the realised fraction close to the target for normal board sizes.
_HASH_BUCKETS = 1_000_000


def _hash_bucket(entry_id: str) -> int:
    """Map an entry id to a stable integer in ``[0, _HASH_BUCKETS)``.

    Uses the first 8 bytes of ``sha256(id)`` so the mapping is process- and
    platform-independent (unlike the salted built-in ``hash``).
    """
    digest = hashlib.sha256(entry_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % _HASH_BUCKETS


def split_board(
    entries: Sequence[BoardEntry],
    cfg: OverfittingConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Partition ``entries`` into ``(train_ids, holdout_ids)``.

    Both halves are returned as tuples of entry ids, each in the input
    order (so callers get a stable, board-order view). Their union is
    exactly the set of input ids and they never overlap.

    See the module docstring for the priority order. The function is pure
    and deterministic: no filesystem, no randomness, no clock.
    """
    ids = [e.id for e in entries]

    # Rule 1: an explicit ``holdout`` tag on any entry wins outright.
    tagged_holdout = {e.id for e in entries if HOLDOUT_TAG in e.tags}
    if tagged_holdout:
        train = tuple(i for i in ids if i not in tagged_holdout)
        holdout = tuple(i for i in ids if i in tagged_holdout)
        return train, holdout

    # Rule 3 (degrade): disabled, or too small to split → empty holdout.
    if not cfg.enabled or len(ids) < cfg.min_board_size_for_split:
        return tuple(ids), ()

    # Rule 2: deterministic, id-stable hash threshold for ~holdout_fraction.
    threshold = cfg.holdout_fraction * _HASH_BUCKETS
    holdout_set = {i for i in ids if _hash_bucket(i) < threshold}
    # Degenerate guards: a fraction that selects everything (no train left)
    # or nothing (no holdout) collapses to the empty-holdout degrade so the
    # split never starves the train slice or surfaces a no-op holdout.
    if not holdout_set or len(holdout_set) == len(ids):
        return tuple(ids), ()
    train = tuple(i for i in ids if i not in holdout_set)
    holdout = tuple(i for i in ids if i in holdout_set)
    return train, holdout


__all__ = ["HOLDOUT_TAG", "split_board"]
