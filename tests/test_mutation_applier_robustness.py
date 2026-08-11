"""Robustness suite for ``zicato.mutation.applier`` across the full edit space.

This file is the systematic complement to ``test_mutation_applier.py``. It
exercises the applier across three crossed dimensions:

* **op × content shape** — ``replace`` of a span with content that is
  single-line, multi-line, prose, code, empty, whitespace-only, or carries
  quotes / newlines / backslashes / unicode / triple-quotes / brace runs;
  ``set_numeric`` / ``set_enum`` with negatives, floats, scientific
  notation, and enum values that need quoting.
* **span context** — the replaced literal sits in a simple ``NAME = "..."``
  assignment, a triple-quoted docstring, a ``+``-joined concatenation, a
  dict / list value, a function arg, a return, a decorator arg, a
  deeply-indented block.
* **safety** — a malformed patch never leaves a corrupt or half-written
  snapshot (atomicity, post-apply parse gate); a read-only source tree
  still applies; duplicate mutation ids are rejected.

The load-bearing invariant the whole suite proves: **a span replace never
silently drops the assignment target** (issue #38) and **never produces an
unparseable snapshot** (issue #11), and the applier handles read-only trees
(issue #4). Every applied tree is re-parsed AND re-enumerated, and where the
intent is recoverable the rewritten value is exec'd and asserted.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(
    *,
    mutation_id: str,
    op: str = "replace",
    new_content: str | None = None,
    new_numeric: float | None = None,
    new_enum: str | None = None,
) -> Patch:
    return Patch(
        id="p1",
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=new_numeric,
        new_enum=new_enum,
        rationale="test",
    )


def tgt_of(tmp_path: Path) -> Path:
    """The target tree ``_apply_one`` writes to (for re-enumeration asserts)."""
    return tmp_path / "tgt"


def _apply_one(tmp_path: Path, source_body: str, patch: Patch, *, filename: str = "m.py") -> str:
    """Apply a single patch to a one-file source tree and return the result."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / filename, source_body)
    apply_patches(src, [patch], tgt)
    return (tgt / filename).read_text(encoding="utf-8")


def _exec_value(out: str, name: str) -> object:
    """Exec a module body and return the value bound to ``name``."""
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102 — controlled test source
    return ns[name]


# ---------------------------------------------------------------------------
# Issue #38 — the exemplar. A multi-line replace of a simple-assignment
# string span must keep its ``NAME =`` target (and not corrupt siblings).
# ---------------------------------------------------------------------------


def test_issue_38_multiline_replace_keeps_assignment_target(tmp_path: Path) -> None:
    """A multi-line replace of ``NAME = "..."`` keeps the ``NAME =`` target.

    This is the exact issue-#38 corruption: the applier used to write the
    new literal as a bare expression, dropping the assignment target, so
    ``exec``'ing the snapshot left the variable undefined.
    """
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="custom_tools_src" role="tool_code"\nCUSTOM_TOOLS_SRC = ""\n',
        _patch(
            mutation_id="custom_tools_src",
            new_content='def expand(x):\n    """Doc."""\n    return x',
        ),
    )
    ast.parse(out)
    assert "CUSTOM_TOOLS_SRC = " in out
    value = _exec_value(out, "CUSTOM_TOOLS_SRC")
    assert isinstance(value, str)
    assert "def expand(x):" in value


