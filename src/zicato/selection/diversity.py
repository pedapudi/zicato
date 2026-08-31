"""Field-diversity summarization — pure set math over mutation-id sets.

Selection-layer concern: a multi-challenger field is only as informative
as its distinct ideas, so the soft-reject policy and the dashboard's
tournament view both need the same pairwise-overlap summary. The
functions here are pure summarizers — they neither query nor enforce.
The orchestrator's enforcement policy and the :mod:`zicato.query`
readers are the two consumers.
"""

from __future__ import annotations

from typing import Any


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap ``|a ∩ b| / |a ∪ b|`` of two mutation-id sets.

    ``0.0`` when both sets are empty (two challengers that target nothing do
    not collapse the field — there is nothing shared to collapse), and
    otherwise the standard set-similarity ratio in ``[0, 1]``.
    """
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def compute_field_diversity(
    mutation_sets: list[tuple[str, frozenset[str]]],
    *,
    tolerance: float | None = None,
    soft_rejected_count: int = 0,
) -> dict[str, Any]:
    """Summarize the field's idea diversity from per-challenger mutation sets.

    ``mutation_sets`` is an ordered list of ``(generation_id, mutation_ids)``
    pairs, one per challenger whose targeted-mutation-id set is known. The
    returned block reports the pairwise Jaccard overlap structure of the
    field (FUNCTIONALITY-RECOMMENDATIONS.md §4.3).

    Two challengers proposing the same mutation-id set collapse a field of N
    into fewer than N real experiments. So the block surfaces
    ``distinct_ideas`` — the count of distinct mutation-id sets — and the
    mean and max pairwise overlap that a soft-reject policy keys off.

    Keys
    ----
    field_size:
        Number of challengers considered.
    distinct_ideas:
        Number of distinct (non-empty) mutation-id sets; an empty set never
        counts as an idea (it cannot collapse the field).
    mean_overlap / max_overlap:
        Mean and max pairwise Jaccard overlap across all challenger pairs
        (``0.0`` for a field of fewer than two challengers).
    max_overlap_pair:
        The ``[gid_a, gid_b]`` of the most-overlapping pair (``None`` when
        there is no pair).
    tolerance:
        The configured ``diversity_tolerance`` (``None`` ⇒ enforcement off).
    soft_rejected_count:
        How many challengers the enforcement soft-rejected this field.

    This is a pure summarizer over the supplied sets; it neither queries nor
    enforces. The orchestrator feeds it the accepted field for the live
    envelope; the dashboard reader feeds it the persisted patch records.
    """
    field_size = len(mutation_sets)
    distinct = {ids for _gid, ids in mutation_sets if ids}
    mean_overlap = 0.0
    max_overlap = 0.0
    max_pair: list[str] | None = None
    pair_overlaps: list[float] = []
    for i in range(field_size):
        for j in range(i + 1, field_size):
            score = jaccard(mutation_sets[i][1], mutation_sets[j][1])
            pair_overlaps.append(score)
            if score > max_overlap:
                max_overlap = score
                max_pair = [mutation_sets[i][0], mutation_sets[j][0]]
    if pair_overlaps:
        mean_overlap = sum(pair_overlaps) / len(pair_overlaps)
    return {
        "field_size": field_size,
        "distinct_ideas": len(distinct),
        "mean_overlap": round(mean_overlap, 6),
        "max_overlap": round(max_overlap, 6),
        "max_overlap_pair": max_pair,
        "tolerance": tolerance,
        "soft_rejected_count": int(soft_rejected_count),
    }


__all__ = ["compute_field_diversity", "jaccard"]
