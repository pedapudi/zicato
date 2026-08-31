"""Pure, deterministic sampler for the genealogy channel. NO IO.

The in-context analogue of AlphaEvolve's prompt sampler: it feeds the
proposer a redacted view of the current reign's candidate LINEAGE so the
LLM can evolve in context — extend a winning line, or re-frame a rejected
idea — reaching even the pure-drift-side complementary pairs the mechanical
recombination slot (:mod:`zicato.epoch.recombine`) cannot see. Where that
slot merges two rejected fixes WITHOUT an LLM call, this channel hands the
LLM the raw material to merge them itself.

The design + the normative redaction contract live in
``docs/design/PROPOSER.md`` §2.7; this module is its mechanical enforcement.
The invariants, mirroring the process-exemplar / recombination precedents:

* **Envelope-safe by construction.** A :class:`GenealogyItem` carries
  proposer-AUTHORED artifacts (the hypothesis ``core_idea``, a capped
  excerpt of the PATCH DIFF TEXT — content the proposer wrote) plus a
  WHOLE-CANDIDATE BANDED outcome. It NEVER carries a board-entry id, a
  per-entry result, an exact Δscalar, or anything holdout-derived — there
  is no per-entry read here at all, so there is no per-entry slice to leak
  (PROPOSER.md §2.7; OVERFITTING.md §11).
* **Banded, reusing the existing vocabulary.** The whole-candidate outcome
  is coarsened through :func:`zicato.proposer.prompts._bucket_scalar_delta`
  — the same ``improved`` / ``flat`` / ``regressed`` three-band vocabulary
  the experiment-memory block already renders — so no new banding primitive
  and no exact response-surface number reaches the model.
* **Deterministic + budget-capped.** No RNG, no wall clock. Parents are the
  champion's promoted spine, walked by ``parent_generation_id`` pointer;
  inspirations are a greedy max--min-Jaccard
  diversity walk with a TOTAL tie-break (Elo down, then generation-id
  ascending), so the same pool yields byte-identical items in ANY input
  order — the leakage budget is a stable block round over round. The
  inspiration pool is capped at :data:`GENEALOGY_POOL_MAX` most-recent
  rejects (the recombination-pool precedent) so the O(pool^2) dissimilarity
  scan stays cheap regardless of epoch length.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from zicato.selection.diversity import jaccard

#: Elo scale midpoint used to default-fill a candidate whose rating the fold
#: has not yet produced (a fresh reject, or an index-absent workspace). Only
#: ever a TIE-BREAK term in the greedy walk — it can reorder within a
#: dissimilarity tie, never manufacture an item (matching
#: :data:`zicato.epoch.recombine.DEFAULT_ELO`).
DEFAULT_ELO: float = 1500.0

#: Hard cap on the inspiration pool the sampler scans — the N most-recent
#: reign rejects. Bounds the O(N^2) greedy-dissimilarity walk to a small
#: constant regardless of epoch length (the ``RECOMBINE_POOL_MAX``
#: precedent).
GENEALOGY_POOL_MAX: int = 24

#: Cap on the PATCH-DIFF excerpt carried per item. The diff text is
#: proposer-authored (the ``new_content`` the model itself wrote), so it is
#: in-envelope — but it is still capped so one item cannot balloon the
#: prompt, and truncation is head-only with an elision marker.
_DIFF_EXCERPT_MAX = 200

#: Cap on the ``core_idea`` carried per item. Like the diff excerpt, the core
#: idea is proposer-authored (in-envelope) — but budget-capped with the same
#: head-only-plus-elision discipline so a pathologically long hypothesis line
#: cannot balloon the prompt (the process-exemplar cap style; PROPOSER.md §2.7).
_CORE_IDEA_MAX = 240

#: Size-band edges over the total patch-diff length (chars). A COARSE label —
#: the proposer reads "how big an edit was this" qualitatively, never the
#: exact byte count.
_SIZE_BAND_SMALL = 200
_SIZE_BAND_MEDIUM = 1000


@dataclass(frozen=True, slots=True)
class GenealogyRecord:
    """One reign candidate as plain data for the pure sampler.

    Assembled by the orchestrator's IO builder from the durable experiment
    records. Carries NO board-entry identity: the outcome is a
    WHOLE-CANDIDATE Δscalar (banded here, never rendered raw), and the patch
    payload is the proposer's own authored edit (targets, op kinds, and the
    diff text it wrote). ``patch_text`` is the RAW ``new_content`` — the
    sampler caps it to the rendered excerpt, so the capping is enforced (and
    tested) here rather than trusted to the caller.

    Fields
    ------
    generation_id / parent_generation_id:
        Lineage coordinates. ``parent_generation_id`` is the reign guard for
        inspirations (``== champion_id``), matching the recombination
        selector's #2 predicate.
    decision:
        The settled tournament decision — ``"promoted"`` feeds the parent
        spine, ``"rejected"`` feeds the inspiration pool; anything else
        (deferred / unsettled) is ignored.
    round_index:
        The 0-based evolve round that minted this generation — the spine's
        most-recent-first sort key.
    core_idea:
        The proposer-authored hypothesis core idea (in-envelope free text).
    patch_mutation_ids:
        The targeted mutation-id set — the greedy walk's dissimilarity axis.
    patch_op_kinds:
        The patches' op kinds (``replace`` / ``set_numeric`` / ``set_enum``).
    patch_text:
        The RAW concatenated patch-diff text (proposer-authored
        ``new_content``); capped to the rendered excerpt by the sampler.
    scalar_score_delta:
        The whole-candidate signed Δscalar (negative = better), or ``None``
        when unsettled. BANDED here — the exact number never escapes.
    is_placebo:
        ``True`` for a random-baseline calibration arm (never a genealogy
        item — it is not a real idea to build on or diverge from).
    """

    generation_id: str
    parent_generation_id: str | None
    decision: str
    round_index: int
    core_idea: str
    patch_mutation_ids: frozenset[str]
    patch_op_kinds: tuple[str, ...]
    patch_text: str
    scalar_score_delta: float | None
    is_placebo: bool = False


@dataclass(frozen=True, slots=True)
class PatchSummary:
    """The redacted, proposer-authored patch metadata for one item.

    ``mutation_ids`` + ``op_kinds`` are structural (code coordinates, not
    board identity); ``size_band`` is a COARSE magnitude label; ``diff_excerpt``
    is a capped, head-truncated slice of the proposer's own diff text. NOTHING
    board-derived rides here.
    """

    mutation_ids: tuple[str, ...]
    op_kinds: tuple[str, ...]
    size_band: str
    diff_excerpt: str


@dataclass(frozen=True, slots=True)
class GenealogyItem:
    """One rendered genealogy item — a parent or an inspiration.

    ``kind`` is ``"parent"`` (a promoted ancestor of the champion spine — build
    on it) or ``"inspiration"`` (a diverse rejected reign candidate — re-frame
    it). ``banded_outcome`` is the whole-candidate Δscalar through the
    experiment-memory band vocabulary (``improved`` / ``flat`` / ``regressed``,
    or ``""`` when unsettled). ``patch_summary`` and ``core_idea`` are
    proposer-authored (in-envelope); ``rationale`` is a static, harness-authored
    line explaining why the item was surfaced.
    """

    kind: str
    generation_id: str
    core_idea: str
    banded_outcome: str
    patch_summary: PatchSummary
    rationale: str


def _elo(record: GenealogyRecord, ratings: Mapping[str, float]) -> float:
    """The candidate's folded Elo, default-filled for the tie-break key."""
    value = ratings.get(record.generation_id)
    return float(value) if value is not None else DEFAULT_ELO


