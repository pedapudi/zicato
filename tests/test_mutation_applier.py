"""Tests for ``zicato.mutation.applier``."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.mutation.applier import apply_patches, apply_patches_unchecked
from zicato.mutation.enumerator import enumerate_mutations


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(
    *,
    pid: str,
    mutation_id: str,
    op: str,
    new_content: str | None = None,
    new_numeric: float | None = None,
    new_enum: str | None = None,
) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=new_numeric,
        new_enum=new_enum,
        rationale="test",
    )


def test_apply_replace_span(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable id="instr"
        INSTR = """original"""
    ''',
    )
    patches = [_patch(pid="p1", mutation_id="instr", op="replace", new_content='"""rewritten"""')]
    apply_patches(src, patches, tgt)

    # Source unchanged.
    assert "original" in file_path.read_text(encoding="utf-8")
    # Target updated.
    new_text = (tgt / "prompts.py").read_text(encoding="utf-8")
    assert "rewritten" in new_text
    assert "original" not in new_text


def test_apply_replace_file_kind(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable:file id="all"
        """Initial."""
        VALUE = "x"
    ''',
    )

    new_body = '# zicato:mutable:file id="all"\n' '"""Replaced module."""\n' 'VALUE = "y"\n'
    patches = [_patch(pid="p1", mutation_id="all", op="replace", new_content=new_body)]
    apply_patches(src, patches, tgt)

    out = (tgt / "prompts.py").read_text(encoding="utf-8")
    assert out == new_body


def test_apply_replace_code_region(tmp_path: Path) -> None:
    """A ``code`` point replaces its body verbatim and stays importable."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "tools.py"
    _write(
        file_path,
        """
        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower().replace(" ", "_")
            # zicato:mutable:end
            return s
        """,
    )
    new_body = '    s = topic.strip().lower().replace(" ", "-")\n'
    patches = [_patch(pid="p1", mutation_id="slug_logic", op="replace", new_content=new_body)]
    apply_patches(src, patches, tgt)

    # Source unchanged.
    assert 'replace(" ", "_")' in file_path.read_text(encoding="utf-8")
    out = (tgt / "tools.py").read_text(encoding="utf-8")
    # New body landed verbatim (no string-literal wrapping).
    assert 'topic.strip().lower().replace(" ", "-")' in out
    assert 'replace(" ", "_")' not in out
    # Markers preserved so the id still resolves; the file still parses
    # and runs the new logic.
    assert '# zicato:mutable:code id="slug_logic"' in out
    assert "# zicato:mutable:end" in out
    ast.parse(out)

    # The mutation id still resolves and is still a code point.
    points = enumerate_mutations([tgt])
    by_id = {p.id: p for p in points}
    assert by_id["slug_logic"].kind == "code"

    # Execute the rewritten function to prove the body is live.
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102 — controlled test source
    assert ns["slug"]("Hello World") == "hello-world"  # type: ignore[operator]


