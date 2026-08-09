"""The mutation surface on non-Python files.

Covers the text marker grammar, the text discovery pass, the containment
properties of a region point, and the end-to-end
enumerate -> validate -> apply -> re-enumerate loop against a fixture tree
whose entire mutable surface is a markdown prompt and a YAML config.

The ``.py`` byte-identity pin lives here too (see
:func:`test_python_enumeration_is_byte_identical_to_the_pinned_golden`):
widening the surface must move ZERO points on the historical Python path.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.epoch.preflight import degraded_patch_for
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import (
    MAX_TEXT_FILE_BYTES,
    TEXT_FILE_SUFFIXES,
    enumerate_mutations,
    is_text_mutation_candidate,
)
from zicato.mutation.formats import (
    _CHECKERS,
    FORMAT_NEUTRAL_CONTENT,
    format_problem,
)
from zicato.mutation.markers import (
    is_end_marker,
    is_grading_marker,
    marker_syntax_for,
    parse_marker_line,
)
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
# Marker grammar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        '# zicato:mutable:file id="x"',
        '// zicato:mutable:file id="x"',
        '<!-- zicato:mutable:file id="x" -->',
        '/* zicato:mutable:file id="x" */',
        '; zicato:mutable:file id="x"',
        '-- zicato:mutable:file id="x"',
        '% zicato:mutable:file id="x"',
        '    <!-- zicato:mutable:file id="x" -->   ',
    ],
)
def test_text_syntax_accepts_every_documented_leader(line: str) -> None:
    parsed = parse_marker_line(line, syntax="text")
    assert parsed is not None
    assert parsed.id == "x"
    assert parsed.is_file is True
    # A block-comment closer lands in the metadata tail and contributes
    # no key="value" pair, so it never leaks into MutationPoint.metadata.
    assert parsed.metadata == {}


def test_text_syntax_preserves_metadata_alongside_a_closer() -> None:
    parsed = parse_marker_line(
        '<!-- zicato:mutable:code id="brief" required_placeholders="{topic}" -->',
        syntax="text",
    )
    assert parsed is not None
    assert parsed.is_code is True
    assert parsed.metadata == {"required_placeholders": "{topic}"}


def test_python_syntax_rejects_non_hash_leaders() -> None:
    """The default syntax is the historical ``#``-only grammar."""

    for line in ('// zicato:mutable id="x"', '<!-- zicato:mutable id="x" -->'):
        assert parse_marker_line(line) is None
        assert parse_marker_line(line, syntax="python") is None


def test_markdown_bullet_is_not_a_marker_leader() -> None:
    """``*`` is deliberately excluded — every markdown bullet would match."""

    assert parse_marker_line('* zicato:mutable:file id="x"', syntax="text") is None


def test_end_sentinel_tolerates_a_block_comment_closer() -> None:
    assert is_end_marker("<!-- zicato:mutable:end -->", syntax="text") is True
    assert is_end_marker("/* zicato:mutable:end */", syntax="text") is True
    assert is_end_marker("// zicato:mutable:end", syntax="text") is True
    # Still not an opening marker.
    assert parse_marker_line("<!-- zicato:mutable:end -->", syntax="text") is None
    # And the python grammar is unchanged: no closer, no foreign leader.
    assert is_end_marker("<!-- zicato:mutable:end -->") is False
    assert is_end_marker("# zicato:mutable:end") is True


def test_marker_syntax_for_maps_only_py_to_python() -> None:
    assert marker_syntax_for(Path("a/b.py")) == "python"
    assert marker_syntax_for(Path("a/b.md")) == "text"
    assert marker_syntax_for(Path("a/b.yaml")) == "text"
    assert marker_syntax_for(Path("Dockerfile")) == "text"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discovery_allowlist_shape() -> None:
    assert is_text_mutation_candidate(Path("prompt.md")) is True
    assert is_text_mutation_candidate(Path("config.yaml")) is True
    assert is_text_mutation_candidate(Path("Dockerfile")) is True
    # .py belongs to the Python pass, never the text pass.
    assert is_text_mutation_candidate(Path("mod.py")) is False
    # Binaries and unlisted suffixes are refused without opening the file.
    assert is_text_mutation_candidate(Path("logo.png")) is False
    assert is_text_mutation_candidate(Path("model.safetensors")) is False
    assert is_text_mutation_candidate(Path("notes")) is False
    # A format with no comment syntax cannot host a marker.
    assert ".csv" not in TEXT_FILE_SUFFIXES


