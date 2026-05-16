"""Tests for the proposer brief parser and forbidden-id enforcement."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.proposer.brief import ProposerBrief, enforce_forbidden, load_brief

# ---------------------------------------------------------------------------
# load_brief
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "brief.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_brief_extracts_forbidden_backticked_ids(tmp_path: Path) -> None:
    body = """\
# Proposer brief

Some prose the operator wrote.

# Forbidden edits
- Never touch the `router__system_prompt` this epoch.
- Hands off `planner__tool_descriptions` too.

# Preferred edits
- Tighten `researcher__system_prompt` instead.
"""
    brief = load_brief(_write(tmp_path, body))
    assert brief.forbidden_ids == ("router__system_prompt", "planner__tool_descriptions")
    assert brief.preferred_ids == ("researcher__system_prompt",)
    assert "Some prose" in brief.text


def test_load_brief_falls_back_to_quoted_ids(tmp_path: Path) -> None:
    body = """\
# Forbidden edits
- Don't touch "router_main"
- And avoid 'planner_alt' too
"""
    brief = load_brief(_write(tmp_path, body))
    assert brief.forbidden_ids == ("router_main", "planner_alt")


def test_load_brief_handles_missing_sections(tmp_path: Path) -> None:
    body = "# Just prose\n\nNo structured sections here.\n"
    brief = load_brief(_write(tmp_path, body))
    assert brief.forbidden_ids == ()
    assert brief.preferred_ids == ()


def test_load_brief_case_insensitive_heading(tmp_path: Path) -> None:
    body = """\
## FORBIDDEN Edits
- Avoid `id_one`.
"""
    brief = load_brief(_write(tmp_path, body))
    assert brief.forbidden_ids == ("id_one",)


def test_load_brief_dedupes_repeated_ids(tmp_path: Path) -> None:
    body = """\
# Forbidden edits
- Avoid `id_a`.
- Avoid `id_a` again.
- And also `id_b`.
"""
    brief = load_brief(_write(tmp_path, body))
    assert brief.forbidden_ids == ("id_a", "id_b")


def test_load_brief_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_brief(tmp_path / "does_not_exist.md")


def test_load_brief_full_text_passes_through(tmp_path: Path) -> None:
    body = "# Title\n\nLine A.\nLine B.\n"
    brief = load_brief(_write(tmp_path, body))
    assert brief.text == body


# ---------------------------------------------------------------------------
# enforce_forbidden
# ---------------------------------------------------------------------------


def _patch(mutation_id: str) -> Patch:
    return Patch(
        id="p1",
        mutation_id=mutation_id,
        op="replace",
        new_content="x",
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )


def test_enforce_forbidden_empty_when_no_forbidden_ids() -> None:
    assert enforce_forbidden([_patch("anything")], ()) == []


def test_enforce_forbidden_empty_when_no_violations() -> None:
    assert enforce_forbidden([_patch("ok_id")], ("bad_id",)) == []


def test_enforce_forbidden_flags_violations() -> None:
    errors = enforce_forbidden([_patch("bad_id")], ("bad_id",))
    assert len(errors) == 1
    assert "bad_id" in errors[0]


def test_enforce_forbidden_returns_one_error_per_offending_patch() -> None:
    patches = [_patch("a"), _patch("b"), _patch("a")]
    errors = enforce_forbidden(patches, ("a",))
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# ProposerBrief dataclass shape
# ---------------------------------------------------------------------------


def test_proposer_brief_is_frozen() -> None:
    b = ProposerBrief(text="x", forbidden_ids=("a",), preferred_ids=())
    # ``@dataclass(frozen=True)`` raises FrozenInstanceError on assignment.
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.text = "y"  # type: ignore[misc]