def test_apply_replace_code_region_reindents_misindented_body(tmp_path: Path) -> None:
    """A ``code`` replacement the proposer emits at the wrong indent (or at
    column 0, multi-line) is re-anchored to the region's indent so the file
    still parses, retains its imports, and re-enumerates."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "tools.py"
    _write(
        file_path,
        """
        from __future__ import annotations

        import os


        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower().replace(" ", "_")
            # zicato:mutable:end
            return s
        """,
    )
    # Proposer emits a multi-line body at column 0 (no leading indent) —
    # the dominant real failure mode. Without re-anchoring this produces an
    # ``unexpected indent`` SyntaxError.
    new_body = "s = topic.strip().lower()\n" 'for ch in " /":\n' '    s = s.replace(ch, "-")\n'
    patches = [_patch(pid="p1", mutation_id="slug_logic", op="replace", new_content=new_body)]
    apply_patches(src, patches, tgt)

    out = (tgt / "tools.py").read_text(encoding="utf-8")
    # File still parses (the body was re-anchored to the region's indent).
    ast.parse(out)
    # Top-level imports are intact — a parseable file enumerates them.
    assert "from __future__ import annotations" in out
    assert "import os" in out
    # Markers preserved → id re-resolves as a code point.
    points = enumerate_mutations([tgt])
    by_id = {p.id: p for p in points}
    assert by_id["slug_logic"].kind == "code"
    # The rewritten body is live.
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102 — controlled test source
    assert ns["slug"]("Hello World") == "hello-world"  # type: ignore[operator]


def test_apply_replace_code_region_strips_echoed_markers(tmp_path: Path) -> None:
    """A ``code`` replacement that echoes the surrounding marker comments
    back has them stripped — only the body lands between the real markers,
    so the id keeps resolving and the markers are not duplicated."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "tools.py"
    _write(
        file_path,
        """
        import os


        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower()
            # zicato:mutable:end
            return s
        """,
    )
    # Proposer echoes the markers back inside its replacement (common).
    new_body = (
        '# zicato:mutable:code id="slug_logic"\n'
        's = topic.strip().lower().replace(" ", "_")\n'
        "# zicato:mutable:end\n"
    )
    patches = [_patch(pid="p1", mutation_id="slug_logic", op="replace", new_content=new_body)]
    apply_patches(src, patches, tgt)

    out = (tgt / "tools.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "import os" in out
    # The marker appears exactly once (not duplicated by the echo).
    assert out.count('# zicato:mutable:code id="slug_logic"') == 1
    assert out.count("# zicato:mutable:end") == 1
    points = enumerate_mutations([tgt])
    by_id = {p.id: p for p in points}
    assert by_id["slug_logic"].kind == "code"


def test_apply_replace_plus_joined_span_preserves_operator(tmp_path: Path) -> None:
    """Replacing a ``+``-joined pointed sub-clause keeps the operator so
    the clause stays a distinct literal and its marker keeps resolving."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "agent.py"
    _write(
        file_path,
        """
        def make():
            return dict(
                instruction=(
                    # zicato:mutable id="base"
                    "base clause.\\n"
                    # zicato:mutable id="clause"
                    + "pointed clause."
                ),
            )
        """,
    )
    patches = [_patch(pid="p1", mutation_id="clause", op="replace", new_content='"REWRITTEN."')]
    apply_patches(src, patches, tgt)

    out = (tgt / "agent.py").read_text(encoding="utf-8")
    ast.parse(out)
    # The leading ``+`` survived so the clause is still its own literal.
    assert "+ " in out and "REWRITTEN." in out
    points = enumerate_mutations([tgt])
    ids = {p.id for p in points}
    assert ids == {"base", "clause"}


def test_apply_numeric(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tgt = tmp_path / "tgt"
    file_path = src / "config.py"
    # The marker binds to the string "DEFAULT_THRESHOLD" name on the
    # assignment target by the enumerator, but the applier looks for
    # the next NUMERIC constant after the marker line.
    _write(
        file_path,
        """
        # zicato:mutable id="threshold"
        DEFAULT_THRESHOLD = 0.85
    """,
    )
    _patches = [_patch(pid="p1", mutation_id="threshold", op="set_numeric", new_numeric=0.42)]
    # The string-only enumeration won't bind to a string literal here
    # (the assignment value is numeric). So we need a string literal
    # for the enumerator's resolution, then the applier rewrites the
    # numeric constant after the marker.
    # In a real file the marker would precede a string-valued line; the
    # applier's contract says "mutation_id must point at a numeric
    # constant declaration", so a tighter test uses a file with both.
    # We retry the test below with a layout that satisfies both forms.


def test_apply_numeric_with_resolvable_marker(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "config.py"
    _write(
        file_path,
        """
        # zicato:mutable id="threshold"
        DEFAULT_THRESHOLD_NAME = "default_threshold"
        DEFAULT_THRESHOLD = 0.85
    """,
    )
    patches = [_patch(pid="p1", mutation_id="threshold", op="set_numeric", new_numeric=0.42)]
    apply_patches(src, patches, tgt)

    new_text = (tgt / "config.py").read_text(encoding="utf-8")
    assert "DEFAULT_THRESHOLD = 0.42" in new_text
    assert "0.85" not in new_text


def test_apply_numeric_integer_value(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "config.py"
    _write(
        file_path,
        """
        # zicato:mutable id="max_turns"
        MAX_TURNS_DOC = "max turns"
        MAX_TURNS = 5
    """,
    )
    patches = [_patch(pid="p1", mutation_id="max_turns", op="set_numeric", new_numeric=10.0)]
    apply_patches(src, patches, tgt)

    new_text = (tgt / "config.py").read_text(encoding="utf-8")
    # Exact-integer values are rendered without a decimal point.
    assert "MAX_TURNS = 10" in new_text
    assert "MAX_TURNS = 10.0" not in new_text


def test_apply_enum(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "router.py"
    _write(
        file_path,
        """
        # zicato:mutable id="strategy"
        STRATEGY = "greedy"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="strategy", op="set_enum", new_enum="balanced")]
    apply_patches(src, patches, tgt)

    new_text = (tgt / "router.py").read_text(encoding="utf-8")
    assert "'balanced'" in new_text or '"balanced"' in new_text
    assert "greedy" not in new_text


