"""Known-answer tests for the pure recombination selector + minter (WS-REC).

One fires/does-not-fire pair per eligibility predicate, the 4-key ranking
known-answers, determinism + shuffled-pool order-independence, and the
pure minter's byte-stable composition rules. Everything here is a pure
function of constructed inputs — no fixtures, no IO.
"""

from __future__ import annotations

import random
from dataclasses import replace

from zicato.core.types import ExpectedDriftMovement, ExpectedMetricMovement, Patch
from zicato.epoch.recombine import (
    DEFAULT_ELO,
    RECOMBINE_POOL_MAX,
    ParentCandidate,
    eligible_parents,
    rank_pairs,
)
from zicato.proposer.recombine import (
    RECOMBINED_HYPOTHESIS_MARKER,
    RecombinationPair,
    mint_recombined_experiment,
)

CHAMPION = "v4"
MANIFEST = frozenset({"m1", "m2", "m3", "m4"})


def _patch(pid: str, mutation_id: str, content: str = "x") -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op="replace",
        new_content=content,
        new_numeric=None,
        new_enum=None,
        rationale=f"edit {mutation_id}",
    )


def _candidate(
    gid: str,
    *,
    decision: str = "rejected",
    parent: str | None = CHAMPION,
    placebo: bool = False,
    recombined: bool = False,
    mutation_ids: tuple[str, ...] = ("m1",),
    improved: tuple[str, ...] = ("e1",),
    regressed: tuple[str, ...] = (),
    elo: float | None = None,
) -> ParentCandidate:
    return ParentCandidate(
        generation_id=gid,
        decision=decision,
        parent_generation_id=parent,
        is_placebo=placebo,
        is_recombined=recombined,
        patch_mutation_ids=frozenset(mutation_ids),
        improved_entry_ids=frozenset(improved),
        regressed_entry_ids=frozenset(regressed),
        elo=elo,
        patches=tuple(_patch(f"{gid}-{m}", m) for m in mutation_ids),
        core_idea=f"idea of {gid}",
    )


# ---------------------------------------------------------------------------
# Per-candidate predicates (#1–#4, #6) — one fires / does-not pair each.
# ---------------------------------------------------------------------------


def test_predicate_1_rejected_only() -> None:
    """#1: only a settled REJECT is recyclable — deferred/promoted are not."""
    rejected = _candidate("v1")
    deferred = _candidate("v2", decision="deferred")
    promoted = _candidate("v3", decision="promoted")
    kept = eligible_parents(
        [rejected, deferred, promoted], champion_id=CHAMPION, manifest_ids=MANIFEST
    )
    assert [c.generation_id for c in kept] == ["v1"]


def test_predicate_2_current_reign_only() -> None:
    """#2: parent pointer == round-start champion; stale-reign rejects drop."""
    current = _candidate("v1", parent=CHAMPION)
    stale = _candidate("v2", parent="v0")
    seedish = _candidate("v3", parent=None)
    kept = eligible_parents([current, stale, seedish], champion_id=CHAMPION, manifest_ids=MANIFEST)
    assert [c.generation_id for c in kept] == ["v1"]


def test_predicate_3_non_placebo() -> None:
    """#3: a random-baseline calibration arm is never merge material."""
    real = _candidate("v1")
    placebo = _candidate("v2", placebo=True)
    kept = eligible_parents([real, placebo], champion_id=CHAMPION, manifest_ids=MANIFEST)
    assert [c.generation_id for c in kept] == ["v1"]


def test_predicate_4_non_recombined_parent() -> None:
    """#4: no chains in v1 — a prior mint is not itself a parent."""
    plain = _candidate("v1")
    prior_mint = _candidate("v2", recombined=True)
    kept = eligible_parents([plain, prior_mint], champion_id=CHAMPION, manifest_ids=MANIFEST)
    assert [c.generation_id for c in kept] == ["v1"]


def test_predicate_6_manifest_membership_and_nonempty_patches() -> None:
    """#6: every PATCH mutation-id must be in the current manifest, and a
    patch-free candidate has nothing to contribute."""
    valid = _candidate("v1", mutation_ids=("m1", "m2"))
    vanished = _candidate("v2", mutation_ids=("m1", "gone"))
    empty = _candidate("v3", mutation_ids=())
    kept = eligible_parents([valid, vanished, empty], champion_id=CHAMPION, manifest_ids=MANIFEST)
    assert [c.generation_id for c in kept] == ["v1"]


