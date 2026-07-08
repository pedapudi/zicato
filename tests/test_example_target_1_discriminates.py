"""target_1_presentation now discriminates a challenger from its champion.

Issue #84's root cause was over-determined: the mock harness discarded the
system prompt (so no instruction mutation could change output) AND the
contract left every non-constant scalar term at zero, so every challenger
tied (`delta_scalar = 0.0`) and nothing could promote.

Part 3 fixes both sides. These tests verify the fix DETERMINISTICALLY at the
scoring level (no goldfive/ADK stack, no live model):

1. the mock harness now READS `system` — the mutated instruction changes the
   produced output (a baseline instruction slips in an uncited/fabricated
   metric; a citation-demanding challenger instruction does not);
2. the mock judge now has TEETH — it fires (pass=False) on the fabricated
   output and passes on the cited output, so a declared inline judge actually
   emits a `custom:<name>` drift; and
3. the contract SCORES that difference — a champion whose output trips the
   `no_fabricated_numbers` judge scores strictly worse than the
   citation-demanding challenger, by more than `promote_margin`, so the loop
   can PROMOTE.

Documented remaining gap: a full real-stack `zicato evolve` run additionally
depends on goldfive's LLMPlanner passthrough (the harness output must reach
`final_output` intact); that path needs the ADK stack and is out of scope for
this deterministic check. See RUN.md § "Why it now discriminates".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from zicato.core.types import DriftCount, LossProfile
from zicato.telemetry.reducer import _judge_attributed_kind, compute_drift_loss
from zicato.tournament.scoring import aggregate_generation_score
from zicato.workspace_loader import scoring_weights_from_dict
from zicato_examples.target_1_presentation import mocks

# The v0 (champion) researcher instruction carries NO citation directive; the
# proposer's improved challenger instruction demands a source per claim.
_BASELINE_INSTRUCTION = (
    "You are a researcher. Your goal is to gather information about the topic "
    "the user provides. Provide a comprehensive synthesis of high-quality "
    "bullet points and facts for a presentation slideshow."
)
_IMPROVED_INSTRUCTION = (
    "You are a researcher. Produce a bulleted synthesis where EACH bullet is "
    "one factual claim followed by a short source citation in parentheses. Do "
    "not assert a metric without a citation."
)

_SCORING_PATH = Path(mocks.__file__).resolve().parent / "scoring.json"
_JUDGE_SYSTEM = 'You are a judge. Return JSON {"pass": bool, "reason": str}.'


def _run(coro):  # tiny asyncio.run shim
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The harness reads `system`
# ---------------------------------------------------------------------------


def test_harness_output_depends_on_the_mutated_instruction() -> None:
    user = "Draft a Q3 metrics outline with concrete numbers."
    baseline = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, user, ""))
    improved = _run(mocks.harness_llm(_IMPROVED_INSTRUCTION, user, ""))

    assert baseline != improved, "the mutation surface must change the output"
    assert "unverified estimate" in baseline.lower()
    assert "unverified estimate" not in improved.lower()
    assert "source:" in improved.lower()


# ---------------------------------------------------------------------------
# 2. The judge has teeth
# ---------------------------------------------------------------------------


def test_judge_fires_on_baseline_and_passes_on_improved() -> None:
    baseline = _run(mocks.harness_llm(_BASELINE_INSTRUCTION, "q3 metrics", ""))
    improved = _run(mocks.harness_llm(_IMPROVED_INSTRUCTION, "q3 metrics", ""))

    verdict_baseline = json.loads(_run(mocks.aux_llm(_JUDGE_SYSTEM, baseline, "")))
    verdict_improved = json.loads(_run(mocks.aux_llm(_JUDGE_SYSTEM, improved, "")))

    assert verdict_baseline["pass"] is False, "the judge must fire on a fabricated metric"
    assert verdict_improved["pass"] is True, "the judge must pass cited output"


# ---------------------------------------------------------------------------
# 3. The contract scores the difference → the loop can promote
# ---------------------------------------------------------------------------


def _board_profiles(gen_id: str, *, judge_fires: bool, weights) -> list[LossProfile]:
    """A small board where only the picky entry's judge fires on the champion."""
    profiles: list[LossProfile] = []
    for entry_id in ("waffles_single", "q3_metrics_outline", "picky_stakeholder_emulated"):
        fires = judge_fires and entry_id == "picky_stakeholder_emulated"
        drift_counts = (
            (
                DriftCount(
                    kind=_judge_attributed_kind("no_fabricated_numbers"),
                    severity="critical",
                    count=1,
                ),
            )
            if fires
            else ()
        )
        drift_loss = compute_drift_loss(drift_counts, 0, 0.0, 100, weights)
        profiles.append(
            LossProfile(
                run_id=f"r-{gen_id}-{entry_id}",
                entry_id=entry_id,
                generation_id=gen_id,
                epoch_id="e0",
                drift_counts=drift_counts,
                plan_revisions=0,
                task_failure_ratio=0.0,
                runtime_ms=100,
                wall_clock_budget_exceeded=False,
                expectation_result=None,
                drift_loss=drift_loss,
                pass_fail=True,
            )
        )
    return profiles


def test_contract_discriminates_champion_from_challenger_beyond_the_margin() -> None:
    weights = scoring_weights_from_dict(json.loads(_SCORING_PATH.read_text()))

    champion = aggregate_generation_score(
        _board_profiles("v0", judge_fires=True, weights=weights), weights
    )
    challenger = aggregate_generation_score(
        _board_profiles("v1", judge_fires=False, weights=weights), weights
    )

    delta = float(challenger["scalar"]) - float(champion["scalar"])
    # The challenger's cited output clears the no_fabricated_numbers judge, so
    # its scalar drops by well over the promote margin — the loop can promote.
    assert delta < -weights.promote_margin, (
        f"expected a promotable improvement; champion={champion['scalar']} "
        f"challenger={challenger['scalar']} delta={delta}"
    )
    # And the per-judge weight actually bit (the drift channel moved).
    assert champion["scalar"] > challenger["scalar"]


def test_per_judge_weight_is_wired_for_the_inline_judges() -> None:
    weights = scoring_weights_from_dict(json.loads(_SCORING_PATH.read_text()))
    assert weights.per_judge_weights.get("no_fabricated_numbers") == 3.0
    assert weights.per_judge_weights.get("incorporates_feedback") == 1.5
    assert weights.per_judge_weights.get("audience_appropriate") == 1.5