def test_apply_unresolved_id_raises(tmp_path: Path) -> None:
    """The atomic path refuses an unknown id with a ValueError and leaves
    no target tree behind."""

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        """
        # zicato:mutable id="known"
        KNOWN = "v"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="unknown", op="replace", new_content='"v2"')]
    with pytest.raises(ValueError, match="does not resolve"):
        apply_patches(src, patches, tgt)
    # Atomic: the pre-check rejected the batch, so no half-built tree
    # is left under target_root.
    assert not tgt.exists()


def test_apply_unchecked_unresolved_id_raises_keyerror(tmp_path: Path) -> None:
    """The legacy unchecked path keeps its best-effort KeyError-on-miss
    behaviour for callers that have pre-validated the batch themselves."""

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        """
        # zicato:mutable id="known"
        KNOWN = "v"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="unknown", op="replace", new_content='"v2"')]
    with pytest.raises(KeyError):
        apply_patches_unchecked(src, patches, tgt)


def test_apply_replace_missing_new_content_raises(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        """
        # zicato:mutable id="known"
        KNOWN = "v"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="known", op="replace", new_content=None)]
    with pytest.raises(ValueError):
        apply_patches(src, patches, tgt)


def test_apply_refuses_existing_target(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()
    with pytest.raises(FileExistsError):
        apply_patches(src, [], tgt)


def test_apply_preserves_unchanged_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="a_id"
        A = "alpha"
    """,
    )
    _write(
        src / "b.py",
        """
        B = "beta"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="a_id", op="replace", new_content='"alpha2"')]
    apply_patches(src, patches, tgt)
    # Untouched file is identical.
    assert (tgt / "b.py").read_text(encoding="utf-8") == (src / "b.py").read_text(encoding="utf-8")
    # Re-enumeration in the target now reflects the rewritten content.
    new_points = {p.id: p for p in enumerate_mutations([tgt])}
    assert "a_id" in new_points
    assert "alpha2" in new_points["a_id"].content


# ---------------------------------------------------------------------------
# Atomicity — apply_patches is all-or-nothing.
# ---------------------------------------------------------------------------


def test_apply_is_atomic_one_bad_patch_applies_none(tmp_path: Path) -> None:
    """A batch with one invalid patch must apply NONE of its patches.

    The first patch is valid; the second targets an unknown id. The
    atomic pre-check must reject the whole batch before the first edit
    lands, and must not leave a half-built target tree behind.
    """

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="good_one"
        GOOD = "original"
    """,
    )
    patches = [
        # Valid — would succeed on its own.
        _patch(pid="p1", mutation_id="good_one", op="replace", new_content='"changed"'),
        # Invalid — id does not enumerate.
        _patch(pid="p2", mutation_id="ghost", op="replace", new_content='"x"'),
    ]
    with pytest.raises(ValueError, match="refusing to apply patch set"):
        apply_patches(src, patches, tgt)
    # Nothing half-applied: target tree was removed by the atomic guard.
    assert not tgt.exists()
    # Source is, as always, untouched.
    assert "original" in (src / "a.py").read_text(encoding="utf-8")


