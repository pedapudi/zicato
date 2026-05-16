"""Tests for ``zicato.mutation.applier``."""

from __future__ import annotations

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