def test_binary_file_is_never_read_by_the_walk(tmp_path: Path) -> None:
    (tmp_path / "blob.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00zicato:mutable")
    _write(tmp_path / "prompt.md", '<!-- zicato:mutable:file id="p" -->\nhello\n')
    ids = {p.id for p in enumerate_mutations([tmp_path])}
    assert ids == {"p"}


def test_oversized_text_file_is_skipped(tmp_path: Path) -> None:
    big = tmp_path / "huge.jsonl"
    big.write_text(
        '# zicato:mutable:file id="huge"\n' + ("x" * (MAX_TEXT_FILE_BYTES + 10)),
        encoding="utf-8",
    )
    assert enumerate_mutations([tmp_path]) == []


def test_vendored_dirs_are_pruned_from_the_text_pass_only(tmp_path: Path) -> None:
    _write(tmp_path / "node_modules" / "pkg" / "x.json", '{"a": 1}\n')
    _write(
        tmp_path / "node_modules" / "pkg" / "prompt.md",
        '<!-- zicato:mutable:file id="vendored" -->\nhi\n',
    )
    # The Python pass keeps its historical reach: a marker under a pruned
    # directory name still enumerates from a .py file.
    _write(
        tmp_path / "node_modules" / "pkg" / "mod.py",
        """
        # zicato:mutable id="vendored_py"
        X = "hello"
        """,
    )
    ids = {p.id for p in enumerate_mutations([tmp_path])}
    assert "vendored" not in ids
    assert "vendored_py" in ids


def test_single_file_root_non_py_now_enumerates(tmp_path: Path) -> None:
    """The silent-zero case the widening turns into real surface."""

    prompt = tmp_path / "prompt.md"
    _write(prompt, '<!-- zicato:mutable:file id="single" -->\nbody\n')
    points = enumerate_mutations([prompt])
    assert [p.id for p in points] == ["single"]
    assert points[0].source_root == tmp_path


def test_single_file_root_with_unwalkable_suffix_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blob = tmp_path / "weights.bin"
    blob.write_bytes(b"\x00\x01")
    with caplog.at_level(logging.WARNING, logger="zicato.mutation.enumerator"):
        assert enumerate_mutations([blob]) == []
    assert "contributes no mutation points" in caplog.text


# --------------------------------------------------------------------------
# Enumeration of text points
# --------------------------------------------------------------------------


def test_markdown_region_body_excludes_the_marker_lines(tmp_path: Path) -> None:
    _write(
        tmp_path / "prompt.md",
        """
        # Researcher brief

        <!-- zicato:mutable:code id="researcher_brief" role="prompt" -->
        Answer the question in three sentences.
        Cite every claim.
        <!-- zicato:mutable:end -->

        (trailing prose)
        """,
    )
    (point,) = enumerate_mutations([tmp_path])
    assert point.id == "researcher_brief"
    assert point.kind == "code"
    assert point.metadata == {"role": "prompt"}
    assert point.content == "Answer the question in three sentences.\nCite every claim.\n"
    assert "zicato:mutable" not in point.content


def test_yaml_region_and_file_markers(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yaml",
        """
        service:
          # zicato:mutable:code id="retry_policy"
          retries: 3
          backoff_seconds: 1.5
          # zicato:mutable:end
          name: fixed
        """,
    )
    (point,) = enumerate_mutations([tmp_path])
    assert point.kind == "code"
    assert point.content == "  retries: 3\n  backoff_seconds: 1.5\n"


def test_bare_span_marker_in_a_text_file_warns_and_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(
        tmp_path / "config.yaml",
        """
        # zicato:mutable id="temperature"
        temperature: 0.7
        """,
    )
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


def test_python_and_text_points_compose_in_one_enumeration(tmp_path: Path) -> None:
    _write(
        tmp_path / "agent.py",
        """
        # zicato:mutable id="instruction"
        INSTRUCTION = "be helpful"
        """,
    )
    _write(tmp_path / "prompt.md", '<!-- zicato:mutable:file id="prompt" -->\nhi\n')
    _write(
        tmp_path / "config.yaml",
        '# zicato:mutable:code id="cfg"\nretries: 2\n# zicato:mutable:end\n',
    )
    points = {p.id: p for p in enumerate_mutations([tmp_path])}
    assert set(points) == {"instruction", "prompt", "cfg"}
    assert points["instruction"].kind == "span"
    assert points["prompt"].kind == "file"
    assert points["cfg"].kind == "code"


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