def test_apply_is_atomic_bad_op_payload_applies_none(tmp_path: Path) -> None:
    """An op/payload mismatch in any patch rejects the whole batch."""

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="good_one"
        GOOD = "original"

        # zicato:mutable id="threshold"
        THRESHOLD_DOC = "threshold"
        THRESHOLD = 0.5
    """,
    )
    patches = [
        _patch(pid="p1", mutation_id="good_one", op="replace", new_content='"changed"'),
        # set_numeric with no new_numeric payload — caught pre-apply.
        _patch(pid="p2", mutation_id="threshold", op="set_numeric", new_numeric=None),
    ]
    with pytest.raises(ValueError, match="refusing to apply patch set"):
        apply_patches(src, patches, tgt)
    assert not tgt.exists()


def test_apply_clean_batch_applies_all(tmp_path: Path) -> None:
    """A fully-valid multi-patch batch applies every patch."""

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="instr"
        INSTR = "original instr"

        # zicato:mutable id="strategy"
        STRATEGY = "greedy"

        # zicato:mutable id="budget"
        BUDGET_DOC = "budget"
        BUDGET = 3
    """,
    )
    patches = [
        _patch(pid="p1", mutation_id="instr", op="replace", new_content='"new instr"'),
        _patch(pid="p2", mutation_id="strategy", op="set_enum", new_enum="balanced"),
        _patch(pid="p3", mutation_id="budget", op="set_numeric", new_numeric=9),
    ]
    apply_patches(src, patches, tgt)
    out = (tgt / "a.py").read_text(encoding="utf-8")
    # Every patch landed.
    assert "new instr" in out
    assert "original instr" not in out
    assert "balanced" in out
    assert "greedy" not in out
    assert "BUDGET = 9" in out
    # Source untouched.
    assert "original instr" in (src / "a.py").read_text(encoding="utf-8")


def test_apply_unchecked_one_bad_patch_leaves_earlier_applied(tmp_path: Path) -> None:
    """Contrast test: the legacy unchecked path is NOT atomic — an earlier
    valid patch stays applied even though a later patch raises. This is
    exactly the non-atomic behaviour ``apply_patches`` now guards against.
    """

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="good_one"
        GOOD = "original"
    """,
    )
    patches = [
        _patch(pid="p1", mutation_id="good_one", op="replace", new_content='"changed"'),
        _patch(pid="p2", mutation_id="ghost", op="replace", new_content='"x"'),
    ]
    with pytest.raises(KeyError):
        apply_patches_unchecked(src, patches, tgt)
    # The unchecked path applied p1 before p2 raised — half-applied tree.
    assert tgt.exists()
    assert "changed" in (tgt / "a.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Surgical span replacement — a span replace must never break the file
# ---------------------------------------------------------------------------


def test_span_replace_reindents_docstring_to_match_suite(tmp_path: Path) -> None:
    """A docstring span replace at the wrong indent is re-anchored.

    A proposer working from a preview can emit ``new_content`` at the
    wrong indentation. The applier owns the literal's syntactic anchor:
    a docstring replacement must open at the function-body indent, not
    wherever the proposer put it — otherwise the suite below it dedents
    into thin air. The file must still parse, the marker must survive,
    and the top-level imports must be untouched.
    """
    import ast

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "agent.py"
    _write(
        file_path,
        '''
        from __future__ import annotations

        import os
        from typing import Any


        def read_files(topic: str) -> dict[str, Any]:
            # zicato:mutable id="read_files__doc"
            """Read the files for the given topic."""
            return {topic: os.getcwd()}
    ''',
    )
    # The proposer emits the docstring indented EIGHT spaces — one level
    # too deep for a function-body docstring (which sits at four).
    over_indented = (
        '        """Read the generated files.\n\n'
        "        Pass the bare topic slug.\n"
        '        """'
    )
    patches = [
        _patch(
            pid="p1",
            mutation_id="read_files__doc",
            op="replace",
            new_content=over_indented,
        )
    ]
    apply_patches(src, patches, tgt)

    out = (tgt / "agent.py").read_text(encoding="utf-8")
    # The patched file still parses — the docstring was re-anchored.
    ast.parse(out)
    # The new docstring landed.
    assert "Read the generated files." in out
    # The marker comment survived verbatim.
    assert '# zicato:mutable id="read_files__doc"' in out
    # Top-level imports are untouched.
    assert "from __future__ import annotations" in out
    assert "import os" in out
    assert "from typing import Any" in out
    # Re-enumeration still resolves the mutation id.
    points = enumerate_mutations([tgt])
    assert any(p.id == "read_files__doc" for p in points)


