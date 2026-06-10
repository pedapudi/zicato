"""Winner-resolution over a pairwise duel matrix (cycle-robust).

The resolver tier described in ``docs/design/SELECTION-THEORY.md`` §3 / §5
and ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §5. Given a matrix of
completed duels — possibly **cyclic**, because the loss is noisy — these
pure functions turn it into a single proposed winner, principled under
cycles:

* :func:`condorcet_check` — the O(n²) fast path: a contestant who beats
  *every* other head-to-head is the unambiguous winner; every method below
  collapses to it when it exists.
* :func:`smith_set` — the smallest dominant set (top cycle); a cheap
  O(n²) front prune, since the winner can only ever be inside it.
* :func:`ranked_pairs` — Tideman's margin-sorted lock/skip, with an
  **auditable trace** of exactly which duels were locked and which were
  skipped because they would have closed a cycle.

Every function is **pure** — it reads a frozen matrix and returns a value;
no strategy state, no IO, no external numerical dependency. The output only
ever *proposes* an internal leader; the unchanged champion-gate still owns
promotion. A resolver may name the wrong leader and the worst case is a
wasted confirmation duel — never an unsafe promotion.

The input is an opaque sequence of :class:`Duel` records (``winner``,
``loser``, ``margin``). Replicates of the same pairing are aggregated by
*net margin* — a pairing both sides have "won" at different times nets to
whichever side accumulated the larger total margin, which is exactly the
right way to read a noisy measurement: the strongest, most-separated
verdicts dominate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Duel:
    """One pairwise verdict: ``winner`` beat ``loser`` by ``margin``.

    ``margin`` is the (non-negative) loss gap — the strength of the verdict.
    A larger margin is a more-separated, less-noise-prone result, which is
    what Ranked Pairs locks first. Replicates of the same pairing are passed
    as separate ``Duel`` records; the matrix builder nets them.
    """

    winner: str
    loser: str
    margin: float = 0.0


@dataclass(frozen=True, slots=True)
class MarginMatrix:
    """An aggregated pairwise margin matrix over a contestant field.

    ``ids`` is the contestant set (insertion-ordered for a stable,
    deterministic resolution). ``net[(a, b)]`` is the net margin by which
    ``a`` beat ``b`` after aggregating all duels — strictly positive when
    ``a`` is the net winner of the pairing, absent / non-positive otherwise.
    Built by :func:`build_matrix`; consumed by every resolver below.
    """

    ids: tuple[str, ...]
    net: dict[tuple[str, str], float] = field(default_factory=dict)

    def beats(self, a: str, b: str) -> bool:
        """True when ``a`` is the net winner over ``b`` (strictly)."""
        return self.net.get((a, b), 0.0) > 0.0

    def margin(self, a: str, b: str) -> float:
        """The net margin by which ``a`` beat ``b`` (``0.0`` if it did not)."""
        return self.net.get((a, b), 0.0)


def build_matrix(duels: Iterable[Duel]) -> MarginMatrix:
    """Aggregate raw duels into a net :class:`MarginMatrix`.

    Replicated / conflicting verdicts for a pairing are summed signed: each
    ``Duel`` adds ``+margin`` to the winner→loser direction. The net for an
    unordered pair is then whichever direction has the larger total; the
    losing direction is dropped (a non-positive net is "did not win"). A
    pairing that nets to exactly zero (two equal-and-opposite verdicts) is
    recorded as *no* edge in either direction — an honest "unresolved tie",
    which the resolvers treat as a missing comparison.
    """
    ids: list[str] = []
    seen: set[str] = set()
    raw: dict[tuple[str, str], float] = {}
    for d in duels:
        if d.winner == d.loser:
            continue
        for gid in (d.winner, d.loser):
            if gid not in seen:
                seen.add(gid)
                ids.append(gid)
        key = (d.winner, d.loser)
        raw[key] = raw.get(key, 0.0) + abs(d.margin)

    net: dict[tuple[str, str], float] = {}
    handled: set[tuple[str, str]] = set()
    for a, b in raw:
        unordered = (a, b) if a <= b else (b, a)
        if unordered in handled:
            continue
        handled.add(unordered)
        fwd = raw.get((a, b), 0.0)
        rev = raw.get((b, a), 0.0)
        diff = fwd - rev
        if diff > 0.0:
            net[(a, b)] = diff
        elif diff < 0.0:
            net[(b, a)] = -diff
        # diff == 0.0 ⇒ no edge (unresolved tie).
    return MarginMatrix(ids=tuple(ids), net=net)


def condorcet_check(matrix: MarginMatrix) -> str | None:
    """Return the Condorcet winner if one exists, else ``None`` (O(n²)).

    A Condorcet winner beats *every* other contestant head-to-head. If one
    exists it is unique and is the unambiguous winner — the fast path every
    resolver collapses to. ``None`` means there is a cycle (or an
    unresolved pairing) and a real resolver must run.
    """
    ids = matrix.ids
    for a in ids:
        if all(matrix.beats(a, b) for b in ids if b != a):
            return a
    return None


def smith_set(matrix: MarginMatrix) -> tuple[str, ...]:
    """The Smith set (top cycle): the smallest dominant set (O(n²)-ish).

    The smallest non-empty set ``S`` such that every member of ``S`` beats
    every contestant outside ``S``. When a Condorcet winner exists the Smith
    set is exactly that one contestant. Used as a **front prune**: any
    contestant outside the Smith set is provably dominated and need not be
    considered, which often collapses the field to a single element.

    Returned in the matrix's contestant order (stable). A pairing with no
    net edge in either direction (an unresolved tie) is treated as "neither
    beats the other", so neither dominates across it — the conservative
    reading that keeps both in contention.
    """
    ids = list(matrix.ids)
    n = len(ids)
    if n == 0:
        return ()

    # "Dominates" relation: a dominates b when a beats b and b does not beat
    # a. Compute the reachability closure, then the Smith set is the unique
    # top strongly-connected component under the *beats-or-not-beaten-by*
    # relation. We use the standard definition via the dominance closure:
    # S is the smallest set closed under "beats" that no outsider beats into.
    #
    # Concretely: start from the contestant(s) with the best Copeland-style
    # standing and grow the set by anyone who beats a current member, until
    # closed. The smallest such closed dominant set is the Smith set.

    def beats(i: int, j: int) -> bool:
        return matrix.beats(ids[i], ids[j])

    # Copeland score (wins minus losses) to seed the search from the top.
    score = [0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if beats(i, j):
                score[i] += 1
            elif beats(j, i):
                score[i] -= 1

    order = sorted(range(n), key=lambda i: (-score[i], i))
    top = order[0]

    # Grow the dominant set: include the seed, then repeatedly add anyone who
    # beats a member (i.e. is not dominated by the set), until closed. The
    # Smith set is the smallest set with no incoming "beats" from outside.
    members = {top}
    changed = True
    while changed:
        changed = False
        for outsider in range(n):
            if outsider in members:
                continue
            # If the outsider beats any member, it must join (a member does
            # not dominate it, so the set is not yet dominant).
            if any(beats(outsider, m) for m in members):
                members.add(outsider)
                changed = True
    return tuple(ids[i] for i in sorted(members))


@dataclass(frozen=True, slots=True)
class LockStep:
    """One entry in the Ranked-Pairs lock-in trace (auditable).

    Fields
    ------
    winner, loser:
        The pairwise verdict this step considered (``winner`` beat
        ``loser``).
    margin:
        Its net margin — the sort key; larger margins are locked first.
    locked:
        ``True`` when the edge was locked into the order, ``False`` when it
        was *skipped* because locking it would have closed a cycle with the
        already-locked edges.
    """

    winner: str
    loser: str
    margin: float
    locked: bool


@dataclass(frozen=True, slots=True)
class RankedPairsResult:
    """The outcome of :func:`ranked_pairs`.

    Fields
    ------
    winner:
        The proposed winner (the source of the locked DAG), or ``None`` for
        an empty field.
    order:
        A full ranking best-first, derived from the locked acyclic relation
        (topological order, ties broken by net Copeland then id).
    trace:
        The ordered lock/skip trace — the auditable artifact that explains
        the resolution ("we trusted the most-separated duels and skipped the
        ones that would have made a cycle").
    """

    winner: str | None
    order: tuple[str, ...]
    trace: tuple[LockStep, ...]


def ranked_pairs(matrix: MarginMatrix) -> RankedPairsResult:
    """Tideman's Ranked Pairs over a margin matrix (polynomial, auditable).

    Sort every net pairwise verdict by margin, strongest first; lock each
    in that order, **skipping** any that would create a cycle with the
    edges already locked. The resulting acyclic relation has a unique source
    — the proposed winner. Deterministic and fully auditable: the returned
    :class:`RankedPairsResult.trace` records exactly which edges were locked
    and which were skipped.

    Condorcet-consistent (it returns the Condorcet winner whenever one
    exists), margin-aware (the most-separated, least-noisy verdicts are
    locked first), cloneproof, and monotone. The reference resolver of
    SELECTION-THEORY.md §5.2 / §8 #1.

    Ties in margin break by ``(winner_id, loser_id)`` so the lock order — and
    thus the winner — is deterministic across runs.
    """
    ids = matrix.ids
    n = len(ids)
    if n == 0:
        return RankedPairsResult(winner=None, order=(), trace=())
    if n == 1:
        return RankedPairsResult(winner=ids[0], order=(ids[0],), trace=())

    # All net edges, sorted by margin desc, then deterministically by id.
    edges = sorted(
        ((a, b, m) for (a, b), m in matrix.net.items() if m > 0.0),
        key=lambda e: (-e[2], e[0], e[1]),
    )

    # Lock edges that do not introduce a cycle. ``reach[a]`` = set of nodes
    # reachable from ``a`` through locked edges; an edge a→b is safe to lock
    # iff b cannot already reach a.
    locked: set[tuple[str, str]] = set()
    reach: dict[str, set[str]] = {gid: set() for gid in ids}
    trace: list[LockStep] = []
    for a, b, m in edges:
        if a in reach[b] or b == a:
            trace.append(LockStep(winner=a, loser=b, margin=m, locked=False))
            continue
        # Lock a→b: b and everything b reaches become reachable from a and
        # from everything that reaches a.
        locked.add((a, b))
        newly = {b} | reach[b]
        for node in ids:
            if node == a or a in reach[node]:
                reach[node].update(newly)
        reach[a].update(newly)
        trace.append(LockStep(winner=a, loser=b, margin=m, locked=True))

    # Standing from the locked DAG: a node's rank is better when more nodes
    # are reachable from it (it sits higher in the order). Break ties by net
    # Copeland (wins - losses over the raw matrix) then id, for determinism.
    cope: dict[str, int] = {}
    for a in ids:
        c = 0
        for b in ids:
            if a == b:
                continue
            if matrix.beats(a, b):
                c += 1
            elif matrix.beats(b, a):
                c -= 1
        cope[a] = c
    order = sorted(ids, key=lambda gid: (-len(reach[gid]), -cope[gid], gid))
    return RankedPairsResult(winner=order[0], order=tuple(order), trace=tuple(trace))


def copeland_order(matrix: MarginMatrix) -> tuple[str, ...]:
    """Rank the field best-first by Copeland score (wins minus losses).

    The margin-blind baseline resolver — the relation today's swiss already
    uses. Provided so ``resolver="copeland"`` routes through this module's
    one matrix substrate rather than re-deriving the count inline. Ties on
    Copeland score break by id for determinism.
    """
    ids = matrix.ids
    cope: dict[str, int] = {}
    for a in ids:
        c = 0
        for b in ids:
            if a == b:
                continue
            if matrix.beats(a, b):
                c += 1
            elif matrix.beats(b, a):
                c -= 1
        cope[a] = c
    return tuple(sorted(ids, key=lambda gid: (-cope[gid], gid)))


def resolve_leader(matrix: MarginMatrix, resolver: str) -> str | None:
    """Pick the proposed internal leader from a matrix under ``resolver``.

    A thin dispatch the strategies call for their INTERNAL leader selection
    only (never the gate). ``resolver``:

    * ``"ranked_pairs"`` — Smith-prune then Ranked Pairs (the recommended
      resolver); falls back to the full field if the prune is empty.
    * ``"copeland"`` — Copeland order over the (Smith-pruned) field.

    Returns the leader id, or ``None`` for an empty field. The Condorcet
    fast path short-circuits both resolvers. Any other ``resolver`` value
    raises — the caller validates the knob before reaching here.
    """
    if not matrix.ids:
        return None
    condorcet = condorcet_check(matrix)
    if condorcet is not None:
        return condorcet
    smith = smith_set(matrix)
    pruned = _restrict(matrix, smith) if smith else matrix
    if resolver == "ranked_pairs":
        return ranked_pairs(pruned).winner
    if resolver == "copeland":
        order = copeland_order(pruned)
        return order[0] if order else None
    raise ValueError(f"unknown resolver {resolver!r}; expected 'ranked_pairs' or 'copeland'")


def _restrict(matrix: MarginMatrix, keep: Sequence[str]) -> MarginMatrix:
    """Restrict a matrix to a subset of contestants (preserving net edges)."""
    keep_set = set(keep)
    ids = tuple(gid for gid in matrix.ids if gid in keep_set)
    net = {(a, b): m for (a, b), m in matrix.net.items() if a in keep_set and b in keep_set}
    return MarginMatrix(ids=ids, net=net)


__all__ = [
    "Duel",
    "MarginMatrix",
    "LockStep",
    "RankedPairsResult",
    "build_matrix",
    "condorcet_check",
    "smith_set",
    "ranked_pairs",
    "copeland_order",
    "resolve_leader",
]
