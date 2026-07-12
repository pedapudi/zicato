"""The genealogy channel (WS-GENE): sampler + redaction + render + A/B.

Everything here is a pure function of constructed inputs — no fixtures, no
IO — mirroring the recombination-engine tests. Four groups:

* the deterministic sampler: the greedy max--min-Jaccard diversity walk's
  known-answer, order-independence over a shuffled pool, and the
  parent/inspiration partition + budget split;
* REDACTION (adversarial): a record whose outcome + patch text carry
  distinctive numbers and long identity-laden content — the exact Δscalar
  never escapes (only a band), the diff excerpt is capped, and the item /
  record types structurally cannot carry a board-entry id or a per-entry
  result;
* the prompt-render golden: byte-identical at ``genealogy = ()`` and the
  section's placement when present;
* a seeded A/B power measurement (genealogy on vs a recency baseline) —
  MEASURED + PRINTED, a no-regression assert only.
"""

from __future__ import annotations

import random
import re
from dataclasses import fields
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.proposer.genealogy import (
    _CORE_IDEA_MAX,
    _DIFF_EXCERPT_MAX,
    GenealogyItem,
    GenealogyRecord,
    sample_genealogy,
)
from zicato.proposer.prompts import render_genealogy_block, render_user_prompt

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _record(
    gid: str,
    *,
    decision: str = "rejected",
    parent: str | None = "v0",
    round_index: int = 1,
    mutation_ids: tuple[str, ...] = ("m1",),
    core_idea: str = "",
    ops: tuple[str, ...] = ("replace",),
    patch_text: str = "edit body",
    delta: float | None = -0.5,
    placebo: bool = False,
) -> GenealogyRecord:
    return GenealogyRecord(
        generation_id=gid,
        parent_generation_id=parent,
        decision=decision,
        round_index=round_index,
        core_idea=core_idea or f"idea of {gid}",
        patch_mutation_ids=frozenset(mutation_ids),
        patch_op_kinds=ops,
        patch_text=patch_text,
        scalar_score_delta=delta,
        is_placebo=placebo,
    )


# ---------------------------------------------------------------------------
# The deterministic sampler
# ---------------------------------------------------------------------------


def test_greedy_dissimilarity_known_answer() -> None:
    """Farthest-point over mutation-id sets: the diverse pick is unambiguous.

    Pool (all rejected, champion=None ⇒ all inspirations): A={m1,m2},
    B={m1,m2} (identical to A), C={m3,m4} (disjoint), D={m1,m5} (partial).
    A has the highest Elo, so it seeds; the farthest from A is C (distance
    1.0) over D (~0.67) and B (0.0). With k=2 and no promoted spine, the
    walk returns exactly [A, C].
    """
    pool = [
        _record("A", mutation_ids=("m1", "m2")),
        _record("B", mutation_ids=("m1", "m2")),
        _record("C", mutation_ids=("m3", "m4")),
        _record("D", mutation_ids=("m1", "m5")),
    ]
    ratings = {"A": 1600.0, "B": 1500.0, "C": 1500.0, "D": 1500.0}
    items = sample_genealogy(pool, ratings, 2)
    assert [it.generation_id for it in items] == ["A", "C"]
    assert all(it.kind == "inspiration" for it in items)


def test_sampler_is_order_independent_over_a_shuffled_pool() -> None:
    """The selection is reproducible for any input order — the shuffle pin."""
    pool = [
        _record("A", mutation_ids=("m1", "m2")),
        _record("B", mutation_ids=("m1", "m2")),
        _record("C", mutation_ids=("m3", "m4")),
        _record("D", mutation_ids=("m1", "m5")),
        _record("E", mutation_ids=("m6",)),
    ]
    ratings = {"A": 1600.0}
    canonical = [it.generation_id for it in sample_genealogy(pool, ratings, 3)]
    rng = random.Random(1234)
    for _ in range(40):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        got = [it.generation_id for it in sample_genealogy(shuffled, ratings, 3)]
        assert got == canonical


