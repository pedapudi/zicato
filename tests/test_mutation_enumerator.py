"""Tests for ``zicato.mutation.enumerator``."""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.markers import parse_marker_line


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_parse_marker_span_basic() -> None:
    parsed = parse_marker_line('# zicato:mutable id="foo"')
    assert parsed is not None
    assert parsed.id == "foo"
    assert parsed.is_file is False
    assert parsed.metadata == {}


def test_parse_marker_file_basic() -> None:
    parsed = parse_marker_line('# zicato:mutable:file id="all_prompts"')
    assert parsed is not None
    assert parsed.id == "all_prompts"
    assert parsed.is_file is True


def test_parse_marker_with_metadata() -> None:
    parsed = parse_marker_line('# zicato:mutable id="refine" required_placeholders="{a},{b}"')
    assert parsed is not None
    assert parsed.id == "refine"
    assert parsed.metadata == {"required_placeholders": "{a},{b}"}


def test_parse_marker_non_marker() -> None:
    assert parse_marker_line("# just a comment") is None
    assert parse_marker_line("x = 1") is None
    assert parse_marker_line("") is None


def test_parse_marker_regex_edge_cases() -> None:
    """Lock the marker regex's accept/reject boundary.

    The regex MUST accept indentation and inline spacing, and MUST reject
    markers with an unquoted ``id`` or no id at all — a silently
    mis-parsed marker is how a bad edit could land at the wrong span.
    """

    # Leading indentation is accepted (markers sit inside functions/classes).
    indented = parse_marker_line('    # zicato:mutable id="nested"')
    assert indented is not None and indented.id == "nested"

    # Extra spacing around the ``#`` and the keyword is tolerated.
    spaced = parse_marker_line('#   zicato:mutable   id="spaced"')
    assert spaced is not None and spaced.id == "spaced"

    # File marker with metadata parses both the file flag and the tail.
    filed = parse_marker_line('# zicato:mutable:file id="fid" role="system_prompt"')
    assert filed is not None
    assert filed.is_file is True
    assert filed.id == "fid"
    assert filed.metadata == {"role": "system_prompt"}

    # Rejected: an id with no quotes.
    assert parse_marker_line("# zicato:mutable id=unquoted") is None
    # Rejected: the keyword with no id at all.
    assert parse_marker_line("# zicato:mutable") is None
    # Rejected: a near-miss keyword.
    assert parse_marker_line('# zicato:mutate id="x"') is None
    # Rejected: an empty id is not a usable mutation id.
    assert parse_marker_line('# zicato:mutable id=""') is None


def test_parse_marker_first_id_binds_rest_is_metadata() -> None:
    """The ``id=`` capture binds the first quoted id; any later
    ``key="value"`` pairs land in metadata, not the id."""

    parsed = parse_marker_line('# zicato:mutable id="primary" language="markdown"')
    assert parsed is not None
    assert parsed.id == "primary"
    assert parsed.metadata == {"language": "markdown"}


def test_enumerate_single_span_assignment(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable id="researcher_instructions"
        RESEARCHER_INSTRUCTIONS = """Hello, world."""
        ''',
    )

    points = enumerate_mutations([root])
    assert len(points) == 1
    p = points[0]
    assert p.id == "researcher_instructions"
    assert p.kind == "span"
    assert p.file == file_path.resolve()
    assert "Hello, world." in p.content
    assert p.content_hash == hashlib.sha256(p.content.encode("utf-8")).hexdigest()
    assert p.line_start <= p.line_end


def test_enumerate_span_inside_keyword_arg(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "agent.py"
    _write(
        file_path,
        '''
        def make_agent():
            return dict(
                name="researcher",
                # zicato:mutable id="researcher_instructions"
                instruction="""You are a researcher.""",
            )
        ''',
    )

    points = enumerate_mutations([root])
    assert len(points) == 1
    p = points[0]
    assert p.id == "researcher_instructions"
    assert p.kind == "span"
    assert "You are a researcher." in p.content


def test_enumerate_file_marker(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable:file id="researcher_prompts"
        """Module-level prompts for the researcher agent."""

        FOO = "bar"
        BAZ = "qux"
        ''',
    )
    points = enumerate_mutations([root])
    assert len(points) == 1
    p = points[0]
    assert p.kind == "file"
    assert p.id == "researcher_prompts"
    assert p.line_start == 1
    # File-level span covers the whole file.
    assert p.content == file_path.read_text(encoding="utf-8")