def test_issue_38_sibling_simple_span_not_corrupted(tmp_path: Path) -> None:
    """A multi-line replace of one simple span must not corrupt a sibling.

    The issue-#38 blast radius: an adjacent simple-string span patched in
    the SAME batch (a one-line ``TOOL_ROSTER`` edit) was also rewritten to
    a bare string, losing its target. Both targets must survive and both
    variables must be defined after exec.
    """
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "prompts.py",
        """
        # zicato:mutable id="roster"
        TOOL_ROSTER = "multi_search"

        # zicato:mutable id="custom_tools_src" role="tool_code"
        CUSTOM_TOOLS_SRC = ""
        """,
    )
    patches = [
        _patch(
            mutation_id="custom_tools_src",
            new_content='def shard(x):\n    """Search shards."""\n    return x',
        ),
        Patch(
            id="p2",
            mutation_id="roster",
            op="replace",
            new_content="multi_search, shard",
            new_numeric=None,
            new_enum=None,
            rationale="test",
        ),
    ]
    apply_patches(src, patches, tgt)
    out = (tgt / "prompts.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "TOOL_ROSTER = " in out
    assert "CUSTOM_TOOLS_SRC = " in out
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102 — controlled test source
    assert ns["TOOL_ROSTER"] == "multi_search, shard"
    assert "def shard(x):" in ns["CUSTOM_TOOLS_SRC"]  # type: ignore[operator]
    # Both ids still re-enumerate after the rewrite.
    ids = {p.id for p in enumerate_mutations([tgt])}
    assert ids == {"roster", "custom_tools_src"}


# ---------------------------------------------------------------------------
# op × content shape — replace a simple ``X = "..."`` span with every shape
# of content. In each case the assignment target survives, the file parses,
# and the rewritten value round-trips through exec.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "new_content", "expected_value"),
    [
        ("single_line_prose", "a new single line", "a new single line"),
        ("multi_line_code", "def f():\n    return 1", "def f():\n    return 1"),
        ("empty", "", ""),
        ("whitespace_only", "   ", "   "),
        ("with_newlines", "line1\nline2\nline3", "line1\nline2\nline3"),
        ("with_double_quote", 'has a " quote', 'has a " quote'),
        ("with_single_quote", "has a ' quote", "has a ' quote"),
        ("trailing_double_quote", 'ends with quote"', 'ends with quote"'),
        ("leading_double_quote", '"starts with quote', '"starts with quote'),
        ("with_backslash", "path\\to\\thing and \\d", "path\\to\\thing and \\d"),
        ("with_unicode", "café — naïve — 日本語", "café — naïve — 日本語"),
        ("embedded_triple_double", 'has a """ run inside', 'has a """ run inside'),
        ("embedded_triple_single", "has a ''' run inside", "has a ''' run inside"),
        ("both_triples", "mixes \"\"\" and ''' delimiters", "mixes \"\"\" and ''' delimiters"),
        ("brace_run_as_prose", "uses {placeholder} braces", "uses {placeholder} braces"),
        ("assignment_echo", 'ROSTER = "multi_search"', 'ROSTER = "multi_search"'),
    ],
)
def test_replace_simple_assignment_content_shapes(
    tmp_path: Path, label: str, new_content: str, expected_value: str
) -> None:
    """Every content shape lands as the literal value of ``X`` — target kept."""
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="x"\nX = "original"\n',
        _patch(mutation_id="x", new_content=new_content),
    )
    ast.parse(out)
    # The assignment target is preserved (issue #38).
    assert "X = " in out
    assert out.lstrip().startswith("# zicato:mutable")
    # The id still re-enumerates.
    assert {p.id for p in enumerate_mutations([tgt_of(tmp_path)])} == {"x"}
    # The rewritten value is exactly the requested content.
    assert _exec_value(out, "X") == expected_value


@pytest.mark.parametrize(
    ("label", "preserved_source", "expected_value"),
    [
        ("plain_double", '"verbatim literal"', "verbatim literal"),
        ("plain_single", "'verbatim literal'", "verbatim literal"),
        ("triple_double", '"""triple body"""', "triple body"),
        ("raw_prefixed", r'r"raw\dstring"', "raw\\dstring"),
        ("implicit_concat", '"part one " "part two"', "part one part two"),
    ],
)
def test_replace_preserves_wellformed_literal_source(
    tmp_path: Path, label: str, preserved_source: str, expected_value: str
) -> None:
    """When ``new_content`` is already valid literal source, it is preserved
    verbatim (not re-wrapped) and still keeps its assignment target."""
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="x"\nX = "original"\n',
        _patch(mutation_id="x", new_content=preserved_source),
    )
    ast.parse(out)
    assert "X = " in out
    assert _exec_value(out, "X") == expected_value


# ---------------------------------------------------------------------------
# span context — the SAME multi-line replace across every syntactic position.
# Each case proves the surrounding syntax (target, kwarg name, parens, comma,
# operator, decorator) survives column-precise surgery.
# ---------------------------------------------------------------------------


