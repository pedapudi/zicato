"""Tests for :mod:`zicato.epoch.genstore` — the :class:`GenerationStore` seam.

The generation store is the pluggable backend for generation source
trees (``docs/design/STORAGE.md`` §4-§5). These tests pin the
:class:`~zicato.epoch.genstore.GenerationStore` protocol's observable
contract against the shipped
:class:`~zicato.epoch.genstore.DirectoryGenerationStore` — coordinate→
path resolution, seed-from-trees, derive-by-patch, the all-or-nothing
boundary, and the stale-tree recovery a crashed round needs.

A future git backend implements the same protocol; the assertions here
are written against the protocol, not the directory layout, wherever
that distinction matters.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from zicato.core.types import Patch
from zicato.epoch.genstore import (
    DirectoryGenerationStore,
    GenerationStore,
    default_generation_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(
    *,
    pid: str,
    mutation_id: str,
    op: str = "replace",
    new_content: str | None = None,
) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )


def _mutable_tree(root: Path, *, instr: str = "original") -> Path:
    """Build a tiny inner-harness source tree with one mutation point."""
    tree = root / "agent"
    _write(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="instr"
        INSTR = """{instr}"""
        ''',
    )
    return tree


# ---------------------------------------------------------------------------
# protocol / construction
# ---------------------------------------------------------------------------


def test_directory_store_satisfies_the_protocol(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path)
    assert isinstance(store, GenerationStore)


def test_default_generation_store_returns_directory_backend(tmp_path: Path) -> None:
    store = default_generation_store(tmp_path)
    assert isinstance(store, DirectoryGenerationStore)
    assert store.workspace_root == tmp_path


# ---------------------------------------------------------------------------
# snapshot_root — pure path math
# ---------------------------------------------------------------------------


def test_snapshot_root_is_pure_path_math(tmp_path: Path) -> None:
    """snapshot_root resolves a coordinate without any I/O side effect."""
    store = DirectoryGenerationStore(tmp_path)
    root = store.snapshot_root("2026-05-16_e1", "v3")
    assert root == tmp_path / "epochs" / "2026-05-16_e1" / "generations" / "v3" / "snapshot"
    # No directory was created by the pure query.
    assert not root.exists()


# ---------------------------------------------------------------------------
# seed_generation
# ---------------------------------------------------------------------------


def test_seed_generation_copies_tree_under_basename(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "registered", instr="seeded")

    root = store.seed_generation("e1", "v0", [tree])

    assert root == store.snapshot_root("e1", "v0")
    # The tree landed under its own basename ("agent").
    seeded = root / "agent" / "prompts.py"
    assert seeded.exists()
    assert "seeded" in seeded.read_text(encoding="utf-8")
    assert store.has_generation("e1", "v0") is True


def test_seed_generation_copies_multiple_trees(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree_a = _mutable_tree(tmp_path / "a")
    _write(tmp_path / "b" / "lib" / "util.py", "X = 1\n")

    root = store.seed_generation("e1", "v0", [tree_a, tmp_path / "b" / "lib"])

    assert (root / "agent" / "prompts.py").exists()
    assert (root / "lib" / "util.py").exists()


def test_seed_generation_copies_a_single_file(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    src = tmp_path / "config.py"
    src.write_text("SETTING = 1\n", encoding="utf-8")

    root = store.seed_generation("e1", "v0", [src])

    assert (root / "config.py").read_text(encoding="utf-8") == "SETTING = 1\n"


def test_list_generations_reports_seeded(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    assert store.list_generations("e1") == ["v0"]


# ---------------------------------------------------------------------------
# derive_generation — the generation-level transaction boundary
# ---------------------------------------------------------------------------


def test_derive_generation_applies_patch_to_child(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src", instr="original")
    store.seed_generation("e1", "v0", [tree])

    child_root = store.derive_generation(
        epoch_id="e1",
        parent_generation_id="v0",
        child_generation_id="v1",
        patches=[
            _patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""'),
        ],
    )

    assert child_root == store.snapshot_root("e1", "v1")
    child_text = (child_root / "agent" / "prompts.py").read_text(encoding="utf-8")
    assert "rewritten" in child_text
    assert "original" not in child_text


def test_derive_generation_leaves_parent_untouched(tmp_path: Path) -> None:
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src", instr="original")
    store.seed_generation("e1", "v0", [tree])

    store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""')],
    )

    parent_text = (store.snapshot_root("e1", "v0") / "agent" / "prompts.py").read_text(
        encoding="utf-8"
    )
    assert "original" in parent_text
    assert "rewritten" not in parent_text


def test_derive_generation_clears_stale_child_from_failed_round(tmp_path: Path) -> None:
    """A leftover child snapshot from a crashed round does not block a retry."""
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])

    # Simulate a crashed round that left a partial child snapshot.
    stale = store.snapshot_root("e1", "v1")
    stale.mkdir(parents=True)
    (stale / "garbage.txt").write_text("partial", encoding="utf-8")

    # The retry succeeds — derive_generation clears the stale tree.
    child_root = store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""ok"""')],
    )
    assert not (child_root / "garbage.txt").exists()
    assert "ok" in (child_root / "agent" / "prompts.py").read_text(encoding="utf-8")


def test_derive_generation_chain(tmp_path: Path) -> None:
    """v0 -> v1 -> v2 each derive from the prior generation's tree."""
    store = DirectoryGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src", instr="gen0")
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""gen1"""')],
    )
    store.derive_generation(
        "e1",
        "v1",
        "v2",
        [_patch(pid="p2", mutation_id="instr", new_content='"""gen2"""')],
    )

    assert sorted(store.list_generations("e1")) == ["v0", "v1", "v2"]
    assert "gen2" in (store.snapshot_root("e1", "v2") / "agent" / "prompts.py").read_text(
        encoding="utf-8"
    )