# ---------------------------------------------------------------------------
# Pair predicates (#5, #7, #8).
# ---------------------------------------------------------------------------


def test_predicate_7_disjointness_rejects_overlapping_pair() -> None:
    """#7 fires: any shared PATCH mutation-id (jaccard > 0) kills the pair.

    REQUIRED, not preferred — the applier is last-wins on a duplicate
    target, so an overlapping union would silently drop one parent's edit.
    """
    a = _candidate("v1", mutation_ids=("m1", "m2"), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2", "m3"), improved=("e2",))
    assert rank_pairs([a, b]) is None


def test_predicate_7_disjointness_passes_disjoint_pair() -> None:
    """#7 does not fire: fully disjoint targets pair up."""
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",))
    pair = rank_pairs([a, b])
    assert pair is not None
    assert (pair[0].generation_id, pair[1].generation_id) == ("v1", "v2")


def test_predicate_8_complementarity_requires_distinct_wins() -> None:
    """#8: both improved sets non-empty AND neither ⊆ the other."""
    base = _candidate("v1", mutation_ids=("m1",), improved=("e1", "e2"))
    # A subset improver adds nothing a single winner would not.
    subset = _candidate("v2", mutation_ids=("m2",), improved=("e1",))
    assert rank_pairs([base, subset]) is None
    # An empty improver has no win to contribute.
    empty = _candidate("v3", mutation_ids=("m3",), improved=())
    assert rank_pairs([base, empty]) is None
    # Identical sets are mutual subsets — not complementary.
    identical = _candidate("v4x", mutation_ids=("m4",), improved=("e1", "e2"))
    assert rank_pairs([base, identical]) is None
    # Overlapping-but-distinct sets ARE complementary.
    distinct = _candidate("v5", mutation_ids=("m4",), improved=("e2", "e3"))
    assert rank_pairs([base, distinct]) is not None


def test_predicate_5_tried_pair_dedup() -> None:
    """#5: a persisted pair never re-mints; an untried pairing still can."""
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",))
    c = _candidate("v3", mutation_ids=("m3",), improved=("e3",))
    tried = frozenset({frozenset({"v1", "v2"})})
    pair = rank_pairs([a, b, c], tried_pairs=tried)
    assert pair is not None
    assert frozenset({pair[0].generation_id, pair[1].generation_id}) != frozenset({"v1", "v2"})
    # With every pairing tried, nothing mints.
    all_tried = frozenset(
        {frozenset({"v1", "v2"}), frozenset({"v1", "v3"}), frozenset({"v2", "v3"})}
    )
    assert rank_pairs([a, b, c], tried_pairs=all_tried) is None


# ---------------------------------------------------------------------------
# Ranking keys — each level only breaks the previous level's ties.
# ---------------------------------------------------------------------------


def test_ranking_key_1_combined_coverage_wins() -> None:
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",))
    wide = _candidate("v3", mutation_ids=("m3",), improved=("e3", "e4", "e5"))
    pair = rank_pairs([a, b, wide])
    assert pair is not None
    # The widest union (1 + 3 = 4 distinct entries) beats the 2-entry pairs.
    assert {pair[0].generation_id, pair[1].generation_id} & {"v3"}


def test_ranking_key_2_cross_regression_breaks_coverage_ties() -> None:
    """Cross-regression is a RANKING penalty, not a filter: a risky pair
    still mints when it is the only pair, but a same-coverage safer pair
    wins."""
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",), regressed=("e9",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",), regressed=())
    c = _candidate("v3", mutation_ids=("m3",), improved=("e2",), regressed=("e8",))
    # (v1,v2) coverage 2 regression 1; (v1,v3) coverage 2 regression 2;
    # (v2,v3) not complementary (identical improved sets).
    pair = rank_pairs([a, b, c])
    assert pair is not None
    assert (pair[0].generation_id, pair[1].generation_id) == ("v1", "v2")
    # NOT a filter: alone, the riskier pair still mints.
    risky_only = rank_pairs([a, c])
    assert risky_only is not None
    assert (risky_only[0].generation_id, risky_only[1].generation_id) == ("v1", "v3")