def _size_band(text: str) -> str:
    """Coarsen a patch-diff length to ``small`` / ``medium`` / ``large``."""
    n = len(text)
    if n < _SIZE_BAND_SMALL:
        return "small"
    if n < _SIZE_BAND_MEDIUM:
        return "medium"
    return "large"


def _diff_excerpt(text: str) -> str:
    """Cap the proposer's diff text to a single-line, head-truncated excerpt."""
    line = " ".join(text.strip().split())
    if len(line) <= _DIFF_EXCERPT_MAX:
        return line
    return line[: _DIFF_EXCERPT_MAX - 1].rstrip() + "…"


def _core_idea(text: str) -> str:
    """Normalize + cap the proposer's core idea (head-only, elided).

    Mirrors :func:`_diff_excerpt`: whitespace is collapsed to one line and the
    line is head-capped to :data:`_CORE_IDEA_MAX` with a trailing ellipsis, so
    an over-long hypothesis line cannot balloon the rendered block.
    """
    line = " ".join(text.strip().split())
    if len(line) <= _CORE_IDEA_MAX:
        return line
    return line[: _CORE_IDEA_MAX - 1].rstrip() + "…"


def _band_outcome(delta: float | None) -> str:
    """Band a whole-candidate Δscalar through the experiment-memory vocabulary.

    Reuses :func:`zicato.proposer.prompts._bucket_scalar_delta` (lazy import —
    the render module imports THIS one, so a top-level import would cycle) so
    the exact number never escapes and no new banding primitive is introduced.
    An unsettled candidate (``None``) renders no band.
    """
    if delta is None:
        return ""
    from zicato.proposer.prompts import _bucket_scalar_delta  # noqa: PLC0415

    return _bucket_scalar_delta(delta)