def test_dissimilarity_tie_breaks_by_elo_then_gid() -> None:
    """Equal min-distance candidates break by Elo down, then gid ascending."""
    # Seed = the highest-Elo record. Two equally-distant follow-ups: the
    # higher Elo wins; with equal Elo the lex-smaller gid wins.
    pool = [
        _record("seed", mutation_ids=("m1",)),
        _record("hi", mutation_ids=("m2",)),
        _record("lo", mutation_ids=("m3",)),
    ]
    # seed seeds (highest Elo); hi and lo are both distance 1.0 from seed.
    got_elo = sample_genealogy(pool, {"seed": 1700, "hi": 1600, "lo": 1500}, 2)
    assert [it.generation_id for it in got_elo] == ["seed", "hi"]
    # With hi/lo tied on Elo, the gid backstop picks "aaa" before "zzz".
    pool2 = [
        _record("seed", mutation_ids=("m1",)),
        _record("zzz", mutation_ids=("m2",)),
        _record("aaa", mutation_ids=("m3",)),
    ]
    got_gid = sample_genealogy(pool2, {"seed": 1700}, 2)
    assert [it.generation_id for it in got_gid] == ["seed", "aaa"]


def test_parents_are_the_promoted_spine_most_recent_first() -> None:
    """No anchor (champion_id=None) ⇒ promoted records sort most-recent-first."""
    records = [
        _record("v1", decision="promoted", round_index=1),
        _record("v2", decision="promoted", round_index=2),
        _record("v3", decision="promoted", round_index=3),
    ]
    # k=4 ⇒ n_parents = 2 (k//2); the two most-recent promoted, v3 then v2.
    items = sample_genealogy(records, {}, 4)
    parents = [it for it in items if it.kind == "parent"]
    assert [it.generation_id for it in parents] == ["v3", "v2"]


def test_parents_walk_the_champion_pointer_chain() -> None:
    """With an anchor, parents are the champion's OWN promoted lineage.

    Chain: champ ← p1 ← p2 (champ's real ancestors). The walk from ``champ``
    follows ``parent_generation_id`` backward, so the spine is exactly the
    champion's line, most-recent-first — regardless of the promoted records'
    round order.
    """
    records = [
        _record("champ", decision="promoted", parent="p1", round_index=5),
        _record("p1", decision="promoted", parent="p2", round_index=3),
        _record("p2", decision="promoted", parent="v0", round_index=1),
    ]
    # k=6 ⇒ n_parents=3; the full chain, champ first (the walk order).
    items = sample_genealogy(records, {}, 6, champion_id="champ")
    parents = [it.generation_id for it in items if it.kind == "parent"]
    assert parents == ["champ", "p1", "p2"]


def test_off_spine_promoted_record_is_excluded() -> None:
    """A promoted record NOT on the champion's chain never surfaces as a parent.

    ``off`` is promoted with the FRESHEST round, so a naive most-recent sort
    would surface it — but its parent pointer (``side``) is off the champion's
    chain, so the pointer walk excludes it. This is the non-linear-structure
    guard the ``parent_generation_id`` chain contract promises.
    """
    records = [
        _record("champ", decision="promoted", parent="p1", round_index=5),
        _record("p1", decision="promoted", parent="v0", round_index=2),
        # Off-spine: promoted, freshest round, but parented off the champ chain.
        _record("off", decision="promoted", parent="side", round_index=9),
    ]
    items = sample_genealogy(records, {}, 6, champion_id="champ")
    parents = [it.generation_id for it in items if it.kind == "parent"]
    assert parents == ["champ", "p1"]
    assert "off" not in parents


def test_spine_walk_terminates_on_a_cyclic_pointer() -> None:
    """A pointer cycle among promoted records cannot spin the walk forever."""
    records = [
        _record("a", decision="promoted", parent="b", round_index=2),
        _record("b", decision="promoted", parent="a", round_index=1),
    ]
    items = sample_genealogy(records, {}, 4, champion_id="a")
    parents = [it.generation_id for it in items if it.kind == "parent"]
    # Each gid is visited at most once — a then b, then the pointer revisits a
    # (already visited) and the walk stops.
    assert parents == ["a", "b"]