def test_ranking_key_3_summed_elo_breaks_evidence_ties() -> None:
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",), elo=1600.0)
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",), elo=1600.0)
    c = _candidate("v3", mutation_ids=("m3",), improved=("e3",), elo=1400.0)
    # All three pairings tie on coverage (2) and regression (0); the two
    # 1600s sum highest.
    pair = rank_pairs([a, b, c])
    assert pair is not None
    assert (pair[0].generation_id, pair[1].generation_id) == ("v1", "v2")


def test_ranking_key_3_default_fill_is_1500() -> None:
    """An unrated candidate fills at DEFAULT_ELO — it can lose to a rated
    1600 pair and beat a rated 1400 pair, never being excluded outright."""
    assert DEFAULT_ELO == 1500.0
    unrated = _candidate("v1", mutation_ids=("m1",), improved=("e1",), elo=None)
    high = _candidate("v2", mutation_ids=("m2",), improved=("e2",), elo=1600.0)
    low = _candidate("v3", mutation_ids=("m3",), improved=("e3",), elo=1400.0)
    pair = rank_pairs([unrated, high, low])
    assert pair is not None
    # (unrated 1500 + high 1600) = 3100 beats (unrated+low)=2900 and
    # (high+low)=3000.
    assert (pair[0].generation_id, pair[1].generation_id) == ("v1", "v2")


def test_ranking_key_4_lexicographic_backstop() -> None:
    """A full evidence + Elo tie resolves by ascending gid — total order."""
    a = _candidate("v1", mutation_ids=("m1",), improved=("e1",))
    b = _candidate("v2", mutation_ids=("m2",), improved=("e2",))
    c = _candidate("v3", mutation_ids=("m3",), improved=("e3",))
    pair = rank_pairs([a, b, c])
    assert pair is not None
    assert (pair[0].generation_id, pair[1].generation_id) == ("v1", "v2")


# ---------------------------------------------------------------------------
# Determinism + order independence.
# ---------------------------------------------------------------------------


def test_same_inputs_twice_yield_identical_pair() -> None:
    pool = [
        _candidate("v1", mutation_ids=("m1",), improved=("e1", "e2"), elo=1520.0),
        _candidate("v2", mutation_ids=("m2",), improved=("e3",), regressed=("e5",)),
        _candidate("v3", mutation_ids=("m3",), improved=("e2", "e4")),
    ]
    first = rank_pairs(eligible_parents(pool, champion_id=CHAMPION, manifest_ids=MANIFEST))
    second = rank_pairs(eligible_parents(pool, champion_id=CHAMPION, manifest_ids=MANIFEST))
    assert first is not None and second is not None
    assert (first[0].generation_id, first[1].generation_id) == (
        second[0].generation_id,
        second[1].generation_id,
    )


def test_shuffled_pool_order_independence() -> None:
    """The selection is a function of the SET, not the input order."""
    pool = [
        _candidate("v1", mutation_ids=("m1",), improved=("e1", "e2")),
        _candidate("v2", mutation_ids=("m2",), improved=("e3",)),
        _candidate("v3", mutation_ids=("m3",), improved=("e2", "e4"), regressed=("e9",)),
        _candidate("v5", mutation_ids=("m4",), improved=("e5",), elo=1610.0),
    ]
    baseline = rank_pairs(eligible_parents(pool, champion_id=CHAMPION, manifest_ids=MANIFEST))
    assert baseline is not None
    baseline_ids = (baseline[0].generation_id, baseline[1].generation_id)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = list(pool)
        rng.shuffle(shuffled)
        pair = rank_pairs(eligible_parents(shuffled, champion_id=CHAMPION, manifest_ids=MANIFEST))
        assert pair is not None
        assert (pair[0].generation_id, pair[1].generation_id) == baseline_ids


def test_pool_cap_constant_is_pinned() -> None:
    assert RECOMBINE_POOL_MAX == 16


# ---------------------------------------------------------------------------
# The pure minter.
# ---------------------------------------------------------------------------