def _summarize_patch(record: GenealogyRecord) -> PatchSummary:
    """Reduce a record's raw patch payload to its redacted, capped summary."""
    return PatchSummary(
        mutation_ids=tuple(sorted(record.patch_mutation_ids)),
        op_kinds=tuple(sorted(set(record.patch_op_kinds))),
        size_band=_size_band(record.patch_text),
        diff_excerpt=_diff_excerpt(record.patch_text),
    )


def _make_item(record: GenealogyRecord, kind: str, rationale: str) -> GenealogyItem:
    return GenealogyItem(
        kind=kind,
        generation_id=record.generation_id,
        core_idea=_core_idea(record.core_idea),
        banded_outcome=_band_outcome(record.scalar_score_delta),
        patch_summary=_summarize_patch(record),
        rationale=rationale,
    )


def _champion_spine(
    promoted: Sequence[GenealogyRecord],
    champion_id: str,
) -> list[GenealogyRecord]:
    """Walk the ``parent_generation_id`` chain backward from ``champion_id``.

    Returns the champion's OWN promoted lineage — the champion record (when it
    is itself a promoted record in the pool) followed by its promoted ancestors,
    most-recent-first (the walk order). A promoted record NOT reachable on this
    chain (an off-spine promotion of a non-linear structure) is excluded by
    construction — matching the PROPOSER.md §2.7 contract ("the champion's own
    promoted patch history via the ``parent_generation_id`` chain").

    Pure + deterministic (a pointer walk has no choices to make). Terminates on
    a missing pointer (a gid absent from the promoted set — e.g. the seed ``v0``
    or the excluded reigning head) and on a cyclic pointer (a ``visited`` set),
    with a hop cap at the pool bound as a belt-and-suspenders backstop.
    """
    by_gid = {r.generation_id: r for r in promoted}
    spine: list[GenealogyRecord] = []
    visited: set[str] = set()
    cap = len(by_gid) + 1
    current: str | None = champion_id
    while current is not None and current not in visited and len(spine) <= cap:
        visited.add(current)
        record = by_gid.get(current)
        if record is None:
            break  # off-chain / missing pointer — the spine ends here
        spine.append(record)
        current = record.parent_generation_id
    return spine


