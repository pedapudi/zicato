"""The mutation surface on non-Python files.

Three tests carry the weight and the rest support them: the ``.py``
byte-identity pin (widening must move ZERO Python points), the guard that
a patch can neither escape its region nor eat its own markers, and the
end-to-end enumerate -> validate -> apply -> re-enumerate round trip.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import (
    MAX_TEXT_FILE_BYTES,
    enumerate_mutations,
    is_text_mutation_candidate,
)
from zicato.mutation.markers import is_end_marker, parse_marker_line
from zicato.mutation.validator import validate_patches, validate_post_apply


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


def _replace(mutation_id: str, new_content: str) -> Patch:
    return Patch(
        id=f"patch_{mutation_id}",
        mutation_id=mutation_id,
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale=f"test rewrite of {mutation_id}",
    )


# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected_metadata"),
    [
        ('# zicato:mutable:file id="x"', {}),
        ('<!-- zicato:mutable:file id="x" -->', {}),
        ('    <!-- zicato:mutable:file id="x" -->   ', {}),
        ('<!-- zicato:mutable:file id="x" role="prompt" -->', {"role": "prompt"}),
    ],
)
def test_text_syntax_leaders_closers_and_metadata(
    line: str, expected_metadata: dict[str, str]
) -> None:
    """A closer lands in the tail, so it never leaks into metadata."""

    parsed = parse_marker_line(line, syntax="text")
    assert parsed is not None
    assert (parsed.id, parsed.is_file) == ("x", True)
    assert parsed.metadata == expected_metadata


def test_python_syntax_stays_hash_only() -> None:
    """The byte-identity guarantee, at the grammar level."""

    assert parse_marker_line('<!-- zicato:mutable id="x" -->') is None
    assert is_end_marker("<!-- zicato:mutable:end -->") is False
    assert is_end_marker("<!-- zicato:mutable:end -->", syntax="text") is True
    assert is_end_marker("# zicato:mutable:end") is True


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_allowlist_decides_without_opening_the_file() -> None:
    assert is_text_mutation_candidate(Path("prompt.md")) is True
    assert is_text_mutation_candidate(Path("config.yaml")) is True
    assert is_text_mutation_candidate(Path("mod.py")) is False  # the Python walk owns it
    assert is_text_mutation_candidate(Path("logo.png")) is False
    assert is_text_mutation_candidate(Path("data.json")) is False  # no comment syntax
    assert is_text_mutation_candidate(Path("notes")) is False


def test_walk_skips_binaries_and_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "blob.png").write_bytes(b"\x89PNG\x00\x00zicato:mutable:file")
    (tmp_path / "huge.md").write_text(
        '<!-- zicato:mutable:file id="huge" -->\n' + "x" * MAX_TEXT_FILE_BYTES,
        encoding="utf-8",
    )
    _write(tmp_path / "prompt.md", '<!-- zicato:mutable:file id="p" -->\nhello\n')
    assert {p.id for p in enumerate_mutations([tmp_path])} == {"p"}


def test_vendored_dirs_are_pruned_from_the_text_pass_only(tmp_path: Path) -> None:
    """Pruning the Python pass would move existing points; it must not."""

    _write(
        tmp_path / "node_modules" / "pkg" / "prompt.md",
        '<!-- zicato:mutable:file id="vendored" -->\nhi\n',
    )
    _write(
        tmp_path / "node_modules" / "pkg" / "mod.py",
        """
        # zicato:mutable id="vendored_py"
        X = "hello"
        """,
    )
    assert {p.id for p in enumerate_mutations([tmp_path])} == {"vendored_py"}


def test_single_file_roots_resolve_by_suffix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-.py single-file root used to enumerate to zero in silence."""

    prompt = tmp_path / "prompt.md"
    _write(prompt, '<!-- zicato:mutable:file id="single" -->\nbody\n')
    points = enumerate_mutations([prompt])
    assert [p.id for p in points] == ["single"]
    assert points[0].source_root == tmp_path

    blob = tmp_path / "weights.bin"
    blob.write_bytes(b"\x00\x01")
    with caplog.at_level(logging.WARNING, logger="zicato.mutation.enumerator"):
        assert enumerate_mutations([blob]) == []
    assert "contributes no mutation points" in caplog.text


