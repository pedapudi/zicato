"""The critic-calibration channel (WS-CAL): sampler + redaction + render.

Everything here is a pure function of constructed inputs — no fixtures, no
IO — mirroring the genealogy-channel tests. Four groups:

* the deterministic sampler: hit / miss / unresolved classification, the
  counts + pooled fraction, recency ordering + the K cap, placebo exclusion,
  run-to-run determinism;
* REDACTION (adversarial): a claim whose Δscalar + core idea carry distinctive
  numbers and long identity-laden content — the exact Δscalar never escapes
  (only a band), the core idea is capped, and the claim / summary types
  structurally cannot carry a board-entry id or a per-entry result;
* the prompt-render golden: byte-identical at ``calibration = None`` and the
  section's placement when present (above experiment memory, below genealogy).
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.proposer.calibration import (
    _CORE_IDEA_MAX,
    CalibrationClaim,
    CalibrationClaimItem,
    CalibrationSummary,
    sample_calibration,
)
from zicato.proposer.genealogy import GenealogyRecord
from zicato.proposer.prompts import (
    render_calibration_block,
    render_user_prompt,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _claim(
    gid: str,
    *,
    round_index: int = 1,
    core_idea: str = "",
    delta: float | None = -0.5,
    matches: int = 2,
    predictions: int = 2,
    placebo: bool = False,
) -> CalibrationClaim:
    return CalibrationClaim(
        generation_id=gid,
        round_index=round_index,
        core_idea=core_idea or f"idea of {gid}",
        scalar_score_delta=delta,
        matches=matches,
        predictions=predictions,
        is_placebo=placebo,
    )


# ---------------------------------------------------------------------------
# The deterministic sampler — grading + counts
# ---------------------------------------------------------------------------


def test_grade_classification_hit_miss_unresolved() -> None:
    """A claim with all predictions verified is a hit; a partial verify is a
    miss; a claim with no predictions is unresolved (and never in `recent`)."""
    claims = [
        _claim("hit", matches=3, predictions=3, round_index=3),
        _claim("miss", matches=1, predictions=3, round_index=2),
        _claim("unres", matches=0, predictions=0, round_index=1),
    ]
    summary = sample_calibration(claims, 5)
    assert summary is not None
    assert summary.hit_count == 1
    assert summary.miss_count == 1
    assert summary.unresolved_count == 1
    grades = {item.generation_id: item.grade for item in summary.recent}
    assert grades == {"hit": "hit", "miss": "miss"}  # unresolved is excluded


def test_calibration_fraction_is_pooled_hit_over_graded() -> None:
    """The fraction is hit / (hit + miss) — unresolved claims never dilute it."""
    claims = [
        _claim("h1", matches=1, predictions=1),
        _claim("h2", matches=2, predictions=2),
        _claim("m1", matches=0, predictions=1),
        _claim("u1", matches=0, predictions=0),
    ]
    summary = sample_calibration(claims, 5)
    assert summary is not None
    assert summary.calibration_fraction == 2 / 3  # 2 hits of 3 graded


def test_recent_is_most_recent_first_and_capped_to_k() -> None:
    """`recent` sorts by round DOWN (gid ascending tie-break) and caps at k."""
    claims = [
        _claim("a", matches=1, predictions=1, round_index=1),
        _claim("b", matches=0, predictions=1, round_index=5),
        _claim("c", matches=1, predictions=1, round_index=3),
    ]
    summary = sample_calibration(claims, 2)
    assert summary is not None
    assert [item.generation_id for item in summary.recent] == ["b", "c"]


def test_placebo_claims_are_excluded_from_every_tally() -> None:
    claims = [
        _claim("real", matches=1, predictions=1),
        _claim("placebo", matches=0, predictions=3, placebo=True),
    ]
    summary = sample_calibration(claims, 5)
    assert summary is not None
    assert summary.hit_count == 1
    assert summary.miss_count == 0
    assert summary.unresolved_count == 0


def test_k_zero_returns_none() -> None:
    assert sample_calibration([_claim("x")], 0) is None
    assert sample_calibration([_claim("x")], -1) is None


def test_no_graded_history_returns_none() -> None:
    """A reign whose settled hypotheses all made no falsifiable predictions
    has no miss pattern to show — the sampler omits the block entirely."""
    claims = [_claim("u1", matches=0, predictions=0), _claim("u2", matches=0, predictions=0)]
    assert sample_calibration(claims, 5) is None


def test_empty_and_all_placebo_return_none() -> None:
    assert sample_calibration([], 5) is None
    assert sample_calibration([_claim("p", placebo=True)], 5) is None


def test_sampler_is_deterministic_run_to_run() -> None:
    claims = [
        _claim("a", matches=1, predictions=1, round_index=2),
        _claim("b", matches=0, predictions=2, round_index=4),
        _claim("c", matches=1, predictions=1, round_index=4),
    ]
    assert sample_calibration(claims, 5) == sample_calibration(claims, 5)


def test_sampler_is_order_independent() -> None:
    claims = [
        _claim("a", matches=1, predictions=1, round_index=2),
        _claim("b", matches=0, predictions=2, round_index=4),
        _claim("c", matches=1, predictions=1, round_index=1),
    ]
    assert sample_calibration(claims, 5) == sample_calibration(list(reversed(claims)), 5)


# ---------------------------------------------------------------------------
# REDACTION (adversarial) — the outcome can never leak
# ---------------------------------------------------------------------------

#: A distinctive Δscalar that must NEVER render verbatim — only its band.
_ADVERSARIAL_DELTA = -0.1234567


def test_exact_delta_never_escapes_only_the_band() -> None:
    """The per-claim realized outcome is banded — no raw number reaches output."""
    claim = _claim("v7", delta=_ADVERSARIAL_DELTA, matches=1, predictions=1)
    block = render_calibration_block(sample_calibration([claim], 5))
    assert "improved" in block
    assert "0.1234567" not in block
    assert "-0.1234567" not in block


def test_recent_item_banded_outcome_is_one_of_three_bands() -> None:
    for delta, band in ((-0.5, "improved"), (0.0, "flat"), (0.9, "regressed")):
        summary = sample_calibration([_claim("g", delta=delta, matches=1, predictions=1)], 5)
        assert summary is not None
        assert summary.recent[0].banded_outcome == band


def test_unsettled_delta_renders_no_band() -> None:
    summary = sample_calibration([_claim("h", delta=None, matches=1, predictions=1)], 5)
    assert summary is not None
    assert summary.recent[0].banded_outcome == ""
    assert "unsettled" in render_calibration_block(summary)


def test_core_idea_is_capped() -> None:
    long_idea = "IDEA_HEAD " + ("y" * 5000) + " IDEA_TAIL"
    summary = sample_calibration([_claim("v7", core_idea=long_idea, matches=1, predictions=1)], 5)
    assert summary is not None
    item = summary.recent[0]
    assert len(item.core_idea) <= _CORE_IDEA_MAX
    assert item.core_idea.endswith("…")
    assert item.core_idea.startswith("IDEA_HEAD")
    assert "IDEA_TAIL" not in render_calibration_block(summary)


def test_no_fine_grained_decimal_leaks_from_the_outcome() -> None:
    """The rendered claim line carries no multi-digit decimal (only the band).

    The proposer's own core idea may legitimately carry numbers (in-envelope),
    so the scan targets the claim line minus the core-idea tail.
    """
    claim = _claim("v7", delta=_ADVERSARIAL_DELTA, core_idea="plain idea", matches=1, predictions=1)
    block = render_calibration_block(sample_calibration([claim], 5))
    # The claim line is `- HIT · Δscalar improved · plain idea`; strip the
    # core-idea tail (after the last ` · `) before scanning for raw decimals.
    claim_line = next(line for line in block.splitlines() if line.startswith("- "))
    head = claim_line.rsplit(" · ", 1)[0]
    assert not re.search(r"-?\d+\.\d{2,}", head), head


def test_summary_and_item_types_carry_no_per_entry_field() -> None:
    """STRUCTURAL envelope proof: no type has an entry-id / per-entry slot."""
    claim_fields = {f.name for f in fields(CalibrationClaim)}
    item_fields = {f.name for f in fields(CalibrationClaimItem)}
    summary_fields = {f.name for f in fields(CalibrationSummary)}
    forbidden = {"entry_id", "entry_ids", "per_entry", "movement", "movements", "holdout"}
    assert claim_fields.isdisjoint(forbidden)
    assert item_fields.isdisjoint(forbidden)
    assert summary_fields.isdisjoint(forbidden)
    # The only numeric outcome an item exposes is the banded string.
    assert "banded_outcome" in item_fields
    assert "scalar_score_delta" not in item_fields


def test_summary_carries_no_exact_delta_field() -> None:
    """The rendered item exposes a band, never the raw scalar it was built from."""
    claim = _claim("v7", delta=_ADVERSARIAL_DELTA, matches=1, predictions=1)
    summary = sample_calibration([claim], 5)
    assert summary is not None
    assert not hasattr(summary.recent[0], "scalar_score_delta")


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


def test_calibration_none_renders_empty_block() -> None:
    assert render_calibration_block(None) == ""


def test_calibration_default_is_byte_identical() -> None:
    """A ``calibration = None`` round renders the exact prompt of before."""
    baseline = render_user_prompt(current_loss_summary="loss", patterns=[], mutations=[_mutation()])
    with_default = render_user_prompt(
        current_loss_summary="loss", patterns=[], mutations=[_mutation()], calibration=None
    )
    assert with_default == baseline
    assert "## Prediction calibration" not in with_default


def test_calibration_section_renders_when_present() -> None:
    summary = sample_calibration(
        [
            _claim("v2", matches=1, predictions=1, round_index=2, core_idea="called it"),
            _claim("v3", matches=0, predictions=2, round_index=3, core_idea="over-claimed"),
        ],
        5,
    )
    prompt = render_user_prompt(
        current_loss_summary="loss", patterns=[], mutations=[_mutation()], calibration=summary
    )
    assert "## Prediction calibration" in prompt
    assert "HIT" in prompt and "MISS" in prompt
    assert "called it" in prompt


def test_calibration_lands_below_genealogy_above_experiment_memory() -> None:
    """Splice order: genealogy block (top), then calibration, then the
    experiment-memory block below it."""
    from zicato.core.types import PriorExperiment
    from zicato.proposer.genealogy import sample_genealogy

    gene = sample_genealogy(
        [
            GenealogyRecord(
                generation_id="v2",
                parent_generation_id="v0",
                decision="promoted",
                round_index=2,
                core_idea="promoted",
                patch_mutation_ids=frozenset({"m1"}),
                patch_op_kinds=("replace",),
                patch_text="edit",
                scalar_score_delta=-0.4,
            )
        ],
        {},
        2,
        champion_id="v2",
    )
    cal = sample_calibration([_claim("v3", matches=1, predictions=1, core_idea="idea")], 5)
    prior = (
        PriorExperiment(
            epoch_id="e1",
            generation_id="v3",
            decision="rejected",
            rejection_reason="",
            modulating=("m1",),
            core_idea="prior thing",
            scalar_score_delta=-0.1,
            same_contract=True,
        ),
    )
    prompt = render_user_prompt(
        current_loss_summary="loss",
        patterns=[],
        mutations=[_mutation()],
        genealogy=gene,
        calibration=cal,
        prior_experiments=prior,
    )
    i_gene = prompt.index("## Candidate genealogy")
    i_cal = prompt.index("## Prediction calibration")
    i_mem = prompt.index("## What's already been tried")
    assert i_gene < i_cal < i_mem