def _greedy_dissimilar(
    pool: list[GenealogyRecord],
    ratings: Mapping[str, float],
    n: int,
) -> list[GenealogyRecord]:
    """Greedy max--min-Jaccard diversity walk — deterministic, order-independent.

    Farthest-point sampling over mutation-id-set distance (``1 - jaccard``):
    the seed is the best tie-break candidate (Elo down, then gid ascending),
    then each pick MAXIMIZES its minimum distance to the already-chosen set,
    ties broken by the same total key. Because the key is total (distances are
    a deterministic function of the chosen set; Elo + gid are unique
    backstops), the selection is reproducible for any fixed pool in ANY input
    order — the shuffled-pool order-independence pin.
    """
    if n <= 0 or not pool:
        return []

    # A deterministic starting order so ``seed`` and every scan are stable.
    remaining = sorted(pool, key=lambda r: (-_elo(r, ratings), r.generation_id))
    selected: list[GenealogyRecord] = [remaining.pop(0)]
    while remaining and len(selected) < n:
        best: GenealogyRecord | None = None
        best_key: tuple[float, float, str] | None = None
        for candidate in remaining:
            min_distance = min(
                1.0 - jaccard(candidate.patch_mutation_ids, s.patch_mutation_ids) for s in selected
            )
            # -min_distance: MAXIMIZE the distance (farthest point). Then the
            # total tie-break: -Elo (higher rating first), then gid ascending.
            key = (-min_distance, -_elo(candidate, ratings), candidate.generation_id)
            if best_key is None or key < best_key:
                best_key = key
                best = candidate
        assert best is not None  # the loop guarantees a pick while remaining
        selected.append(best)
        remaining.remove(best)
    return selected


def sample_genealogy(
    records: Sequence[GenealogyRecord],
    ratings: Mapping[str, float],
    k: int,
    *,
    champion_id: str | None = None,
) -> tuple[GenealogyItem, ...]:
    """Sample ``≤ k`` genealogy items — parents (spine) + inspirations (diverse).

    Deterministic and IO-free. Partitions the reign records:

    * **Parents** — the champion's OWN promoted spine, built by walking the
      ``parent_generation_id`` chain backward from ``champion_id`` through the
      promoted records (:func:`_champion_spine`), most-recent-first (the walk
      order), taking the first ``k // 2``. An off-spine promotion — a promoted
      record NOT on the champion's chain — is excluded by construction. When
      ``champion_id`` is ``None`` (no anchor to walk from), the spine falls
      back to the promoted records sorted most-recent-first by ``round_index``
      (gid backstop). When the spine is shorter than the budget, the unused
      slots backfill into inspirations.
    * **Inspirations** — REJECTED records (reign-scoped to ``champion_id`` when
      given — the recombination #2 reign guard), capped at the
      :data:`GENEALOGY_POOL_MAX` most-recent, then the greedy
      max--min-Jaccard diversity walk (:func:`_greedy_dissimilar`) for the
      remaining budget.

    Placebo arms are excluded from both. Returns ``()`` at ``k <= 0`` or when
    no record qualifies — the caller's "omit this section" sentinel.
    """
    if k <= 0 or not records:
        return ()

    live = [r for r in records if not r.is_placebo]

    promoted = [r for r in live if r.decision == "promoted"]
    if champion_id is not None:
        # Walk the champion's own promoted lineage by pointer — off-spine
        # promotions never surface as parents (the PROPOSER.md §2.7 contract).
        spine = _champion_spine(promoted, champion_id)
    else:
        # No anchor to walk from: most-recent-first over all promoted records.
        spine = sorted(promoted, key=lambda r: (-r.round_index, r.generation_id))
    n_parents = k // 2
    parents = spine[:n_parents]

    parent_ids = {r.generation_id for r in parents}
    rejected = [
        r
        for r in live
        if r.decision == "rejected"
        and r.generation_id not in parent_ids
        and (champion_id is None or r.parent_generation_id == champion_id)
    ]
    # Cap to the most-recent rejects before the O(pool^2) walk (the
    # recombination-pool precedent). Most-recent = highest round_index.
    rejected.sort(key=lambda r: (-r.round_index, r.generation_id))
    pool = rejected[:GENEALOGY_POOL_MAX]

    n_inspirations = k - len(parents)
    inspirations = _greedy_dissimilar(pool, ratings, n_inspirations)

    items: list[GenealogyItem] = []
    for record in parents:
        items.append(
            _make_item(
                record,
                "parent",
                "promoted ancestor on the champion spine — build on this direction",
            )
        )
    for record in inspirations:
        items.append(
            _make_item(
                record,
                "inspiration",
                "diverse rejected idea — a different framing may clear the gate",
            )
        )
    return tuple(items)


__all__ = [
    "DEFAULT_ELO",
    "GENEALOGY_POOL_MAX",
    "GenealogyItem",
    "GenealogyRecord",
    "PatchSummary",
    "sample_genealogy",
]
