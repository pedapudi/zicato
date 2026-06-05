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