#: The full enumeration of ``_python_fixture_tree``, captured from the
#: pre-widening enumerator and committed here verbatim. This is the pin:
#: it is an EQUALITY assertion over the whole point set, not a membership
#: check, so a widening that adds, drops, reorders, or reshapes a single
#: Python point turns it red.
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


def test_python_enumeration_is_byte_identical_to_the_pinned_golden(tmp_path: Path) -> None:
    _python_fixture_tree(tmp_path)
    points = enumerate_mutations([tmp_path])
    actual = tuple(
        (p.id, p.kind, p.file.relative_to(tmp_path).as_posix(), p.line_start, p.line_end)
        for p in points
    )
    assert actual == _PY_GOLDEN


def test_python_enumeration_is_unaffected_by_neighbouring_text_files(tmp_path: Path) -> None:
    """Adding text surface must not perturb any Python point.

    The stronger half of the pin: enumerate the same Python tree twice,
    once alone and once beside markdown / YAML / JSON files carrying their
    own markers, and require the Python points to be identical field for
    field — content and content_hash included.
    """

    bare = tmp_path / "bare"
    mixed = tmp_path / "mixed"
    _python_fixture_tree(bare)
    _python_fixture_tree(mixed)
    _write(mixed / "prompt.md", '<!-- zicato:mutable:file id="md" -->\nbody\n')
    _write(mixed / "cfg.yaml", '# zicato:mutable:code id="yml"\na: 1\n# zicato:mutable:end\n')
    _write(mixed / "data.toml", "a = 1\n")
    _write(mixed / "pkg" / "notes.txt", "no markers here\n")

    def normalise(root: Path) -> list[dict[str, object]]:
        rows = []
        for point in enumerate_mutations([root]):
            row = asdict(point)
            row["file"] = point.file.relative_to(root).as_posix()
            row["source_root"] = "<root>"
            rows.append(row)
        return rows

    bare_rows = normalise(bare)
    mixed_rows = [row for row in normalise(mixed) if str(row["file"]).endswith(".py")]
    assert mixed_rows == bare_rows
    assert len(bare_rows) == len(_PY_GOLDEN)


# --------------------------------------------------------------------------
# Containment: a patch cannot escape the region or eat its markers
# --------------------------------------------------------------------------


def _region_tree(root: Path) -> None:
    _write(
        root / "prompt.md",
        """
        # Header the proposer must not touch

        <!-- zicato:mutable:code id="brief" -->
        original brief
        <!-- zicato:mutable:end -->

        Footer the proposer must not touch
        """,
    )