def test_bare_span_marker_in_a_text_file_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Not supported, and deliberately not a silent drop."""

    _write(tmp_path / "config.yaml", '# zicato:mutable id="temperature"\ntemperature: 0.7\n')
    with caplog.at_level(logging.WARNING, logger="zicato.mutation.enumerator"):
        assert enumerate_mutations([tmp_path]) == []
    assert "span markers bind to a Python string literal" in caplog.text
    assert "temperature" in caplog.text


def test_grading_sentinel_skips_a_text_file_wholesale(tmp_path: Path) -> None:
    _write(
        tmp_path / "rubric.md",
        """
        <!-- zicato:grading -->
        <!-- zicato:mutable:file id="rubric" -->
        The operator owns this rubric.
        """,
    )
    assert enumerate_mutations([tmp_path]) == []


# --------------------------------------------------------------------------
# The .py byte-identity pin
# --------------------------------------------------------------------------


def _python_fixture_tree(root: Path) -> None:
    """A tree exercising every historical Python resolution rule."""

    _write(
        root / "pkg" / "agent.py",
        '''
        """Module docstring showing the syntax:

        # zicato:mutable id="documented_not_real"
        """

        # zicato:mutable id="instruction"
        INSTRUCTION = """You research the user's question."""

        # zicato:mutable id="description" role="tool_description"
        DESCRIPTION = "Performs lookup."

        def make(x):
            # zicato:mutable:code id="slug_logic"
            slug = x.lower()
            slug = slug.replace(" ", "-")
            # zicato:mutable:end
            return slug

        # zicato:mutable id="dangling_no_literal_below"
        ''',
    )
    _write(
        root / "pkg" / "prompts.py",
        '''
        """Prompts."""

        # zicato:mutable:file id="all_prompts"

        INTRO = "intro"
        # zicato:mutable id="outline" required_placeholders="{n}"
        OUTLINE = "outline with {n}"
        ''',
    )
    _write(
        root / "pkg" / "grading.py",
        """
        # zicato:grading
        # zicato:mutable id="never_enumerated"
        JUDGE = "operator owned"
        """,
    )
    _write(root / "pkg" / "broken.py", "def f(:\n")


#: Captured by running the PRE-widening enumerator over
#: ``_python_fixture_tree`` and pasting its output verbatim, in the
#: enumerator's own sort order. Note what is absent and must stay absent:
#: ``documented_not_real`` (a marker inside a docstring), ``never_enumerated``
#: (behind a ``# zicato:grading`` sentinel), ``dangling_no_literal_below``
#: (a span marker with no literal beneath it), and anything at all from
#: the unparseable ``broken.py``.
_PY_GOLDEN: tuple[tuple[object, ...], ...] = (
    ("instruction", "span", "pkg/agent.py", 7, 7),
    ("description", "span", "pkg/agent.py", 10, 10),
    ("slug_logic", "code", "pkg/agent.py", 14, 15),
    ("all_prompts", "file", "pkg/prompts.py", 1, 7),
    ("outline", "span", "pkg/prompts.py", 7, 7),
)


def test_python_enumeration_is_byte_identical(tmp_path: Path) -> None:
    """The pin. Two halves, both equality assertions, never membership.

    First: the fixture tree's whole point set equals the golden captured
    from the pre-widening enumerator. Second: the SAME tree enumerated
    beside marker-carrying markdown / YAML files yields Python rows
    identical field for field — ``content`` and ``content_hash`` included.
    """

    bare, mixed = tmp_path / "bare", tmp_path / "mixed"
    _python_fixture_tree(bare)
    _python_fixture_tree(mixed)
    _write(mixed / "prompt.md", '<!-- zicato:mutable:file id="md" -->\nbody\n')
    _write(mixed / "cfg.yaml", '# zicato:mutable:code id="yml"\na: 1\n# zicato:mutable:end\n')
    _write(mixed / "pkg" / "notes.txt", "no markers here\n")

    def rows(root: Path) -> list[dict[str, object]]:
        out = []
        for point in enumerate_mutations([root]):
            row = asdict(point)
            row["file"] = point.file.relative_to(root).as_posix()
            row["source_root"] = "<root>"
            out.append(row)
        return out

    bare_rows = rows(bare)
    assert (
        tuple((r["id"], r["kind"], r["file"], r["line_start"], r["line_end"]) for r in bare_rows)
        == _PY_GOLDEN
    )
    assert [r for r in rows(mixed) if str(r["file"]).endswith(".py")] == bare_rows


# --------------------------------------------------------------------------
# Containment: a patch cannot escape the region or eat its markers
# --------------------------------------------------------------------------


def test_region_patch_cannot_escape_or_eat_its_markers(tmp_path: Path) -> None:
    """The containment guard.

    The replacement tries to close the region early, inject content
    outside it, and open a fresh region under an id of its own choosing.
    All three marker lines are stripped: the anchors survive, no new point
    appears, and the surrounding text is untouched.
    """

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(
        src / "prompt.md",
        """
        # Header the proposer must not touch

        <!-- zicato:mutable:code id="brief" -->
        original brief
        <!-- zicato:mutable:end -->

        Footer the proposer must not touch
        """,
    )
    hostile = (
        "<!-- zicato:mutable:end -->\n"
        "escaped content\n"
        '<!-- zicato:mutable:code id="hijacked" -->\n'
        "more\n"
    )
    apply_patches(src, [_replace("brief", hostile)], dst)

    text = (dst / "prompt.md").read_text(encoding="utf-8")
    assert "# Header the proposer must not touch" in text
    assert "Footer the proposer must not touch" in text
    assert "original brief" not in text
    assert text.count("zicato:mutable:code") == 1
    assert text.count("zicato:mutable:end") == 1
    assert "hijacked" not in text

    points = {p.id: p for p in enumerate_mutations([dst])}
    assert set(points) == {"brief"}
    assert points["brief"].content == "escaped content\nmore\n"


def test_whole_file_patch_that_drops_its_marker_is_caught_post_apply(tmp_path: Path) -> None:
    """A ``:file`` point CAN delete its own marker; A2 rejects the result."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "prompt.md", '<!-- zicato:mutable:file id="whole" -->\nbody\n')
    pre = enumerate_mutations([src])
    patch = _replace("whole", "no marker any more\n")
    apply_patches(src, [patch], dst)
    assert any(
        "no longer resolves" in problem for problem in validate_post_apply(dst, [patch], pre)
    )


def test_region_replace_preserves_relative_indentation(tmp_path: Path) -> None:
    """Nested YAML survives a proposer that emits at column 0."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(
        src / "config.yaml",
        """
        service:
          policy:
            # zicato:mutable:code id="retry"
            retries: 3
            nested:
              backoff: 1
            # zicato:mutable:end
        """,
    )
    apply_patches(src, [_replace("retry", "retries: 5\nnested:\n  backoff: 2\n")], dst)
    assert (dst / "config.yaml").read_text(encoding="utf-8") == textwrap.dedent(
        """\
        service:
          policy:
            # zicato:mutable:code id="retry"
            retries: 5
            nested:
              backoff: 2
            # zicato:mutable:end
        """
    )


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_end_to_end_text_only_surface(tmp_path: Path) -> None:
    """enumerate -> validate -> apply -> re-enumerate, no Python involved."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(
        src / "prompts" / "researcher.md",
        """
        # Researcher

        <!-- zicato:mutable:code id="researcher_brief" required_placeholders="{topic}" -->
        Research {topic} and answer in three sentences.
        <!-- zicato:mutable:end -->
        """,
    )
    _write(
        src / "config" / "runtime.yaml",
        """
        runtime:
          # zicato:mutable:code id="retry_policy"
          retries: 3
          # zicato:mutable:end
          owner: operator
        """,
    )
    _write(
        src / "config" / "tools.toml", '# zicato:mutable:file id="tools"\nenabled = ["search"]\n'
    )
    _write(src / "README.md", "# an unmarked neighbour\n")

    pre = enumerate_mutations([src])
    assert {p.id for p in pre} == {"researcher_brief", "retry_policy", "tools"}
    assert {p.file.suffix for p in pre} == {".md", ".yaml", ".toml"}

    patches = [
        _replace("researcher_brief", "Research {topic} exhaustively; cite every claim.\n"),
        _replace("retry_policy", "retries: 7\n"),
        _replace("tools", '# zicato:mutable:file id="tools"\nenabled = ["search", "browse"]\n'),
    ]
    assert validate_patches(patches, enumeration=pre) == []

    apply_patches(src, patches, dst)

    assert validate_post_apply(dst, patches, pre) == []
    post = {p.id: p for p in enumerate_mutations([dst])}
    assert post["researcher_brief"].content == "Research {topic} exhaustively; cite every claim.\n"
    assert post["retry_policy"].content == "  retries: 7\n"
    assert "browse" in post["tools"].content
    # Unmarked neighbours untouched; the source tree never mutated.
    assert "owner: operator" in (dst / "config" / "runtime.yaml").read_text(encoding="utf-8")
    assert "retries: 3" in (src / "config" / "runtime.yaml").read_text(encoding="utf-8")


def test_required_placeholders_survive_the_text_path(tmp_path: Path) -> None:
    """A3 is format-agnostic and still fires on a markdown region."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(
        src / "prompt.md",
        """
        <!-- zicato:mutable:code id="brief" required_placeholders="{topic}" -->
        Research {topic}.
        <!-- zicato:mutable:end -->
        """,
    )
    pre = enumerate_mutations([src])
    patch = _replace("brief", "Research the thing.\n")
    apply_patches(src, [patch], dst)
    problems = validate_post_apply(dst, [patch], pre)
    assert any("required placeholder '{topic}'" in problem for problem in problems)


def test_set_numeric_is_rejected_against_a_text_region(tmp_path: Path) -> None:
    """Op/kind rules are reused unchanged: text points are file or code."""

    _write(
        tmp_path / "config.yaml",
        '# zicato:mutable:code id="retry"\nretries: 3\n# zicato:mutable:end\n',
    )
    patch = Patch(
        id="p",
        mutation_id="retry",
        op="set_numeric",
        new_content=None,
        new_numeric=9.0,
        new_enum=None,
        rationale="test",
    )
    problems = validate_patches([patch], source_root=tmp_path)
    assert any("incompatible with mutation point" in problem for problem in problems)