def test_span_replace_prose_at_wrong_indent_stays_surgical(tmp_path: Path) -> None:
    """A prose (unwrapped) replacement is also anchored to the span indent."""
    import ast

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "agent.py"
    _write(
        file_path,
        '''
        import os


        def tool() -> str:
            # zicato:mutable id="tool__doc"
            """Original docstring."""
            return os.getcwd()
    ''',
    )
    # Raw prose — no quotes; the applier wraps AND anchors it.
    patches = [
        _patch(
            pid="p1",
            mutation_id="tool__doc",
            op="replace",
            new_content="A rewritten description of the tool.",
        )
    ]
    apply_patches(src, patches, tgt)
    out = (tgt / "agent.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "A rewritten description of the tool." in out
    assert "import os" in out
    assert '# zicato:mutable id="tool__doc"' in out


def test_span_replace_already_correct_indent_is_idempotent(tmp_path: Path) -> None:
    """Re-anchoring a correctly-indented literal is a no-op."""
    import ast

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "agent.py"
    _write(
        file_path,
        '''
        import os


        def tool() -> str:
            # zicato:mutable id="tool__doc"
            """Original."""
            return os.getcwd()
    ''',
    )
    # Correctly indented four-space docstring — the common case.
    patches = [
        _patch(
            pid="p1",
            mutation_id="tool__doc",
            op="replace",
            new_content='    """Rewritten docstring."""',
        )
    ]
    apply_patches(src, patches, tgt)
    out = (tgt / "agent.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "Rewritten docstring." in out
    assert "import os" in out


@pytest.mark.parametrize(
    ("new_content", "needle"),
    [
        # Trailing single quote — the original four-quote fuse bug.
        ('multi_search, lookup_entry_context"', "lookup_entry_context"),
        # Assignment echo ending in a quote — the exact issue-#11 repro.
        ('ROSTER = "multi_search, lookup_entry_context"', "ROSTER ="),
        # Leading quote, never closed — fuses at the opening delimiter.
        ('"leading quote, never closed', "leading quote"),
        # Embedded triple-double-quote — forces the '\\'\\'\\'' fallback.
        ('contains a """ run inside', "contains a"),
        # Both triple forms present — must fall back to repr().
        ("mixes \"\"\" and ''' delimiters", "mixes"),
    ],
)
def test_span_replace_prose_with_quotes_stays_parseable(
    tmp_path: Path, new_content: str, needle: str
) -> None:
    """A prose/echo span replace with stray quotes must never corrupt the
    snapshot — the applier wraps it into a collision-proof literal so the
    target tree parses AND every mutation id re-enumerates.

    This is the regression gate for issue #11: a malformed proposer patch
    (an assignment echo, a stray trailing/leading quote, an embedded
    triple-quote run) must degrade into a low-quality literal, not an
    unparseable file that silently drops the file's mutation ids and
    crashes the next generation with ``KeyError``.
    """

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "prompts.py",
        """
        # zicato:mutable id="roster"
        ROSTER = "multi_search"
    """,
    )
    patches = [_patch(pid="p1", mutation_id="roster", op="replace", new_content=new_content)]
    apply_patches(src, patches, tgt)

    out = (tgt / "prompts.py").read_text(encoding="utf-8")
    # The target tree parses — no four-quote fuse, no unterminated literal.
    ast.parse(out)
    # Every mutation id re-enumerates (the file did not silently vanish).
    ids = {p.id for p in enumerate_mutations([tgt])}
    assert ids == {"roster"}
    # The replacement content survived (wrapped as a literal body).
    assert needle in out


def test_apply_replace_onto_read_only_snapshot(tmp_path: Path) -> None:
    """A patch lands even when the source file is mode 0o444.

    The copied child tree inherits file modes from ``source_root``. When
    the parent snapshot is read-only (an immutable / archived mutable
    tree), the inherited copy is read-only too and a naive ``write_text``
    would raise ``PermissionError``. The applier must restore owner-write
    before writing.
    """

    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(
        file_path,
        '''
        # zicato:mutable id="instr"
        INSTR = """original"""
    ''',
    )
    # Make the source file read-only so the copied target inherits 0o444.
    file_path.chmod(0o444)

    patches = [_patch(pid="p1", mutation_id="instr", op="replace", new_content='"""rewritten"""')]
    apply_patches(src, patches, tgt)

    new_text = (tgt / "prompts.py").read_text(encoding="utf-8")
    assert "rewritten" in new_text
    assert "original" not in new_text