def test_budget_split_and_backfill() -> None:
    """k // 2 parents; inspirations take the rest, backfilling a short spine."""
    records = [
        _record("v1", decision="promoted", round_index=1),
        _record("r1", decision="rejected", mutation_ids=("m1",), round_index=2),
        _record("r2", decision="rejected", mutation_ids=("m2",), round_index=3),
        _record("r3", decision="rejected", mutation_ids=("m3",), round_index=4),
    ]
    # k=4 ⇒ n_parents=2 requested but only ONE promoted exists ⇒ parents=[v1];
    # inspirations backfill to k - 1 = 3 of the rejects.
    items = sample_genealogy(records, {}, 4)
    assert sum(1 for it in items if it.kind == "parent") == 1
    assert sum(1 for it in items if it.kind == "inspiration") == 3
    assert len(items) == 4


def test_reign_scoping_and_placebo_exclusion() -> None:
    """Inspirations are reign-scoped to champion_id; placebo arms never appear."""
    records = [
        _record("in_reign", decision="rejected", parent="champ", mutation_ids=("m1",)),
        _record("stale", decision="rejected", parent="old_champ", mutation_ids=("m2",)),
        _record("placebo", decision="rejected", parent="champ", placebo=True),
    ]
    items = sample_genealogy(records, {}, 4, champion_id="champ")
    ids = {it.generation_id for it in items}
    assert "in_reign" in ids
    assert "stale" not in ids  # different reign
    assert "placebo" not in ids  # calibration arm


def test_k_zero_and_empty_return_nothing() -> None:
    assert sample_genealogy([_record("A")], {}, 0) == ()
    assert sample_genealogy([], {}, 4) == ()


def test_sampler_is_deterministic_run_to_run() -> None:
    records = [
        _record("v2", decision="promoted", round_index=2),
        _record("r1", decision="rejected", mutation_ids=("m1", "m2"), round_index=3),
        _record("r2", decision="rejected", mutation_ids=("m3",), round_index=4),
    ]
    first = sample_genealogy(records, {"r1": 1550}, 4, champion_id="v0")
    second = sample_genealogy(records, {"r1": 1550}, 4, champion_id="v0")
    assert first == second


# ---------------------------------------------------------------------------
# REDACTION (adversarial) — the outcome + patch text can never leak
# ---------------------------------------------------------------------------

#: A distinctive Δscalar that must NEVER render verbatim — only its band.
_ADVERSARIAL_DELTA = -0.1234567
#: A long, identity-laden diff body the excerpt must cap.
_LONG_DIFF = "LEAK_TOKEN_" + ("x" * 5000) + "_TAIL_MARKER"


def test_exact_delta_never_escapes_only_the_band() -> None:
    """The exact Δscalar is coarsened to a band — no raw number reaches output."""
    rec = _record("v7", delta=_ADVERSARIAL_DELTA, core_idea="fix the router")
    items = sample_genealogy([rec], {}, 2, champion_id="v0")
    block = render_genealogy_block(items)
    # The band is present; the exact number (either sign) is absent.
    assert "improved" in block
    assert "0.1234567" not in block
    assert "-0.1234567" not in block
    # No fine-grained decimal survives in the banded outcome at all.
    assert "Δscalar=improved" in block
    # And the item's banded_outcome is exactly one of the three bands.
    assert items[0].banded_outcome in {"improved", "flat", "regressed"}


def test_flat_and_regressed_bands_render() -> None:
    flat = sample_genealogy([_record("f", delta=0.0)], {}, 2, champion_id="v0")
    reg = sample_genealogy([_record("g", delta=0.9)], {}, 2, champion_id="v0")
    assert flat[0].banded_outcome == "flat"
    assert reg[0].banded_outcome == "regressed"


def test_unsettled_delta_renders_no_band() -> None:
    item = sample_genealogy([_record("h", delta=None)], {}, 2, champion_id="v0")[0]
    assert item.banded_outcome == ""
    assert "Δscalar" not in render_genealogy_block([item])


