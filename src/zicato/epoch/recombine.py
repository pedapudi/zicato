"""Pure parent selector for the mechanical recombination slot (WS-REC).

A single tournament winner can only ever discount ONE challenger's diff,
so two complementary fixes that each improve a DIFFERENT slice of the
board — and that a parsimony-biased selector rejects one at a time
because neither alone clears the promote margin — are lost forever. This
module is the pure, IO-free core that decides whether a round has such a
pair and, if so, WHICH pair to merge. The orchestrator's
``_build_recombination_pair`` does the IO (reading records, per-entry
grids, and one Elo fold) and assembles the plain-data
:class:`ParentCandidate` tuples; everything here is a deterministic
function of that pre-fetched data, so the selector is exhaustively
testable per-predicate with no fixtures.

The 8 hard eligibility predicates (a candidate must clear ALL of #1–#6;
a PAIR must clear #7–#8), justified inline, are the settled design of
``docs/design`` WS-REC. Cross-regression is deliberately a RANKING
penalty, not a filter (per-entry single-sample verdicts are noisy — the
screen's confirm-before-veto lesson). The ranking is total and
deterministic: any evidence tie is broken lexicographically, so the same
pre-fetched data always mints the same pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from zicato.core.types import ExpectedDriftMovement, ExpectedMetricMovement, Patch

#: Elo scale midpoint used to default-fill a candidate whose rating the
#: fold has not yet produced (a fresh reject, or an index-absent
#: workspace). 1500 is the conventional Elo seed; filling both sides of a
#: pair with it makes the summed-Elo ranking key inert across an
#: all-unrated pool — it can only ever REORDER within an evidence tie,
#: never manufacture a pair.
DEFAULT_ELO: float = 1500.0

#: Hard cap on the recombination pool size — the N most-recent rejects the
#: builder reads per round. Bounds the O(N^2) pair scan and the per-entry
#: grid reads to a small constant regardless of epoch length.
RECOMBINE_POOL_MAX: int = 16


@dataclass(frozen=True, slots=True)
class ParentCandidate:
    """One rejected challenger as plain data for the pure selector.

    Assembled by the orchestrator's IO builder from the durable records
    (``experiment.json`` + the per-entry loss grid + the Elo fold). Carries
    NO board-entry identity beyond the improved/regressed id SETS, which
    the builder has already intersected with the round's TRAIN entries — so
    a holdout entry can never enter the selection (the envelope boundary).
    The patch payload rides along only so the minter can compose the union;
    the selector reads only ``patch_mutation_ids`` for disjointness.

    Fields
    ------
    generation_id:
        The rejected challenger's lineage id (``v7``). The pair provenance
        (``Experiment.recombined_from``) is built from two of these.
    decision:
        The settled tournament decision — must be ``"rejected"`` (#1).
    parent_generation_id:
        The lineage head this challenger was proposed against — must equal
        the round-start champion for #2 (the current-reign guard).
    is_placebo:
        ``True`` for a random-baseline calibration arm (#3 rejects it).
    is_recombined:
        ``True`` when this challenger was ITSELF a recombination mint (its
        ``recombined_from`` is non-empty) — #4 forbids chains in v1.
    patch_mutation_ids:
        The PATCH targets' mutation-id set (the applier is last-wins on a
        duplicate target, so a pair MUST be disjoint here — #7).
    improved_entry_ids / regressed_entry_ids:
        TRAIN entries this challenger flipped to passing / to failing vs
        the same champion. Complementarity (#8) reads ``improved``;
        cross-regression (ranking key 2) reads ``regressed``.
    elo:
        The candidate's folded Elo rating, or ``None`` (default-filled to
        :data:`DEFAULT_ELO` in the summed-Elo ranking key).
    patches:
        The reconstructed patch tuple (minter input only).
    core_idea / expected_drift_movements / expected_metric_movements:
        The hypothesis text the minter composes the recombined hypothesis
        from (minter input only).
    """

    generation_id: str
    decision: str
    parent_generation_id: str | None
    is_placebo: bool
    is_recombined: bool
    patch_mutation_ids: frozenset[str]
    improved_entry_ids: frozenset[str]
    regressed_entry_ids: frozenset[str]
    elo: float | None
    patches: tuple[Patch, ...]
    core_idea: str
    expected_drift_movements: tuple[ExpectedDriftMovement, ...] = ()
    expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()


def eligible_parents(
    candidates: list[ParentCandidate],
    *,
    champion_id: str,
    manifest_ids: frozenset[str],
) -> list[ParentCandidate]:
    """Filter the pool by the 6 per-candidate predicates (#1–#6).

    #1 **rejected** — not deferred: a live evidence loop is not a settled
       negative, so only a decided ``rejected`` challenger is recyclable.
    #2 **current reign** — ``parent_generation_id == champion_id``: the
       parent pointer IS the staleness guard; a promotion moves the
       champion and empties the pool automatically (no stale-child recall).
    #3 **non-placebo** — a random-baseline calibration arm is never a real
       improvement to merge.
    #4 **non-recombined parent** — no chains in v1 (a recombined mint's
       ``recombined_from`` is non-empty); keeps provenance one level deep.
    #6 **patches reconstructable + all mutation-ids in the manifest** — a
       target the current manifest no longer exposes cannot be applied, so
       the whole candidate is dropped. (Reconstructability is the builder's
       guard — only readable patch sets reach here; this enforces the
       manifest-membership half.) A patch-free candidate is dropped too:
       there is nothing to contribute to a union.

    Order-preserving; the caller controls pool ordering (most-recent
    first). Predicate #5 (pair-already-tried) and #7/#8 (disjoint /
    complementary) are PAIR-level and live in :func:`rank_pairs`.
    """
    kept: list[ParentCandidate] = []
    for c in candidates:
        if c.decision != "rejected":
            continue
        if c.parent_generation_id != champion_id:
            continue
        if c.is_placebo:
            continue
        if c.is_recombined:
            continue
        if not c.patch_mutation_ids:
            continue
        if not c.patch_mutation_ids <= manifest_ids:
            continue
        kept.append(c)
    return kept


def _complementary(a: ParentCandidate, b: ParentCandidate) -> bool:
    """#8 — improved sets both non-empty and neither is a subset of the other.

    The whole point of recombination: each parent must carry a DISTINCT
    win the other lacks, so the merge can capture both. Two challengers
    that improved the same slice (one ⊆ the other) add nothing a single
    winner would not — they are not complementary.
    """
    ia, ib = a.improved_entry_ids, b.improved_entry_ids
    if not ia or not ib:
        return False
    return not (ia <= ib) and not (ib <= ia)


def rank_pairs(
    eligible: list[ParentCandidate],
    *,
    tried_pairs: frozenset[frozenset[str]] = frozenset(),
    merge_mode: str = "mechanical",
) -> tuple[ParentCandidate, ParentCandidate] | None:
    """Return the single best mergeable pair (A, B) in ascending-gid order.

    Enumerates every unordered pair of the eligible pool, drops those that
    fail a PAIR predicate, and ranks the survivors by a deterministic,
    total key. ``None`` when no pair survives — the caller then mints
    nothing and the round is byte-identical.

    ``merge_mode`` (``"mechanical"`` default | ``"llm"``) is the ONLY split
    (WS-MERGE; PROPOSER.md §2.6.1): in ``"llm"`` mode predicate #7
    (disjointness) RELAXES for pair selection — the LLM merge resolves the
    overlap the mechanical mint cannot — and overlap becomes a RANKING
    penalty instead. EVERY other predicate holds in both modes (#8
    especially). Because a mechanical-mode survivor is disjoint BY the #7
    filter, its overlap key is always ``0`` and constant across survivors,
    so the mechanical selection is byte-identical to before this parameter
    existed.

    Pair predicates:
    #5 **pair not already tried** — a ``frozenset({gid_a, gid_b})`` in
       ``tried_pairs`` (built from every persisted ``recombined_from``) is
       skipped: a round-SPENDING mint must not be re-minted (a vetoed,
       unpersisted mint is not in the set, so it may retry).
    #7 **disjoint** patch mutation-id sets (the inline ``overlap`` count:
       zero means disjoint — the applier is last-wins on a duplicate
       target) — HARD in ``"mechanical"`` mode; in ``"llm"`` mode any
       overlap is allowed and ranked (key level 2 below).
    #8 **complementary** improved sets (:func:`_complementary`).

    Ranking key (each level only breaks the previous level's ties):
    1. combined TRAIN coverage DOWN — the union of the two improved sets;
       more distinct entries fixed is the whole objective.
    2. patch OVERLAP UP — the size of the two patch mutation-id sets'
       intersection; prefer LESS overlap at equal coverage (a cleaner merge
       for the LLM). Always ``0`` in ``"mechanical"`` mode (the #7 filter
       already dropped every overlapping pair), so this term never disturbs
       the mechanical ranking — it only orders ``"llm"``-mode survivors.
    3. cross-regression penalty UP — the union of the two regressed sets;
       fewer entries put at risk wins (a penalty, never a filter: per-entry
       single-sample verdicts are noisy, the screen + gate still adjudicate).
    4. summed Elo DOWN — higher-rated parents preferred; default-filled to
       :data:`DEFAULT_ELO` so it can only reorder within an evidence tie.
    5. lexicographic backstop — ``(gid_a, gid_b)`` ascending: a total order
       that makes the selection reproducible for any fixed pool, in any
       input order (the shuffled-pool order-independence pin).
    """
    allow_overlap = merge_mode == "llm"
    best: tuple[ParentCandidate, ParentCandidate] | None = None
    best_key: tuple[int, int, int, float, str, str] | None = None
    for x, y in combinations(eligible, 2):
        # Canonicalise to ascending-gid (A, B) so the minted patch order
        # and the ranking backstop are order-independent.
        a, b = (x, y) if x.generation_id <= y.generation_id else (y, x)
        if frozenset({a.generation_id, b.generation_id}) in tried_pairs:
            continue
        overlap = len(a.patch_mutation_ids & b.patch_mutation_ids)
        if overlap and not allow_overlap:
            # #7 disjointness — HARD in mechanical mode only.
            continue
        if not _complementary(a, b):
            continue
        coverage = len(a.improved_entry_ids | b.improved_entry_ids)
        cross_regression = len(a.regressed_entry_ids | b.regressed_entry_ids)
        summed_elo = (a.elo if a.elo is not None else DEFAULT_ELO) + (
            b.elo if b.elo is not None else DEFAULT_ELO
        )
        key = (
            -coverage,  # more coverage first
            overlap,  # less overlap first (constant 0 in mechanical mode)
            cross_regression,  # less risk first
            -summed_elo,  # higher rating first
            a.generation_id,  # lexicographic backstop
            b.generation_id,
        )
        if best_key is None or key < best_key:
            best_key = key
            best = (a, b)
    return best


__all__ = [
    "DEFAULT_ELO",
    "RECOMBINE_POOL_MAX",
    "ParentCandidate",
    "eligible_parents",
    "rank_pairs",
]
