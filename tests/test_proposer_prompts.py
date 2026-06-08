"""Tests for the proposer system-prompt skills injection.

The skills block is the Phase 2a surface by which a proposer's
operator-authored guidance modules reach the model. These tests pin two
invariants: the no-skills path is byte-identical to the pre-skills prompt
(so every existing caller is unaffected), and a non-empty skills tuple
appends each skill's name / description / body AFTER the brief block.
"""

from __future__ import annotations

from zicato.core.types import ProposerSkill
from zicato.proposer.prompts import render_skills_block, render_system_prompt

_BRIEF = "# Proposer brief\n- Prefer concrete deltas.\n"


def test_no_skills_is_byte_identical_to_bare_render() -> None:
    # The default-argument call and the explicit empty-tuple call must
    # both reproduce the pre-skills prompt exactly — no trailing section,
    # no extra newline — so every existing caller / test is unaffected.
    assert render_system_prompt(_BRIEF, ()) == render_system_prompt(_BRIEF)


def test_render_skills_block_empty_is_empty_string() -> None:
    assert render_skills_block(()) == ""


def test_skills_appear_after_the_brief() -> None:
    skills = (
        ProposerSkill(
            name="diversify",
            description="avoid re-proposing rejected directions",
            body="When a direction was rejected, change the lever, not the wording.",
        ),
        ProposerSkill(
            name="cost-aware",
            description="watch token spend",
            body="Prefer edits that shrink prompts over edits that grow them.",
        ),
    )
    rendered = render_system_prompt(_BRIEF, skills)

    # The brief body still lands verbatim, and the skills section follows it.
    assert "Prefer concrete deltas." in rendered
    brief_at = rendered.index("Prefer concrete deltas.")
    skills_header_at = rendered.index("Proposer skills (composable guidance modules")
    assert brief_at < skills_header_at

    for skill in skills:
        assert skill.name in rendered
        assert skill.description in rendered
        assert skill.body in rendered
        # Each skill's heading and body land after the brief block.
        assert rendered.index(skill.body) > brief_at


def test_skill_without_description_renders_name_only() -> None:
    skill = ProposerSkill(name="terse", description="", body="Keep rationales to one sentence.")
    block = render_skills_block((skill,))
    assert block.startswith("### terse\n")
    assert "—" not in block.splitlines()[0]


# ---------------------------------------------------------------------------
# Proposer leakage restriction (OVERFITTING.md §11 / §12 #3). When the
# default-on ``restrict_proposer_visibility`` posture is active, the render
# boundary aggregates per-entry pattern identities to counts/rates and
# coarsens experiment-memory Δscalar to buckets. With it OFF, both render
# verbatim, byte-for-byte as before this lever existed.
# ---------------------------------------------------------------------------

from zicato.core.types import MutationPoint, Pattern, PriorExperiment  # noqa: E402
from zicato.proposer.prompts import (  # noqa: E402
    render_metric_targets_block,
    render_pattern_block,
    render_prior_experiments_block,
    render_user_prompt,
)


def _hot_task_pattern() -> Pattern:
    # Mirrors a detect_hot_tasks detail dict, which names the entry + task.
    return Pattern(
        id="p1",
        kind="hot_task",
        summary="task fails frequently",
        detail={
            "entry_id": "contradictory",
            "task_id": "t3",
            "fail_or_block_rate": "0.40",
            "starts": "10",
        },
        affected_mutation_ids=("m1",),
        severity="warning",
    )


def _metric_freq_pattern() -> Pattern:
    return Pattern(
        id="p2",
        kind="metric_frequency",
        summary="off_topic fires often",
        detail={
            "metric": "drift:off_topic",
            "affected_entry_ids": "a,b,c,d",
            "rate": "0.40",
        },
        severity="warning",
    )


def test_pattern_block_unrestricted_is_verbatim() -> None:
    patterns = [_hot_task_pattern(), _metric_freq_pattern()]
    rendered = render_pattern_block(patterns, restrict=False)
    # The default-arg call (no restrict) must match the explicit False.
    assert render_pattern_block(patterns) == rendered
    # Per-entry identities render verbatim when unrestricted.
    assert "entry_id=contradictory" in rendered
    assert "task_id=t3" in rendered
    assert "affected_entry_ids=a,b,c,d" in rendered