def test_diff_excerpt_is_capped() -> None:
    """A pathologically long diff is capped to the excerpt budget with elision."""
    rec = _record("v7", patch_text=_LONG_DIFF)
    item = sample_genealogy([rec], {}, 2, champion_id="v0")[0]
    excerpt = item.patch_summary.diff_excerpt
    assert len(excerpt) <= _DIFF_EXCERPT_MAX
    assert excerpt.endswith("…")
    # The full body never survives; the tail marker beyond the cap is gone.
    block = render_genealogy_block([item])
    assert "_TAIL_MARKER" not in block
    assert len(_LONG_DIFF) > _DIFF_EXCERPT_MAX  # sanity: the fixture is long


def test_core_idea_is_capped() -> None:
    """A pathologically long core idea is head-capped with an elision marker."""
    long_idea = "IDEA_HEAD " + ("y" * 5000) + " IDEA_TAIL"
    item = sample_genealogy([_record("v7", core_idea=long_idea)], {}, 2, champion_id="v0")[0]
    assert len(item.core_idea) <= _CORE_IDEA_MAX
    assert item.core_idea.endswith("…")
    assert item.core_idea.startswith("IDEA_HEAD")
    # The tail beyond the cap never survives into the rendered block.
    block = render_genealogy_block([item])
    assert "IDEA_TAIL" not in block
    assert len(long_idea) > _CORE_IDEA_MAX  # sanity: the fixture is long


def test_no_fine_grained_decimal_leaks_from_the_outcome() -> None:
    """A blanket scan: the rendered OUTCOME carries no multi-digit decimal.

    The proposer's own core idea / diff text may legitimately carry numbers
    (they are proposer-authored, in-envelope), so the scan targets the head
    line where the banded outcome lives — never a raw response-surface value.
    """
    rec = _record("v7", delta=_ADVERSARIAL_DELTA, core_idea="plain idea", patch_text="short")
    block = render_genealogy_block(sample_genealogy([rec], {}, 2, champion_id="v0"))
    head = block.splitlines()[1]  # the entry head line under the section banner
    assert not re.search(r"-?\d+\.\d{2,}", head), head


def test_record_and_item_types_carry_no_per_entry_field() -> None:
    """STRUCTURAL envelope proof: neither type has an entry-id / per-entry slot."""
    record_fields = {f.name for f in fields(GenealogyRecord)}
    item_fields = {f.name for f in fields(GenealogyItem)}
    forbidden = {"entry_id", "entry_ids", "per_entry", "improved_entry_ids", "holdout"}
    assert record_fields.isdisjoint(forbidden)
    assert item_fields.isdisjoint(forbidden)
    # The only numeric outcome the item exposes is the banded string.
    assert "banded_outcome" in item_fields
    assert "scalar_score_delta" not in item_fields


# ---------------------------------------------------------------------------
# The prompt-render golden
# ---------------------------------------------------------------------------


def _mutation() -> MutationPoint:
    return MutationPoint(
        id="m1",
        kind="span",
        file=Path("/abs/x.py"),
        source_root=Path("/abs"),
        line_start=1,
        line_end=2,
        content="body",
        content_hash="h",
        metadata={},
    )


def test_genealogy_default_is_byte_identical() -> None:
    """A ``genealogy = ()`` round renders the exact prompt of before the surface."""
    baseline = render_user_prompt(current_loss_summary="loss", patterns=[], mutations=[_mutation()])
    with_default = render_user_prompt(
        current_loss_summary="loss", patterns=[], mutations=[_mutation()], genealogy=()
    )
    assert with_default == baseline
    assert "## Candidate genealogy" not in with_default


def test_genealogy_section_renders_when_present() -> None:
    records = [
        _record("v2", decision="promoted", round_index=2, core_idea="promoted ancestor idea"),
        _record(
            "r1",
            decision="rejected",
            parent="v2",
            mutation_ids=("m9",),
            core_idea="a rejected idea",
        ),
    ]
    # champion_id="v2": the walk anchors on the promoted spine head (v2), and
    # r1 is reign-scoped to it (parent == champion).
    items = sample_genealogy(records, {}, 4, champion_id="v2")
    rendered = render_user_prompt(
        current_loss_summary="loss",
        patterns=[],
        mutations=[_mutation()],
        genealogy=items,
    )
    assert "## Candidate genealogy" in rendered
    assert "Champion lineage" in rendered
    assert "promoted ancestor idea" in rendered
    assert "Rejected candidates worth re-framing" in rendered
    assert "a rejected idea" in rendered


