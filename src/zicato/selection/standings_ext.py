"""Opt-in standings extensions: rating + resolver knobs over the audit.

The glue between the pure :mod:`zicato.selection.rating` /
:mod:`zicato.selection.resolve` layers and the strategies. It reads the two
opt-in ``TournamentStructure.params`` knobs — ``rating`` and ``resolver`` —
and converts a strategy's flat audit (the ``MatchupResult`` list every
non-gauntlet strategy already accumulates) into the inputs those pure layers
consume:

* :func:`audit_duels` → per-duel ``(winner, loser)`` Bernoulli outcomes for
  Bradley--Terry, one per replicated duel.
* :func:`audit_matrix` → the net :class:`~zicato.selection.resolve.MarginMatrix`
  the resolvers read.

Both are derived ENTIRELY from already-measured data — the gate's
``delta_scalar`` (``right - left``; negative ⇒ ``right`` is better) and the
two side scalars — so a rating / resolver choice costs **zero new board
runs**. The winner of each audited duel is its lower-scalar side
(``MatchupResult.lower_scalar_id``), exactly the side the strategies already
advance; the margin is ``|delta_scalar|``.

Everything here is **opt-in**: :func:`read_rating` / :func:`read_resolver`
return ``None`` when the knob is absent or set to its default, in which
case the strategy takes its Copeland / scalar path unchanged. A
present knob never touches the gate — it only re-orders the INTERNAL leader
pick (the ``_maybe_final`` / survivor step).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zicato.selection.rating import (
    DuelOutcome,
    fit_bradley_terry,
    theta_rank,
)
from zicato.selection.resolve import Duel, MarginMatrix, build_matrix, resolve_leader
from zicato.selection.strategy import MatchupResult

#: The rating knob's accepted values. ``"bradley_terry"`` enables the BT
#: standings/uncertainty layer; absence (or ``"none"``) is the default behaviour.
_VALID_RATINGS = frozenset({"bradley_terry"})

#: The resolver knob's accepted values for INTERNAL leader selection. Absence
#: (or ``"none"``) keeps each structure's existing leader pick.
_VALID_RESOLVERS = frozenset({"copeland", "ranked_pairs"})


def read_rating(params: Mapping[str, Any]) -> str | None:
    """The selected rating model, or ``None`` for the default behaviour.

    Reads ``params["rating"]``; returns the lower-cased token only when it
    is a recognised non-``none`` rating. Anything absent, ``none``, empty,
    or unrecognised ⇒ ``None`` (the byte-identical default path). The knob
    is read defensively because ``params`` is an opaque operator-supplied
    map.
    """
    raw = params.get("rating", None)
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    return token if token in _VALID_RATINGS else None


def read_resolver(params: Mapping[str, Any]) -> str | None:
    """The selected internal-leader resolver, or ``None`` for the pick.

    Reads ``params["resolver"]``; returns the token only when it is a
    recognised non-``none`` resolver. Absent / ``none`` / unrecognised ⇒
    ``None``, leaving the strategy's existing leader selection in place.
    """
    raw = params.get("resolver", None)
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    return token if token in _VALID_RESOLVERS else None


def audit_duels(audit: Sequence[MatchupResult]) -> list[DuelOutcome]:
    """Convert a strategy audit into Bradley--Terry ``(winner, loser)`` outcomes.

    One outcome per audited duel: the winner is the lower-scalar side (the
    side the strategies already advance), the loser the other. A duel whose
    two sides tie on scalar is skipped (no definite winner to feed the
    likelihood). Byes / single-competitor records never reach the audit, so
    every entry here is a genuine pairwise verdict.
    """
    out: list[DuelOutcome] = []
    for r in audit:
        if r.left_id == r.right_id:
            continue
        delta = r.outcome.delta_scalar  # right - left; < 0 ⇒ right better
        if delta < 0.0:
            out.append((r.right_id, r.left_id))
        elif delta > 0.0:
            out.append((r.left_id, r.right_id))
        # delta == 0.0 ⇒ exact tie, skipped.
    return out


def audit_matrix(audit: Sequence[MatchupResult]) -> MarginMatrix:
    """Build the net margin matrix the resolvers read from a strategy audit.

    Each audited duel contributes one :class:`~zicato.selection.resolve.Duel`
    from its lower-scalar winner to the loser, with margin ``|delta_scalar|``.
    :func:`~zicato.selection.resolve.build_matrix` nets replicated /
    conflicting verdicts per pairing.
    """
    duels: list[Duel] = []
    for r in audit:
        if r.left_id == r.right_id:
            continue
        delta = r.outcome.delta_scalar
        margin = abs(delta)
        if delta < 0.0:
            duels.append(Duel(winner=r.right_id, loser=r.left_id, margin=margin))
        elif delta > 0.0:
            duels.append(Duel(winner=r.left_id, loser=r.right_id, margin=margin))
    return build_matrix(duels)


def rating_order(audit: Sequence[MatchupResult]) -> list[str]:
    """Bradley--Terry strength order (best-first) over an audit, or ``[]``.

    Fits BT on the audit's per-duel outcomes and returns the ids by
    descending strength. Empty when the audit has no resolvable duels. The
    drop-in for the Copeland / scalar standings sort when
    ``rating="bradley_terry"`` is selected.
    """
    duels = audit_duels(audit)
    if not duels:
        return []
    rating = fit_bradley_terry(duels)
    return theta_rank(rating)


def resolver_leader(audit: Sequence[MatchupResult], resolver: str) -> str | None:
    """The resolver's proposed internal leader over an audit, or ``None``.

    Builds the net margin matrix and runs the selected resolver
    (Smith-prune + Ranked Pairs, or Copeland) through
    :func:`~zicato.selection.resolve.resolve_leader`. ``None`` when the audit
    yields no resolvable field.
    """
    matrix = audit_matrix(audit)
    if not matrix.ids:
        return None
    return resolve_leader(matrix, resolver)


__all__ = [
    "read_rating",
    "read_resolver",
    "audit_duels",
    "audit_matrix",
    "rating_order",
    "resolver_leader",
]