def test_pattern_block_restricted_aggregates_identities() -> None:
    patterns = [_hot_task_pattern(), _metric_freq_pattern()]
    rendered = render_pattern_block(patterns, restrict=True)
    # No literal per-entry / per-task identity survives.
    assert "contradictory" not in rendered
    assert "task_id=t3" not in rendered
    assert "a,b,c,d" not in rendered
    # But aggregate counts / rates DO survive so a general fix can be sized.
    assert "entries_affected=4" in rendered
    assert "fail_or_block_rate=0.40" in rendered
    assert "rate=0.40" in rendered
    # The non-leaky structure (id / kind / summary / mutation ids) is intact.
    assert "id=p1" in rendered
    assert "task fails frequently" in rendered
    assert "affected_mutation_ids: m1" in rendered


def _prior(delta: float | None, *, gen: str = "g1") -> PriorExperiment:
    return PriorExperiment(
        generation_id=gen,
        epoch_id="e1",
        core_idea="tighten the refusal guard",
        modulating=("m1",),
        decision="promoted",
        rejection_reason="",
        scalar_score_delta=delta,
        same_contract=True,
    )


def test_prior_experiments_unrestricted_shows_fine_grained_delta() -> None:
    prior = [_prior(-0.137)]
    rendered = render_prior_experiments_block(prior, restrict=False)
    assert render_prior_experiments_block(prior) == rendered  # default == False
    assert "Δscalar=-0.137" in rendered
    assert "improved" not in rendered


def test_prior_experiments_restricted_buckets_delta() -> None:
    improved = render_prior_experiments_block([_prior(-0.137, gen="gi")], restrict=True)
    flat = render_prior_experiments_block([_prior(0.001, gen="gf")], restrict=True)
    regressed = render_prior_experiments_block([_prior(0.250, gen="gr")], restrict=True)
    assert "Δscalar=improved" in improved
    assert "Δscalar=flat" in flat
    assert "Δscalar=regressed" in regressed
    # The exact number never reaches the prompt.
    assert "-0.137" not in improved
    assert "0.250" not in regressed


# ---------------------------------------------------------------------------
# Valid expectation targets block — the prompt must enumerate, for THIS
# board, the declared judges and the valid built-in drift kinds, and show
# the EXACT metric-movement shape so the proposer can write a movement that
# validates instead of mangling a declared judge into ``drift:custom:<name>``.
# These tests pin that the prompt and the validator's accepted forms agree.
# ---------------------------------------------------------------------------


def test_metric_targets_block_enumerates_declared_judges_and_correct_shape() -> None:
    from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS

    block = render_metric_targets_block(["file_findability"])
    # The declared judge is named for THIS board.
    assert "file_findability" in block
    # The CORRECT metric-movement shape uses the bare judge name as
    # metric_name — exactly what the validator accepts.
    assert '"metric_name": "file_findability"' in block
    # And the prompt explicitly flags the mangles the model naturally
    # produces as WRONG, so it does not reach for them.
    assert "drift:custom:file_findability" in block
    assert "custom:file_findability" in block
    assert "WRONG" in block
    # The built-in drift kinds are enumerated and shown in their own
    # (``drift:<kind>``) form, distinct from the bare-judge form.
    assert "drift:<kind>" in block
    for kind in GOLDFIVE_DRIFT_KINDS:
        assert kind in block


def test_metric_targets_block_no_judges_renders_explicit_notice() -> None:
    block = render_metric_targets_block(())
    assert "no custom judges" in block
    # The drift-kind enumeration is always present, judges or not.
    assert "drift:<kind>" in block


def test_user_prompt_includes_valid_expectation_targets_for_declared_judges() -> None:
    from pathlib import Path

    mutation = MutationPoint(
        id="router__system_prompt",
        kind="span",
        file=Path("/abs/router.py"),
        source_root=Path("/abs"),
        line_start=1,
        line_end=3,
        content="route the message",
        content_hash="h",
        metadata={},
    )
    rendered = render_user_prompt(
        current_loss_summary="loss is high",
        patterns=[],
        mutations=[mutation],
        custom_judge_names=["file_findability"],
    )
    # The dedicated section header is present in the assembled user prompt.
    assert "## Valid expectation targets" in rendered
    # The declared judge, the correct shape, and the drift-kind enumeration
    # all reach the model in the rendered prompt.
    assert '"metric_name": "file_findability"' in rendered
    assert "drift:<kind>" in rendered
    assert "off_topic" in rendered
