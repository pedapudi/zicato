"""Winner-resolution over a pairwise duel matrix (cycle-robust).

The resolver tier described in ``docs/design/SELECTION-THEORY.md`` §3 / §5
and ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §5. Given a matrix of
completed duels — possibly **cyclic**, because the loss is noisy — these
pure functions turn it into a single proposed winner, principled under
cycles:

* :func:`condorcet_check` — the O(n²) fast path: a contestant who beats
  *every* other head-to-head is the unambiguous winner; every method below
  collapses to it when it exists.
* :func:`smith_set` — the smallest strict dominating set; an O(n²) prune
  that retains missing and tied comparisons.
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
    """Return the smallest strict dominating set in O(n²), preserving input order.

    Every retained contestant must strictly beat every outsider. Missing and
    tied comparisons therefore keep both contestants in contention. The matrix
    must be asymmetric, as produced by ``build_matrix``: a pairing has at most
    one positive direction.
    """
    ids = matrix.ids
    if not ids:
        return ()

    # Every member of a strict dominating set has a higher wins-minus-losses
    # score than every outsider. A maximum-score seed is therefore inside the
    # smallest such set, even with missing or tied internal comparisons.
    seed = max(
        ids,
        key=lambda candidate: sum(
            int(matrix.beats(candidate, other)) - int(matrix.beats(other, candidate))
            for other in ids
            if other != candidate
        ),
    )
    members = {seed}
    pending = [seed]
    while pending:
        member = pending.pop()
        for outsider in ids:
            # An outsider the member cannot beat must join any dominating set
            # containing that member. Each admitted member is visited once.
            if outsider not in members and not matrix.beats(member, outsider):
                members.add(outsider)
                pending.append(outsider)
    return tuple(gid for gid in ids if gid in members)


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
    edges already locked. The resulting acyclic relation may have several
    sources when comparisons are unresolved. Its deterministic ranking names
    the proposed winner. The returned
    :class:`RankedPairsResult.trace` records exactly which edges were locked
    and which were skipped.

    Condorcet-consistent (it returns the Condorcet winner whenever one
    exists) and margin-aware (larger measured margins are locked first).
    These properties do not establish empirical effectiveness. The resolver of
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

    The margin-blind baseline resolver — the relation the swiss already
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

    * ``"ranked_pairs"`` — strict-dominance prune followed by Ranked Pairs;
      falls back to the full field if the prune is empty.
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