def test_genealogy_lands_above_experiment_memory() -> None:
    """Placement: genealogy sits directly above ``## What's already been tried``."""
    from zicato.core.types import PriorExperiment

    prior = (
        PriorExperiment(
            generation_id="v1",
            epoch_id="e",
            core_idea="tried",
            modulating=("m1",),
            decision="rejected",
            rejection_reason="",
            scalar_score_delta=None,
        ),
    )
    items = sample_genealogy([_record("v2", decision="promoted", round_index=2)], {}, 2)
    rendered = render_user_prompt(
        current_loss_summary="loss",
        patterns=[],
        mutations=[_mutation()],
        prior_experiments=prior,
        genealogy=items,
    )
    gen_at = rendered.index("## Candidate genealogy")
    mem_at = rendered.index("## What's already been tried")
    assert gen_at < mem_at


# ---------------------------------------------------------------------------
# A/B power measurement — MEASURED + PRINTED, no-regression assert only
# ---------------------------------------------------------------------------

_AB_TRIALS = 300
_AB_POOL = 12
_AB_K = 4


def _complementary_pair_present(mutation_sets: list[frozenset[str]]) -> bool:
    """A merge is possible when two surfaced sets are disjoint + both non-empty.

    The proposal-validity PROXY: the in-context proposer can compose a
    complementary union only if the genealogy it was shown contains two
    ideas that touch nothing in common (the recombination disjointness
    condition). A more diverse surfaced set contains such a pair more often.
    """
    n = len(mutation_sets)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = mutation_sets[i], mutation_sets[j]
            if a and b and not (a & b):
                return True
    return False


def test_ab_genealogy_surfaces_mergeable_material_no_worse_than_recency() -> None:
    """Seeded A/B: greedy diversity surfaces a mergeable pair >= a recency baseline.

    A = the genealogy sampler (greedy max--min-Jaccard). B = a recency
    baseline (the k most-recent rejects). Over seeded synthetic reigns we
    measure P(a complementary pair is surfaced) — the proposal-validity
    proxy — and PRINT both rates. Assertion: A is NEVER worse than B (the
    channel cannot reduce the mergeable material the proposer sees). The
    magnitude is documentation, not a gate.
    """
    universe = [f"m{i}" for i in range(8)]
    hits_gene = 0
    hits_recency = 0
    for trial in range(_AB_TRIALS):
        rng = random.Random(9000 + trial)
        records: list[GenealogyRecord] = []
        for idx in range(_AB_POOL):
            size = rng.randint(1, 3)
            ids = tuple(rng.sample(universe, size))
            records.append(
                _record(
                    f"r{idx:02d}",
                    decision="rejected",
                    parent="champ",
                    round_index=idx,
                    mutation_ids=ids,
                )
            )
        ratings = {r.generation_id: 1500.0 + rng.random() for r in records}

        # A — the sampler's greedy-diverse inspirations.
        gene_items = sample_genealogy(records, ratings, _AB_K, champion_id="champ")
        gene_sets = [it.patch_summary.mutation_ids for it in gene_items]
        gene_sets_fs = [frozenset(s) for s in gene_sets]

        # B — the recency baseline: the k most-recent rejects (highest round).
        recent = sorted(records, key=lambda r: -r.round_index)[:_AB_K]
        recency_sets = [r.patch_mutation_ids for r in recent]

        hits_gene += _complementary_pair_present(gene_sets_fs)
        hits_recency += _complementary_pair_present(recency_sets)

    p_gene = hits_gene / _AB_TRIALS
    p_recency = hits_recency / _AB_TRIALS
    print(
        f"\n[genealogy A/B] trials={_AB_TRIALS} pool={_AB_POOL} k={_AB_K} "
        f"P(mergeable | genealogy)={p_gene:.3f} "
        f"P(mergeable | recency)={p_recency:.3f} "
        f"delta={p_gene - p_recency:+.3f}"
    )
    # No-regression only: the diversity walk never surfaces LESS mergeable
    # material than plain recency. (In practice it surfaces strictly more.)
    assert p_gene >= p_recency