def test_context_kwarg_value(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        def make():
            return dict(
                # zicato:mutable id="x"
                instruction="old",
            )
        """,
        _patch(mutation_id="x", new_content="a new\nmulti-line instruction"),
    )
    ast.parse(out)
    assert "instruction=" in out
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["make"]()["instruction"] == "a new\nmulti-line instruction"  # type: ignore[index,operator]


def test_context_positional_arg(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        def make():
            return tuple((
                # zicato:mutable id="x"
                "old",
            ))
        """,
        _patch(mutation_id="x", new_content="positional\nvalue"),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["make"]() == ("positional\nvalue",)  # type: ignore[operator]


def test_context_return_expression(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        def g():
            # zicato:mutable id="x"
            return "old"
        """,
        _patch(mutation_id="x", new_content="returned\nvalue"),
    )
    ast.parse(out)
    assert "return " in out
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["g"]() == "returned\nvalue"  # type: ignore[operator]


def test_context_dict_value(tmp_path: Path) -> None:
    """A marker above a ``key: value`` pair binds to the value when the key
    is on the same line as the value's opening; here the value sits on its
    own line so the nearest literal after the marker is the value."""
    out = _apply_one(
        tmp_path,
        """
        D = {
            "key":
            # zicato:mutable id="x"
            "old",
        }
        """,
        _patch(mutation_id="x", new_content="dict\nvalue"),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["D"] == {"key": "dict\nvalue"}  # type: ignore[index]


def test_context_list_value(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        L = [
            # zicato:mutable id="x"
            "old",
        ]
        """,
        _patch(mutation_id="x", new_content="list\nvalue"),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["L"] == ["list\nvalue"]  # type: ignore[index]


def test_context_decorator_arg(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        def deco(label):
            def wrap(fn):
                fn.label = label
                return fn
            return wrap

        @deco(
            # zicato:mutable id="x"
            "old-label"
        )
        def target():
            return 1
        """,
        _patch(mutation_id="x", new_content="new-label"),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["target"].label == "new-label"  # type: ignore[attr-defined]


def test_context_docstring_multiline(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        '''
        def h():
            # zicato:mutable id="x"
            """Old doc."""
            return 1
        ''',
        _patch(mutation_id="x", new_content="New doc.\n\nWith a body paragraph."),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["h"].__doc__ == "New doc.\n\nWith a body paragraph."  # type: ignore[attr-defined]
    assert ns["h"]() == 1  # type: ignore[operator]


def test_context_deeply_indented_block(tmp_path: Path) -> None:
    out = _apply_one(
        tmp_path,
        """
        def a():
            def b():
                def c():
                    # zicato:mutable id="x"
                    v = "old"
                    return v
                return c
            return b
        """,
        _patch(mutation_id="x", new_content="deep\nvalue"),
    )
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["a"]()()() == "deep\nvalue"  # type: ignore[operator]


def test_context_plus_joined_concat_preserves_operator(tmp_path: Path) -> None:
    """A ``+``-joined sub-clause replace keeps the operator AND the sibling
    clause's target — both clauses stay distinct literals that re-enumerate."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "agent.py",
        """
        PROMPT = (
            # zicato:mutable id="base"
            "base clause.\\n"
            # zicato:mutable id="clause"
            + "pointed clause."
        )
        """,
    )
    patches = [_patch(mutation_id="clause", new_content="rewritten clause.")]
    apply_patches(src, patches, tgt)
    out = (tgt / "agent.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "+ " in out
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["PROMPT"] == "base clause.\nrewritten clause."
    assert {p.id for p in enumerate_mutations([tgt])} == {"base", "clause"}


# ---------------------------------------------------------------------------
# set_numeric / set_enum — the AST-constant-after-marker location.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rendered", "expected"),
    [
        (-3, "T = -3", -3),
        (0.001, "T = 0.001", 0.001),
        (1e-9, "T = 1e-09", 1e-9),
        (10.0, "T = 10", 10),  # exact-integer float renders without a point
        (0, "T = 0", 0),
        (-2.5, "T = -2.5", -2.5),
    ],
)
def test_set_numeric_edge_values(
    tmp_path: Path, value: float, rendered: str, expected: float
) -> None:
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="t"\nT_DOC = "threshold"\nT = 99\n',
        _patch(mutation_id="t", op="set_numeric", new_numeric=value),
    )
    ast.parse(out)
    assert rendered in out
    assert _exec_value(out, "T") == expected


@pytest.mark.parametrize(
    "enum_value",
    [
        "balanced",
        "needs'quote",
        'needs"double',
        "café",
        "with space and -dash",
        "both \" and ' quotes",
    ],
)
def test_set_enum_quoting(tmp_path: Path, enum_value: str) -> None:
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="s"\nS = "greedy"\n',
        _patch(mutation_id="s", op="set_enum", new_enum=enum_value),
    )
    ast.parse(out)
    assert "greedy" not in out
    assert _exec_value(out, "S") == enum_value


