"""Tests for the proposer rubric parser and forbidden-id enforcement."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.proposer.rubric import Rubric, enforce_forbidden, load_rubric

# ---------------------------------------------------------------------------
# load_rubric
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "rubric.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_rubric_extracts_forbidden_backticked_ids(tmp_path: Path) -> None:
    body = """\
# Rubric

Some prose the operator wrote.

# Forbidden edits
- Never touch the `router__system_prompt` this epoch.
- Hands off `planner__tool_descriptions` too.

# Preferred edits
- Tighten `researcher__system_prompt` instead.
"""
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.forbidden_ids == ("router__system_prompt", "planner__tool_descriptions")
    assert rubric.preferred_ids == ("researcher__system_prompt",)
    assert "Some prose" in rubric.text


def test_load_rubric_falls_back_to_quoted_ids(tmp_path: Path) -> None:
    body = """\
# Forbidden edits
- Don't touch "router_main"
- And avoid 'planner_alt' too
"""
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.forbidden_ids == ("router_main", "planner_alt")


def test_load_rubric_handles_missing_sections(tmp_path: Path) -> None:
    body = "# Just prose\n\nNo structured sections here.\n"
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.forbidden_ids == ()
    assert rubric.preferred_ids == ()


def test_load_rubric_case_insensitive_heading(tmp_path: Path) -> None:
    body = """\
## FORBIDDEN Edits
- Avoid `id_one`.
"""
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.forbidden_ids == ("id_one",)


def test_load_rubric_dedupes_repeated_ids(tmp_path: Path) -> None:
    body = """\
# Forbidden edits
- Avoid `id_a`.
- Avoid `id_a` again.
- And also `id_b`.
"""
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.forbidden_ids == ("id_a", "id_b")


def test_load_rubric_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_rubric(tmp_path / "does_not_exist.md")


def test_load_rubric_full_text_passes_through(tmp_path: Path) -> None:
    body = "# Title\n\nLine A.\nLine B.\n"
    rubric = load_rubric(_write(tmp_path, body))
    assert rubric.text == body


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
# Rubric dataclass shape
# ---------------------------------------------------------------------------


def test_rubric_is_frozen() -> None:
    r = Rubric(text="x", forbidden_ids=("a",), preferred_ids=())
    # ``@dataclass(frozen=True)`` raises FrozenInstanceError on assignment.
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.text = "y"  # type: ignore[misc]