def test_region_patch_cannot_escape_its_markers(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _region_tree(src)
    apply_patches(src, [_replace("brief", "rewritten brief\n")], dst)
    text = (dst / "prompt.md").read_text(encoding="utf-8")
    assert "# Header the proposer must not touch" in text
    assert "Footer the proposer must not touch" in text
    assert "original brief" not in text
    assert "rewritten brief" in text
    # Both anchors survive, so the id still resolves for the next round.
    assert text.count("zicato:mutable:code") == 1
    assert text.count("zicato:mutable:end") == 1
    assert {p.id for p in enumerate_mutations([dst])} == {"brief"}


def test_region_patch_cannot_eat_its_own_markers(tmp_path: Path) -> None:
    """A proposer echoing the markers back has them stripped, not honoured."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _region_tree(src)
    hostile = (
        "<!-- zicato:mutable:end -->\n"
        "escaped content\n"
        '<!-- zicato:mutable:code id="hijacked" -->\n'
        "more\n"
    )
    apply_patches(src, [_replace("brief", hostile)], dst)
    text = (dst / "prompt.md").read_text(encoding="utf-8")
    assert text.count("zicato:mutable:code") == 1
    assert text.count("zicato:mutable:end") == 1
    assert "hijacked" not in text
    assert "escaped content" in text
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
    problems = validate_post_apply(dst, [patch], pre)
    assert any("no longer resolves" in problem for problem in problems)


def test_region_replace_preserves_relative_indentation(tmp_path: Path) -> None:
    """Nested YAML survives the dedent/re-anchor round trip."""

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
    apply_patches(
        src,
        # The proposer emits at column 0, as it routinely does.
        [_replace("retry", "retries: 5\nnested:\n  backoff: 2\n")],
        dst,
    )
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
# Post-apply format gate
# --------------------------------------------------------------------------


_TOML_SRC = '# zicato:mutable:file id="cfg"\nretries = 3\n'


def test_format_gate_rejects_a_whole_file_replace_that_breaks_toml(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "cfg.toml", _TOML_SRC)
    with pytest.raises(ValueError, match="post-apply format problem"):
        apply_patches(
            src, [_replace("cfg", '# zicato:mutable:file id="cfg"\nretries = = =\n')], dst
        )
    assert not dst.exists()


def test_format_gate_accepts_a_valid_toml_rewrite(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "cfg.toml", _TOML_SRC)
    apply_patches(src, [_replace("cfg", '# zicato:mutable:file id="cfg"\nretries = 99\n')], dst)
    assert "retries = 99" in (dst / "cfg.toml").read_text(encoding="utf-8")


def test_format_gate_does_not_fail_a_file_that_was_already_malformed(tmp_path: Path) -> None:
    """The gate catches breakage, never pre-existing breakage."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "cfg.toml", '# zicato:mutable:file id="cfg"\nnot = = toml\n')
    apply_patches(src, [_replace("cfg", '# zicato:mutable:file id="cfg"\nstill = = broken\n')], dst)
    assert "still = = broken" in (dst / "cfg.toml").read_text(encoding="utf-8")


def test_json_cannot_host_a_marker_and_is_not_walked() -> None:
    """The documented reason the format registry has no JSON entry."""

    assert is_text_mutation_candidate(Path("data.json")) is False
    assert is_text_mutation_candidate(Path("data.jsonl")) is False
    # The comment-bearing dialects do work.
    assert is_text_mutation_candidate(Path("tsconfig.jsonc")) is True
    assert is_text_mutation_candidate(Path("data.json5")) is True


def test_every_format_checked_suffix_has_a_neutral_degradation() -> None:
    """Pre-flight must always be able to degrade a gated file legally."""

    for suffix in _CHECKERS:
        assert suffix in FORMAT_NEUTRAL_CONTENT
        assert format_problem(Path(f"x{suffix}"), FORMAT_NEUTRAL_CONTENT[suffix]) is None


def test_preflight_degradation_of_a_gated_file_survives_the_gate(tmp_path: Path) -> None:
    """The interaction the neutral-content table exists to protect.

    A whole-file pre-flight probe on a ``.toml`` point must APPLY (and be
    measured as a degradation), not be rejected as a malformed patch.
    """

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "cfg.toml", _TOML_SRC)
    (point,) = enumerate_mutations([src])
    apply_patches(src, [degraded_patch_for(point)], dst)
    assert "retries" not in (dst / "cfg.toml").read_text(encoding="utf-8")


def test_format_gate_leaves_region_patches_alone(tmp_path: Path) -> None:
    """A region patch is a fragment; the gate does not adjudicate it."""

    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(
        src / "cfg.toml",
        '# zicato:mutable:code id="section"\nname = "a"\n# zicato:mutable:end\n',
    )
    apply_patches(src, [_replace("section", "this is not toml\n")], dst)
    assert "this is not toml" in (dst / "cfg.toml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_end_to_end_markdown_and_yaml_surface(tmp_path: Path) -> None:
    """enumerate -> validate -> apply -> re-enumerate on a text-only tree."""

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
    # Unmarked neighbours of every shape, including a binary the walk must
    # never open and a Python module that carries no markers.
    _write(src / "README.md", "# no markers here\n")
    (src / "assets" / "logo.png").parent.mkdir(parents=True, exist_ok=True)
    (src / "assets" / "logo.png").write_bytes(b"\x89PNG\x00\x00zicato:mutable:file")

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
    assert set(post) == {"researcher_brief", "retry_policy", "tools"}
    assert post["researcher_brief"].content == (
        "Research {topic} exhaustively; cite every claim.\n"
    )
    assert post["retry_policy"].content == "  retries: 7\n"
    assert "browse" in post["tools"].content
    # Unmarked neighbours are untouched.
    assert "owner: operator" in (dst / "config" / "runtime.yaml").read_text(encoding="utf-8")
    # The source tree is never mutated.
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
    """Op/kind rules still hold: text points are ``file`` or ``code`` only."""

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


def test_grading_marker_helper_is_syntax_aware() -> None:
    assert is_grading_marker("<!-- zicato:grading -->", syntax="text") is True
    assert is_grading_marker("<!-- zicato:grading -->") is False
    assert is_grading_marker("# zicato:grading") is True