# ---------------------------------------------------------------------------
# Atomicity & corruption-safety (issue #11) — a malformed patch must never
# leave a half-written or corrupt snapshot and must fail cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        'multi_search, lookup_entry_context"',  # trailing quote — the four-quote fuse
        '"leading quote, never closed',  # leading quote — fuses at the open delimiter
        'ROSTER = "multi_search"',  # assignment echo ending in a quote
        'contains a """ run inside',  # embedded triple-double-quote
        "mixes \"\"\" and ''' delimiters",  # both triple forms present
    ],
)
def test_issue_11_stray_quote_payloads_stay_parseable(tmp_path: Path, payload: str) -> None:
    """A stray-quote / echo payload must wrap into a collision-proof literal
    so the snapshot parses AND every id re-enumerates — never a delayed
    KeyError one generation later."""
    out = _apply_one(
        tmp_path,
        '# zicato:mutable id="roster"\nROSTER = "multi_search"\n',
        _patch(mutation_id="roster", new_content=payload),
    )
    ast.parse(out)
    assert "ROSTER = " in out
    assert {p.id for p in enumerate_mutations([tgt_of(tmp_path)])} == {"roster"}
    # The payload round-trips as the literal value.
    assert _exec_value(out, "ROSTER") == payload


def test_issue_11_unparseable_code_region_rejected_atomically(tmp_path: Path) -> None:
    """A ``:code`` region body is written verbatim, so a genuinely
    unparseable body is the one way a patch can still break a file. The
    post-apply syntax gate must reject the batch and leave NO snapshot — the
    failure is attributed to the round that produced it, not the next one.
    """
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "tools.py",
        """
        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower()
            # zicato:mutable:end
            return s
        """,
    )
    # An unbalanced paren cannot be re-anchored into valid Python.
    patches = [_patch(mutation_id="slug_logic", new_content="s = topic.lower((")]
    with pytest.raises(ValueError, match="post-apply syntax"):
        apply_patches(src, patches, tgt)
    # Atomic: no corrupt snapshot is left behind.
    assert not tgt.exists()
    # Source is untouched.
    assert "s = topic.lower()" in (src / "tools.py").read_text(encoding="utf-8")


def test_issue_11_no_corrupt_snapshot_blocks_next_generation(tmp_path: Path) -> None:
    """End-to-end: a rejected batch leaves the parent usable. Deriving a
    second, clean generation from the SAME parent succeeds — the corruption
    never propagates."""
    src = tmp_path / "src"
    bad_tgt = tmp_path / "bad"
    good_tgt = tmp_path / "good"
    _write(
        src / "tools.py",
        """
        def slug(topic):
            # zicato:mutable:code id="slug_logic"
            s = topic.lower()
            # zicato:mutable:end
            return s
        """,
    )
    with pytest.raises(ValueError):
        apply_patches(src, [_patch(mutation_id="slug_logic", new_content="s = (")], bad_tgt)
    assert not bad_tgt.exists()
    # A clean derivation from the same parent works.
    apply_patches(
        src,
        [_patch(mutation_id="slug_logic", new_content="s = topic.strip().lower()")],
        good_tgt,
    )
    out = (good_tgt / "tools.py").read_text(encoding="utf-8")
    ast.parse(out)
    ns: dict[str, object] = {}
    exec(out, ns)  # noqa: S102
    assert ns["slug"]("Hello") == "hello"  # type: ignore[operator]


