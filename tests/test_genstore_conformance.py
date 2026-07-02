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

import tempfile
import textwrap
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.epoch.genstore import (
    EPHEMERAL_SNAPSHOT_PREFIX,
    DirectoryGenerationStore,
    EphemeralCheckout,
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
    # The directory backend pins the message text; other backends share only
    # the exception type.
    match = "does not exist" if isinstance(store, DirectoryGenerationStore) else None
    with pytest.raises(FileNotFoundError, match=match):
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
    # The directory backend pins the message text; other backends share only
    # the exception type.
    match = "has no source tree" if isinstance(store, DirectoryGenerationStore) else None
    with pytest.raises(FileNotFoundError, match=match):
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


# ---------------------------------------------------------------------------
# checkout_ephemeral — the per-run isolated working copy
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the system temp dir so ``ztw-snap-*`` parents land privately.

    ``checkout_ephemeral`` MUST place its parent under
    ``tempfile.gettempdir()`` (the supervisor's reaper guard depends on
    that placement); patching ``tempfile.tempdir`` gives each test an
    empty, private temp root to assert against.
    """
    isolated = tmp_path / "ztw-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(isolated))
    return isolated


def _seeded(store: GenerationStore, tmp_path: Path) -> Path:
    """Seed ``e1/v0`` and return its canonical snapshot root."""
    tree = _mutable_tree(tmp_path / "src", instr="checkout-me")
    return store.seed_generation("e1", "v0", [tree])


def test_checkout_ephemeral_materialises_isolated_tree(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    canonical = _seeded(store, tmp_path)
    co = store.checkout_ephemeral("e1", "v0", "v0--entry_a")
    try:
        assert isinstance(co, EphemeralCheckout)
        assert co.working_dir.is_dir()
        assert co.working_dir.resolve() != canonical.resolve()
        body = (co.working_dir / "agent" / "prompts.py").read_text(encoding="utf-8")
        assert "checkout-me" in body
        # Basename parity: __file__-derived paths look the same as under
        # the canonical tree.
        assert co.working_dir.name == canonical.name
    finally:
        co.cleanup()


def test_checkout_ephemeral_parent_shape_is_reapable(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    """The parent is ``{tempdir}/ztw-snap-{run_id}-*`` — the reaper's guard shape."""
    _seeded(store, tmp_path)
    run_id = "v0--entry_b"
    co = store.checkout_ephemeral("e1", "v0", run_id)
    try:
        parent = co.working_dir.parent
        assert parent.parent == _isolated_tempdir
        assert parent.name.startswith(f"{EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-")
        # The scratch dir shares the parent so ONE cleanup (or the
        # supervisor's reap of the parent) removes both.
        assert co.scratch_dir.parent == parent
        assert co.scratch_dir.is_dir()
        assert list(co.scratch_dir.iterdir()) == []
    finally:
        co.cleanup()


def test_checkout_ephemeral_contains_no_git_admin_files(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    """The working tree carries no ``.git`` — a plain throwaway tree.

    Parity across backends: the directory backend's copy filter skips
    ``.git``; the git backend detaches its per-run worktree by unlinking
    the pointer file. Either way the worker cannot reach a repository
    through its mounted tree.
    """
    _seeded(store, tmp_path)
    co = store.checkout_ephemeral("e1", "v0", "v0--entry_g")
    try:
        assert not (co.working_dir / ".git").exists()
    finally:
        co.cleanup()


def test_checkout_ephemeral_stray_write_never_reaches_canonical(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    _seeded(store, tmp_path)
    co1 = store.checkout_ephemeral("e1", "v0", "v0--entry_a")
    try:
        (co1.working_dir / "stray.txt").write_text("pollution", encoding="utf-8")
        (co1.working_dir / "agent" / "prompts.py").write_text("clobbered", encoding="utf-8")
    finally:
        co1.cleanup()
    # The canonical tree (via the read surface) is untouched...
    assert "checkout-me" in store.read_file("e1", "v0", "agent/prompts.py").decode("utf-8")
    with pytest.raises(FileNotFoundError):
        store.read_file("e1", "v0", "stray.txt")
    # ...and a subsequent checkout of the SAME generation starts clean.
    co2 = store.checkout_ephemeral("e1", "v0", "v0--entry_a")
    try:
        assert not (co2.working_dir / "stray.txt").exists()
        body = (co2.working_dir / "agent" / "prompts.py").read_text(encoding="utf-8")
        assert "checkout-me" in body
    finally:
        co2.cleanup()


def test_checkout_ephemeral_cleanup_removes_parent_and_is_idempotent(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    _seeded(store, tmp_path)
    co = store.checkout_ephemeral("e1", "v0", "v0--entry_a")
    parent = co.working_dir.parent
    assert parent.is_dir()
    co.cleanup()
    assert not parent.exists()
    co.cleanup()  # idempotent — a double cleanup must not raise
    assert list(_isolated_tempdir.iterdir()) == []


def test_checkout_ephemeral_missing_generation_raises(
    store: GenerationStore, _isolated_tempdir: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        store.checkout_ephemeral("e1", "v99", "v99--entry_a")
    # No half-made checkout parent is left behind.
    assert list(_isolated_tempdir.iterdir()) == []


def test_checkout_ephemeral_concurrent_same_generation_isolated(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    """Concurrent checkouts of ONE generation are mutually isolated.

    This is the champion-replicate shape: N runs of the same generation
    in flight at once (and, for the git backend, N concurrent
    ``worktree add`` calls contending on the repo lock).
    """
    _seeded(store, tmp_path)

    def one(i: int) -> EphemeralCheckout:
        return store.checkout_ephemeral("e1", "v0", f"v0--entry_{i}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        checkouts = list(ex.map(one, range(8)))
    try:
        roots = {c.working_dir.resolve() for c in checkouts}
        assert len(roots) == 8, "every run must get its own tree"
        for i, c in enumerate(checkouts):
            (c.working_dir / "mine.txt").write_text(str(i), encoding="utf-8")
        for i, c in enumerate(checkouts):
            assert (c.working_dir / "mine.txt").read_text(encoding="utf-8") == str(i)
    finally:
        for c in checkouts:
            c.cleanup()
    assert list(_isolated_tempdir.iterdir()) == []


def test_checkout_ephemeral_leaves_store_healthy(
    store: GenerationStore, tmp_path: Path, _isolated_tempdir: Path
) -> None:
    """Checkout + cleanup must not perturb subsequent store transactions."""
    _seeded(store, tmp_path)
    co = store.checkout_ephemeral("e1", "v0", "v0--entry_a")
    co.cleanup()
    child_root = store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""after-checkout"""')],
    )
    assert "after-checkout" in (child_root / "agent" / "prompts.py").read_text(encoding="utf-8")
    # And the new child is itself checkout-able.
    co2 = store.checkout_ephemeral("e1", "v1", "v1--entry_a")
    try:
        body = (co2.working_dir / "agent" / "prompts.py").read_text(encoding="utf-8")
        assert "after-checkout" in body
    finally:
        co2.cleanup()
