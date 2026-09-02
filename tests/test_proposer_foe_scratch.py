"""The proposer's working copy, and what a change in it is read back as.

The projection is the rule that keeps a free-form edit loop inside the
operator's declared surface, so these cases pin both directions: an edit
inside a declared point becomes that point's patch, and an edit outside
every point blocks the round with the path and the line range. The trees
here carry real markers and are read through the real enumerator, because
what the projection must agree with is the enumerator's own reading of a
point rather than a description of it written beside the test.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from zicato.core.types import MutationPoint
from zicato.mutation.enumerator import enumerate_mutations
from zicato.proposer.foe_scratch import (
    SCRATCH_PREFIX,
    EditOutsideMutationPointError,
    changed_ranges,
    project_onto_mutation_points,
    scratch_working_copy,
)

_SOURCE = textwrap.dedent(
    '''
    import os

    # zicato:mutable id="router__prompt"
    PROMPT = """Route the message."""

    # zicato:mutable id="router__style"
    STYLE = """terse"""
    '''
).lstrip()


def _snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "prompts.py").write_text(_SOURCE, encoding="utf-8")
    return root


def _points(root: Path) -> list[MutationPoint]:
    return enumerate_mutations([root])


def _write(root: Path, body: str) -> None:
    (root / "agent" / "prompts.py").write_text(body, encoding="utf-8")


def _project(snapshot: Path, scratch: Path) -> list:
    return project_onto_mutation_points(
        changed_ranges(snapshot, scratch), _points(snapshot), scratch
    )


def test_the_working_copy_is_a_writable_twin_of_the_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        assert scratch.name.startswith(SCRATCH_PREFIX)
        assert (scratch / "agent" / "prompts.py").read_text(encoding="utf-8") == _SOURCE
        _write(scratch, _SOURCE.replace("terse", "very terse"))
    assert (snapshot / "agent" / "prompts.py").read_text(encoding="utf-8") == _SOURCE


def test_the_working_copy_is_removed_when_the_episode_raises_mid_edit(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    seen: list[Path] = []
    with pytest.raises(RuntimeError, match="the episode died"):
        with scratch_working_copy(snapshot) as scratch:
            seen.append(scratch)
            _write(scratch, _SOURCE.replace("terse", "very terse"))
            raise RuntimeError("the episode died")
    assert not seen[0].exists()


def test_an_edit_inside_one_point_becomes_that_point_s_patch(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        _write(scratch, _SOURCE.replace("Route the message.", "Answer with the agent name."))
        patches = _project(snapshot, scratch)
    assert [p.mutation_id for p in patches] == ["router__prompt"]
    assert patches[0].op == "replace"
    assert patches[0].new_content == 'PROMPT = """Answer with the agent name."""\n'


def test_the_patch_content_is_what_the_enumerator_reads_off_the_copy(tmp_path: Path) -> None:
    """The one authority on a point's value is the enumerator, not the diff."""
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        _write(scratch, _SOURCE.replace("Route the message.", "Say less."))
        patches = _project(snapshot, scratch)
        edited = {p.id: p.content for p in enumerate_mutations([scratch])}
    assert patches[0].new_content == edited["router__prompt"]


def test_edits_in_two_points_become_two_patches(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        _write(
            scratch,
            _SOURCE.replace("Route the message.", "Say less.").replace("terse", "blunt"),
        )
        patches = _project(snapshot, scratch)
    assert [p.mutation_id for p in patches] == ["router__prompt", "router__style"]
    assert {p.new_content for p in patches} == {
        'PROMPT = """Say less."""\n',
        'STYLE = """blunt"""\n',
    }


def test_several_edits_inside_one_point_are_one_patch(tmp_path: Path) -> None:
    """A point is replaced as a unit, so it can only produce one patch."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "p.py").write_text(
        '# zicato:mutable id="block"\nBODY = """a\nb\nc"""\n', encoding="utf-8"
    )
    with scratch_working_copy(snapshot) as scratch:
        (scratch / "p.py").write_text(
            '# zicato:mutable id="block"\nBODY = """A\nb\nC"""\n', encoding="utf-8"
        )
        patches = project_onto_mutation_points(
            changed_ranges(snapshot, scratch), enumerate_mutations([snapshot]), scratch
        )
    assert len(patches) == 1
    assert patches[0].new_content == 'BODY = """A\nb\nC"""\n'


def test_a_change_outside_every_point_names_its_path_and_line_range(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        _write(scratch, _SOURCE.replace("import os", "import os, sys"))
        with pytest.raises(EditOutsideMutationPointError) as raised:
            _project(snapshot, scratch)
    (finding,) = raised.value.findings
    assert "prompts.py:1-1" in finding
    assert "outside every declared mutation point" in finding


def test_a_new_file_the_copy_added_is_outside_every_point(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        (scratch / "agent" / "extra.py").write_text("SNEAK = 1\n", encoding="utf-8")
        with pytest.raises(EditOutsideMutationPointError, match="extra.py"):
            _project(snapshot, scratch)


def test_a_point_the_copy_no_longer_declares_is_refused(tmp_path: Path) -> None:
    """The change is inside the point, and the point is gone anyway.

    A whole-file point that the copy deleted changes only lines the point
    covers, so the line check passes. What fails is that the copy no
    longer declares the point at all, which only re-reading the copy
    through the enumerator can see — and which is why the projection does.
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "brief.md").write_text(
        '<!-- zicato:mutable:file id="brief" -->\nBe terse.\n', encoding="utf-8"
    )
    with scratch_working_copy(snapshot) as scratch:
        (scratch / "brief.md").unlink()
        with pytest.raises(EditOutsideMutationPointError, match="no longer resolves"):
            project_onto_mutation_points(
                changed_ranges(snapshot, scratch), enumerate_mutations([snapshot]), scratch
            )


def test_an_untouched_copy_produces_no_patches(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        assert changed_ranges(snapshot, scratch) == []
        assert _project(snapshot, scratch) == []


def test_a_patch_set_survives_apply_diff_and_projection(tmp_path: Path) -> None:
    """The round trip a proposal makes: edit, read the tree back, compare."""
    snapshot = _snapshot(tmp_path)
    with scratch_working_copy(snapshot) as scratch:
        _write(
            scratch,
            _SOURCE.replace("Route the message.", "Say less.").replace("terse", "blunt"),
        )
        patches = _project(snapshot, scratch)
    assert {p.mutation_id: p.new_content for p in patches} == {
        "router__prompt": 'PROMPT = """Say less."""\n',
        "router__style": 'STYLE = """blunt"""\n',
    }