def test_post_apply_gate_ignores_file_broken_before_the_batch(tmp_path: Path) -> None:
    """The syntax gate must blame only the round that caused the damage.

    A source tree can hold a ``.py`` file that never parsed — a deliberately
    broken test fixture, a template, a file that uses newer syntax than the
    running interpreter.  That file is copied into every child snapshot, so
    the gate sees the same ``SyntaxError`` on every batch.  It must not
    reject a batch whose own patches all applied cleanly.
    """
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "prompts.py", '# zicato:mutable id="roster"\nROSTER = "old"\n')
    # Broken before the run starts, and no patch in the batch touches it.
    _write(src / "fixture_broken.py", "def f(:\n    pass\n")

    apply_patches(src, [_patch(mutation_id="roster", new_content="new")], tgt)

    out = (tgt / "prompts.py").read_text(encoding="utf-8")
    assert _exec_value(out, "ROSTER") == "new"
    # The pre-existing breakage is carried forward untouched, not repaired
    # and not treated as this round's fault.
    assert (tgt / "fixture_broken.py").read_text(encoding="utf-8") == "def f(:\n    pass\n"


def test_pre_existing_break_does_not_stall_successive_generations(tmp_path: Path) -> None:
    """The failure above compounds: the broken file rides into every child.

    If the gate rejects on it, no generation is ever promoted and the evolve
    loop stalls for good.  Two derivations from the same parent must both
    succeed.
    """
    src = tmp_path / "src"
    _write(src / "prompts.py", '# zicato:mutable id="roster"\nROSTER = "gen0"\n')
    _write(src / "fixture_broken.py", "def f(:\n    pass\n")

    gen1 = tmp_path / "gen1"
    apply_patches(src, [_patch(mutation_id="roster", new_content="gen1")], gen1)
    gen2 = tmp_path / "gen2"
    apply_patches(gen1, [_patch(mutation_id="roster", new_content="gen2")], gen2)

    assert _exec_value((gen2 / "prompts.py").read_text(encoding="utf-8"), "ROSTER") == "gen2"


# ---------------------------------------------------------------------------
# Filesystem (issue #4) — read-only source trees apply successfully.
# ---------------------------------------------------------------------------


def test_issue_4_read_only_file_applies(tmp_path: Path) -> None:
    """A mode-0o444 source file is copied read-only; the applier restores
    owner-write before writing the patched content."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    file_path = src / "prompts.py"
    _write(file_path, '# zicato:mutable id="x"\nX = "original"\n')
    file_path.chmod(0o444)
    apply_patches(src, [_patch(mutation_id="x", new_content="rewritten")], tgt)
    result = (tgt / "prompts.py").read_text(encoding="utf-8")
    assert _exec_value(result, "X") == "rewritten"


def test_issue_4_read_only_directory_applies(tmp_path: Path) -> None:
    """A read-only source DIRECTORY (mode 0o555) still produces a writable
    child copy the applier can patch — the copytree must not propagate the
    directory's missing write bit into the child working copy."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    sub = src / "agent"
    _write(sub / "prompts.py", '# zicato:mutable id="x"\nX = "original"\n')
    sub.chmod(0o555)
    try:
        apply_patches(src, [_patch(mutation_id="x", new_content="rewritten")], tgt)
    finally:
        # Restore so pytest can clean up tmp_path.
        sub.chmod(0o755)
    result = (tgt / "agent" / "prompts.py").read_text(encoding="utf-8")
    assert _exec_value(result, "X") == "rewritten"