def test_enumerate_metadata_preserved(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable id="refine_prompt" required_placeholders="{drift_kind},{plan_summary}"
        REFINE_PROMPT = """Drift: {drift_kind}. Plan: {plan_summary}."""
        ''',
    )
    points = enumerate_mutations([root])
    assert len(points) == 1
    assert points[0].metadata == {"required_placeholders": "{drift_kind},{plan_summary}"}


def test_enumerate_multiple_files_deterministic_order(tmp_path: Path) -> None:
    root = tmp_path / "src"
    a = root / "a.py"
    b = root / "b.py"
    sub_c = root / "sub" / "c.py"
    _write(
        a,
        """
        # zicato:mutable id="a_one"
        A_ONE = "alpha"

        # zicato:mutable id="a_two"
        A_TWO = "beta"
    """,
    )
    _write(
        b,
        '''
        # zicato:mutable:file id="b_whole"
        """Top of B."""
        B = "gamma"
    ''',
    )
    _write(
        sub_c,
        """
        # zicato:mutable id="c_one"
        C_ONE = "delta"
    """,
    )

    first = [p.id for p in enumerate_mutations([root])]
    second = [p.id for p in enumerate_mutations([root])]
    assert first == second, "enumeration order must be deterministic"
    # Same root, sorted by file then line.
    assert first == ["a_one", "a_two", "b_whole", "c_one"]


def test_enumerate_is_fully_deterministic_across_runs(tmp_path: Path) -> None:
    """Re-enumerating an unchanged tree yields byte-identical points.

    The applier re-enters the enumerator on every patch; if enumeration
    were non-deterministic an edit could resolve to a different span
    between the pre-validate pass and the apply pass. This test pins the
    whole identifying tuple — id, kind, line range, content, and content
    hash — across repeated calls.
    """

    root = tmp_path / "src"
    _write(
        root / "a.py",
        '''
        # zicato:mutable id="a_one" role="system_prompt"
        A_ONE = """alpha body"""

        # zicato:mutable id="a_two" required_placeholders="{x}"
        A_TWO = """beta {x}"""
    ''',
    )
    _write(
        root / "sub" / "b.py",
        '''
        # zicato:mutable:file id="b_whole"
        """Top of B."""
        B = "gamma"
    ''',
    )

    def _fingerprint() -> list[tuple]:
        return [
            (
                p.id,
                p.kind,
                str(p.file),
                p.line_start,
                p.line_end,
                p.content,
                p.content_hash,
                tuple(sorted(p.metadata.items())),
            )
            for p in enumerate_mutations([root])
        ]

    runs = [_fingerprint() for _ in range(4)]
    assert all(
        run == runs[0] for run in runs
    ), "enumeration must be byte-identical across repeated runs"
    # And the content_hash genuinely matches the content it describes.
    for p in enumerate_mutations([root]):
        assert p.content_hash == hashlib.sha256(p.content.encode("utf-8")).hexdigest()


def test_enumerate_marker_without_literal_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "broken.py"
    _write(
        file_path,
        """
        # zicato:mutable id="dangling"
        # nothing follows
        x = 1
        """,
    )
    points = enumerate_mutations([root])
    # Marker has no string literal target; enumerator finds zero string
    # spans after the comment and silently drops it.
    assert points == []


def test_enumerate_skips_unparseable_file(tmp_path: Path) -> None:
    root = tmp_path / "src"
    file_path = root / "broken.py"
    _write(
        file_path,
        """
        # zicato:mutable id="will_not_resolve"
        def foo(:::  # syntax error
        """,
    )
    # Best-effort: enumerator returns empty list for unparseable files.
    assert enumerate_mutations([root]) == []


def test_enumerate_missing_root_returns_empty(tmp_path: Path) -> None:
    assert enumerate_mutations([tmp_path / "does_not_exist"]) == []
