"""Tests for :mod:`zicato.proposer.skills` — skill loading + spec resolution.

The loader must discover ``*.md`` skills sorted by filename, parse
SKILL.md-style frontmatter (tolerating its absence), and resolve a
proposer dir (or ``None``) into a hash-ready
:class:`~zicato.core.types.ProposerSpec`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.core.types import ProposerSpec
from zicato.proposer.skills import (
    load_proposer_skills,
    normalize_skill_body,
    resolve_proposer_spec,
)


def test_load_skills_parses_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "a.md").write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\nPrefer terse patches.\n"
    )
    skills = load_proposer_skills(skills_dir)
    assert len(skills) == 1
    assert skills[0].name == "tighten"
    assert skills[0].description == "keep it terse"
    assert "Prefer terse patches." in skills[0].body


def test_load_skills_tolerates_missing_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "raw_skill.md").write_text("Just a body, no frontmatter.\n")
    skills = load_proposer_skills(skills_dir)
    assert len(skills) == 1
    # Name falls back to the file stem; description is empty.
    assert skills[0].name == "raw_skill"
    assert skills[0].description == ""
    assert skills[0].body.strip() == "Just a body, no frontmatter."


def test_load_skills_sorted_by_filename(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "z.md").write_text("---\nname: z\n---\nz\n")
    (skills_dir / "a.md").write_text("---\nname: a\n---\na\n")
    skills = load_proposer_skills(skills_dir)
    assert [s.name for s in skills] == ["a", "z"]


def test_load_skills_missing_dir_is_empty(tmp_path: Path) -> None:
    assert load_proposer_skills(tmp_path / "nope") == ()


def test_resolve_none_is_builtin_default() -> None:
    spec = resolve_proposer_spec(None)
    assert spec == ProposerSpec.default()
    assert spec.agent_id == "builtin:default"
    assert spec.tools == ()
    assert spec.skills == ()


def test_resolve_dir_sets_agent_id_and_skills(tmp_path: Path) -> None:
    proposer = tmp_path / "proposers" / "p1"
    (proposer / "skills").mkdir(parents=True)
    (proposer / "skills" / "a.md").write_text("---\nname: tighten\n---\nbody\n")
    spec = resolve_proposer_spec(proposer)
    assert spec.agent_id == "dir:p1"
    assert spec.tools == ()
    assert [s.name for s in spec.skills] == ["tighten"]


def test_a_proposer_dir_carrying_an_agent_module_is_refused(tmp_path: Path) -> None:
    """A directory that still ships ``agent.py`` describes a removed runtime.

    Resolving it is fine — the skills are still the epoch's, and the
    contract still hashes — but building an agent from it refuses by
    name, so the operator is told to delete the module rather than
    discovering at the first propose that it never ran.
    """
    from zicato.proposer.agent import build_proposer_agent
    from zicato.proposer.foe_config import ProposerConfigError

    proposer = tmp_path / "proposers" / "p1"
    (proposer / "skills").mkdir(parents=True)
    (proposer / "agent.py").write_text("def build():\n    return 1\n")
    spec = resolve_proposer_spec(proposer)

    with pytest.raises(ProposerConfigError, match="custom proposer agent modules were removed"):
        build_proposer_agent(spec, proposer_path=proposer)


def test_normalize_skill_body_strips_whitespace_noise() -> None:
    a = normalize_skill_body("body line   \r\n\r\n")
    b = normalize_skill_body("\n\nbody line\n")
    assert a == b == "body line"