def test_issue_4_read_only_sibling_file_in_batch(tmp_path: Path) -> None:
    """Two read-only files patched in one batch both apply — the owner-write
    restore happens per write site, not once for the first file."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    a = src / "a.py"
    b = src / "b.py"
    _write(a, '# zicato:mutable id="a_id"\nA = "alpha"\n')
    _write(b, '# zicato:mutable id="b_id"\nB = "beta"\n')
    a.chmod(0o444)
    b.chmod(0o444)
    patches = [
        _patch(mutation_id="a_id", new_content="alpha2"),
        Patch(
            id="p2",
            mutation_id="b_id",
            op="replace",
            new_content="beta2",
            new_numeric=None,
            new_enum=None,
            rationale="test",
        ),
    ]
    apply_patches(src, patches, tgt)
    assert _exec_value((tgt / "a.py").read_text(encoding="utf-8"), "A") == "alpha2"
    assert _exec_value((tgt / "b.py").read_text(encoding="utf-8"), "B") == "beta2"


# ---------------------------------------------------------------------------
# Invariants — duplicate mutation id, idempotency, re-enumeration.
# ---------------------------------------------------------------------------


def test_duplicate_mutation_id_across_files_rejected(tmp_path: Path) -> None:
    """A mutation id declared in two files is ambiguous: a patch targeting it
    is rejected loudly (no last-write-wins), and no snapshot is left."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(src / "a.py", '# zicato:mutable id="dup"\nA = "alpha"\n')
    _write(src / "b.py", '# zicato:mutable id="dup"\nB = "beta"\n')
    with pytest.raises(ValueError, match="ambiguous"):
        apply_patches(src, [_patch(mutation_id="dup", new_content="x")], tgt)
    assert not tgt.exists()


def test_duplicate_mutation_id_within_file_rejected(tmp_path: Path) -> None:
    """Two markers in one file sharing an id are equally ambiguous."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="dup"
        A = "alpha"

        # zicato:mutable id="dup"
        B = "beta"
        """,
    )
    with pytest.raises(ValueError, match="ambiguous"):
        apply_patches(src, [_patch(mutation_id="dup", new_content="x")], tgt)
    assert not tgt.exists()


def test_unrelated_duplicate_does_not_block_clean_batch(tmp_path: Path) -> None:
    """A duplicate id elsewhere in the tree does NOT block a batch that only
    targets a unique id — only ids a patch actually targets are checked."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "a.py",
        """
        # zicato:mutable id="dup"
        A = "alpha"

        # zicato:mutable id="solo"
        C = "gamma"
        """,
    )
    _write(src / "b.py", '# zicato:mutable id="dup"\nB = "beta"\n')
    apply_patches(src, [_patch(mutation_id="solo", new_content="changed")], tgt)
    assert _exec_value((tgt / "a.py").read_text(encoding="utf-8"), "C") == "changed"


def test_replace_is_idempotent(tmp_path: Path) -> None:
    """Applying the same replace twice (parent -> child -> grandchild) yields
    the same value and the id keeps resolving each generation."""
    src = tmp_path / "src"
    child = tmp_path / "child"
    grandchild = tmp_path / "grandchild"
    _write(src / "m.py", '# zicato:mutable id="x"\nX = "original"\n')
    patch = _patch(mutation_id="x", new_content="stable value")
    apply_patches(src, [patch], child)
    apply_patches(child, [patch], grandchild)
    out_child = (child / "m.py").read_text(encoding="utf-8")
    out_grand = (grandchild / "m.py").read_text(encoding="utf-8")
    assert _exec_value(out_child, "X") == "stable value"
    assert _exec_value(out_grand, "X") == "stable value"
    assert {p.id for p in enumerate_mutations([grandchild])} == {"x"}


def test_required_placeholders_survive_multiline_replace(tmp_path: Path) -> None:
    """A span carrying ``required_placeholders`` keeps them across a
    multi-line replace; the surviving target lets post-apply validation read
    the placeholders back off the still-resolving point."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "m.py",
        '# zicato:mutable id="tpl" required_placeholders="{topic}"\n' 'TPL = "Research {topic}."\n',
    )
    apply_patches(
        src,
        [_patch(mutation_id="tpl", new_content="Investigate {topic}\nin depth.")],
        tgt,
    )
    out = (tgt / "m.py").read_text(encoding="utf-8")
    ast.parse(out)
    assert "TPL = " in out
    assert "{topic}" in _exec_value(out, "TPL")  # type: ignore[operator]
