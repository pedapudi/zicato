"""Cross-backend conformance suite for the :class:`GenerationStore` protocol.

Every test here is parametrised over **both** generation-store backends —
:class:`~zicato.epoch.genstore.DirectoryGenerationStore` and
:class:`~zicato.epoch.git_genstore.GitGenerationStore` — so the git
backend is held to the exact same observable contract as the shipped
directory backend. A backend that diverges fails here.

The directory-specific tests stay in ``tests/test_epoch_genstore.py``;
this file is the *protocol* contract, written against behaviour both
backends must share: seed-from-trees, derive-by-patch, the
all-or-nothing transaction boundary, artifact exclusion, and the
dashboard read surface (``list_tree`` / ``read_file`` / ``list_patches``).
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.epoch.genstore import (
    DirectoryGenerationStore,
    GenerationStore,
    PatchRecord,
    TreeEntry,
)
from zicato.epoch.git_genstore import GitGenerationStore

# ---------------------------------------------------------------------------
# Backend parametrisation
# ---------------------------------------------------------------------------

#: Each backend factory takes a workspace root and returns a fresh store.
_BACKENDS: dict[str, Callable[[Path], GenerationStore]] = {
    "directory": DirectoryGenerationStore,
    "git": GitGenerationStore,
}


@pytest.fixture(params=sorted(_BACKENDS), ids=sorted(_BACKENDS))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> GenerationStore:
    """A fresh generation store, once per backend."""
    factory = _BACKENDS[request.param]
    return factory(tmp_path / "ws")


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
        rationale="conformance",
    )


def _mutable_tree(root: Path, *, instr: str = "original") -> Path:
    """A tiny inner-harness source tree with one mutation point."""
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
# protocol conformance
# ---------------------------------------------------------------------------


def test_backend_satisfies_the_protocol(store: GenerationStore) -> None:
    assert isinstance(store, GenerationStore)


# ---------------------------------------------------------------------------
# coordinate queries
# ---------------------------------------------------------------------------


def test_has_generation_false_before_materialisation(store: GenerationStore) -> None:
    assert store.has_generation("e1", "v0") is False


def test_list_generations_empty_for_unknown_epoch(store: GenerationStore) -> None:
    assert store.list_generations("never_existed") == []


# ---------------------------------------------------------------------------
# seed_generation
# ---------------------------------------------------------------------------


def test_seed_generation_materialises_tree(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "registered", instr="seeded")
    root = store.seed_generation("e1", "v0", [tree])
    assert root.is_dir()
    assert (root / "agent" / "prompts.py").is_file()
    assert "seeded" in (root / "agent" / "prompts.py").read_text(encoding="utf-8")
    assert store.has_generation("e1", "v0") is True
    assert store.list_generations("e1") == ["v0"]


def test_seed_generation_raises_for_missing_source(store: GenerationStore, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        store.seed_generation("e1", "v0", [tmp_path / "ghost"])


def test_seed_generation_excludes_run_artifacts(store: GenerationStore, tmp_path: Path) -> None:
    """A registered tree's ``output/`` must NOT enter the generation."""
    tree = _mutable_tree(tmp_path / "registered")
    artifact = tree / "output" / "rendered.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("noise", encoding="utf-8")
    cache = tree / "__pycache__" / "prompts.cpython-311.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_text("bytecode", encoding="utf-8")

    store.seed_generation("e1", "v0", [tree])

    paths = {e.path for e in store.list_tree("e1", "v0")}
    assert "agent/prompts.py" in paths
    assert not any("output" in p for p in paths), paths
    assert not any("__pycache__" in p for p in paths), paths


# ---------------------------------------------------------------------------
# derive_generation — the generation-level transaction boundary
# ---------------------------------------------------------------------------


def test_derive_generation_applies_patch(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src", instr="original")
    store.seed_generation("e1", "v0", [tree])

    child_root = store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""')],
    )
    text = (child_root / "agent" / "prompts.py").read_text(encoding="utf-8")
    assert "rewritten" in text
    assert "original" not in text


def test_derive_generation_leaves_parent_untouched(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src", instr="original")
    parent_root = store.seed_generation("e1", "v0", [tree])
    store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""')],
    )
    # Re-resolve the parent: a backend may move/recreate the worktree.
    parent_text = store.read_file("e1", "v0", "agent/prompts.py").decode("utf-8")
    assert "original" in parent_text
    assert "rewritten" not in parent_text
    # parent_root path still points at a clean parent tree.
    assert parent_root.is_dir()


def test_derive_generation_raises_for_missing_parent(
    store: GenerationStore,
) -> None:
    with pytest.raises(FileNotFoundError):
        store.derive_generation("e1", "v0", "v1", [])


def test_derive_generation_all_or_nothing_on_bad_patch(
    store: GenerationStore, tmp_path: Path
) -> None:
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])

    with pytest.raises(ValueError):
        store.derive_generation(
            "e1",
            "v0",
            "v1",
            [_patch(pid="bad", mutation_id="does_not_exist", new_content='"""x"""')],
        )
    assert store.has_generation("e1", "v1") is False


def test_derive_generation_chain(store: GenerationStore, tmp_path: Path) -> None:
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
    text = store.read_file("e1", "v2", "agent/prompts.py").decode("utf-8")
    assert "gen2" in text


# ---------------------------------------------------------------------------
# read surface — the dashboard file-tree / file-browser API
# ---------------------------------------------------------------------------


def test_list_tree_returns_sorted_entries(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src")
    _write(tree / "lib" / "util.py", "X = 1\n")
    store.seed_generation("e1", "v0", [tree])

    entries = store.list_tree("e1", "v0")
    assert all(isinstance(e, TreeEntry) for e in entries)
    paths = [e.path for e in entries]
    assert paths == sorted(paths)
    file_paths = {e.path for e in entries if not e.is_dir}
    assert "agent/prompts.py" in file_paths
    assert "agent/lib/util.py" in file_paths
    # The file entries carry a non-zero size.
    for e in entries:
        if not e.is_dir:
            assert e.size > 0


def test_list_tree_raises_for_missing_generation(store: GenerationStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.list_tree("e1", "v99")


def test_read_file_returns_bytes(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src", instr="hello")
    store.seed_generation("e1", "v0", [tree])
    data = store.read_file("e1", "v0", "agent/prompts.py")
    assert isinstance(data, bytes)
    assert b"hello" in data


def test_read_file_rejects_traversal(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    with pytest.raises(ValueError):
        store.read_file("e1", "v0", "../../../etc/passwd")


def test_read_file_missing_file_raises(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    with pytest.raises(FileNotFoundError):
        store.read_file("e1", "v0", "agent/nonexistent.py")


def test_list_patches_empty_for_seed(store: GenerationStore, tmp_path: Path) -> None:
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    record = store.list_patches("e1", "v0")
    assert isinstance(record, PatchRecord)
    assert record.generation_id == "v0"
    assert record.patches == ()