def _mk_pair(**overrides: object) -> RecombinationPair:
    base = dict(
        a_generation_id="v1",
        b_generation_id="v2",
        a_patches=(_patch("pa", "m1", "content-a"),),
        b_patches=(_patch("pb", "m2", "content-b"),),
        a_core_idea="Fix the summary line.",
        b_core_idea="Fix the citations.",
        a_improved_count=1,
        b_improved_count=1,
        combined_improved_count=2,
        combined_regressed_count=0,
    )
    base.update(overrides)
    return RecombinationPair(**base)  # type: ignore[arg-type]


def test_mint_patch_order_and_fresh_ids() -> None:
    pair = _mk_pair()
    exp = mint_recombined_experiment(
        pair,
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v7",
        proposed_at="2026-07-11T00:00:00+00:00",
    )
    # A-then-B in ascending-gid order; payloads preserved verbatim.
    assert [p.mutation_id for p in exp.patches] == ["m1", "m2"]
    assert [p.new_content for p in exp.patches] == ["content-a", "content-b"]
    # FRESH uuid4 ids — never the parents' persisted patch ids.
    assert all(p.id not in {"pa", "pb"} for p in exp.patches)
    assert len({p.id for p in exp.patches}) == 2
    assert all(len(p.id) == 32 for p in exp.patches)  # uuid4().hex
    # Provenance rides the FIELD, ascending-gid.
    assert exp.recombined_from == ("v1", "v2")
    assert exp.parent_generation_id == "v0"
    assert exp.generation_id == "v7"
    assert exp.outcome is None


def test_mint_hypothesis_composition() -> None:
    pair = _mk_pair(
        a_core_idea="A " * 100,  # forces the 80-char clip
        b_core_idea="Fix the citations.",
    )
    exp = mint_recombined_experiment(
        pair,
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v7",
        proposed_at="2026-07-11T00:00:00+00:00",
    )
    core = exp.hypothesis.core_idea
    assert core.startswith(RECOMBINED_HYPOTHESIS_MARKER + " ")
    assert " + " in core
    assert len(core) <= 180
    # modulating = union of PATCH mutation-ids (sorted).
    assert exp.hypothesis.modulating == ("m1", "m2")
    # why is COUNTS-ONLY — the entry counts appear, no entry id can (the
    # pair value never carries one).
    assert "1 train entr" in exp.hypothesis.why
    assert "2 distinct entries" in exp.hypothesis.why


def test_mint_movements_concat_dedup_first_wins() -> None:
    a_drift = ExpectedDriftMovement(kind="off_topic", direction="decrease", magnitude="medium")
    b_drift_dup = ExpectedDriftMovement(kind="off_topic", direction="increase", magnitude="small")
    b_drift_new = ExpectedDriftMovement(
        kind="unexpected_output", direction="decrease", magnitude="small"
    )
    a_metric = ExpectedMetricMovement(
        metric_name="cost:tokens_spent", direction="decrease", magnitude="small"
    )
    b_metric_dup = ExpectedMetricMovement(
        metric_name="cost:tokens_spent", direction="increase", magnitude="large"
    )
    pair = _mk_pair(
        a_expected_drift_movements=(a_drift,),
        b_expected_drift_movements=(b_drift_dup, b_drift_new),
        a_expected_metric_movements=(a_metric,),
        b_expected_metric_movements=(b_metric_dup,),
    )
    exp = mint_recombined_experiment(
        pair,
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v7",
        proposed_at="2026-07-11T00:00:00+00:00",
    )
    # Concatenated A-then-B, deduped per axis, FIRST (parent A) wins.
    assert exp.hypothesis.expected_drift_movements == (a_drift, b_drift_new)
    assert exp.hypothesis.expected_metric_movements == (a_metric,)


def test_mint_is_deterministic_up_to_patch_ids() -> None:
    pair = _mk_pair()
    kw = dict(
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v7",
        proposed_at="2026-07-11T00:00:00+00:00",
    )
    one = mint_recombined_experiment(pair, **kw)  # type: ignore[arg-type]
    two = mint_recombined_experiment(pair, **kw)  # type: ignore[arg-type]
    strip = lambda e: replace(  # noqa: E731
        e, patches=tuple(replace(p, id="") for p in e.patches)
    )
    assert strip(one) == strip(two)
