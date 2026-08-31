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
dashboard read surface (``list_tree`` / ``read_file`` / source diffs).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests._source_tree_builders import mutable_tree, write_dedented
from zicato.core.types import Patch
from zicato.epoch.genstore import (
    EPHEMERAL_SNAPSHOT_PREFIX,
    DirectoryGenerationStore,
    EphemeralCheckout,
    GenerationStore,
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
def backend(request: pytest.FixtureRequest) -> str:
    """The backend under test — the single parametrisation axis."""
    return str(request.param)


@pytest.fixture
def store(backend: str, tmp_path: Path) -> GenerationStore:
    """A fresh, EMPTY generation store, once per backend."""
    return _BACKENDS[backend](tmp_path / "ws")


@pytest.fixture(scope="session")
def _seeded_ws_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Per-backend workspace templates with ``e1/v0`` already seeded.

    Seeding the git backend costs a dozen-plus ``git`` subprocess spawns
    (init + identity + add + commit + tag + worktree). Building the seeded
    workspace ONCE per backend and ``copytree``-ing it per test keeps every
    test hermetic — each test still gets a private, writable workspace —
    while dropping the per-test spawn storm. Tests whose contract IS the
    seeding behaviour keep seeding a fresh store instead.
    """
    templates: dict[str, Path] = {}
    for name, factory in _BACKENDS.items():
        base = tmp_path_factory.mktemp(f"genstore-template-{name}")
        ws = base / "ws"
        factory(ws).seed_generation("e1", "v0", [_template_tree(base / "src")])
        # A materialised git worktree registers its ABSOLUTE path inside the
        # repo, which cannot survive relocation-by-copytree: drop the
        # worktrees and prune the registrations so each copy re-materialises
        # its own on first materialize_snapshot().
        worktrees = ws / GitGenerationStore.WORKTREES_DIRNAME
        if worktrees.is_dir():
            shutil.rmtree(worktrees)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(ws / GitGenerationStore.REPO_DIRNAME),
                check=True,
                capture_output=True,
            )
        templates[name] = ws
    return templates


@pytest.fixture
def seeded_store(
    backend: str, _seeded_ws_templates: dict[str, Path], tmp_path: Path
) -> GenerationStore:
    """A store with ``e1/v0`` already seeded from :func:`_template_tree`.

    A private copy of the session template — mutations (derives, new
    worktrees) never leak between tests.
    """
    ws = tmp_path / "ws"
    shutil.copytree(_seeded_ws_templates[backend], ws)
    return _BACKENDS[backend](ws)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _template_tree(root: Path) -> Path:
    """The tree the session-scoped seeded templates are built from.

    ``mutable_tree`` (``instr="original"``) plus one never-mutated extra
    file, so the read-surface tests can assert on a multi-file listing.
    """
    tree = mutable_tree(root, instr="original")
    write_dedented(tree / "lib" / "util.py", "X = 1\n")
    return tree


# ---------------------------------------------------------------------------
# protocol conformance
# ---------------------------------------------------------------------------


def test_backend_satisfies_the_protocol(store: GenerationStore) -> None:
    assert isinstance(store, GenerationStore)


def test_backend_reports_its_configured_name(store: GenerationStore, backend: str) -> None:
    assert store.backend_name == backend


# ---------------------------------------------------------------------------
# coordinate queries
# ---------------------------------------------------------------------------


def test_has_generation_false_before_materialisation(store: GenerationStore) -> None:
    assert store.has_generation("e1", "v0") is False


def test_list_generations_empty_for_unknown_epoch(store: GenerationStore) -> None:
    assert store.list_generations("never_existed") == []


def test_snapshot_path_is_pure(store: GenerationStore) -> None:
    path = store.snapshot_path("e1", "v0")
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        store.materialize_snapshot("e1", "v0")


# ---------------------------------------------------------------------------
# seed_generation
# ---------------------------------------------------------------------------


def test_seed_generation_materialises_tree(store: GenerationStore, tmp_path: Path) -> None:
    tree = mutable_tree(tmp_path / "registered", instr="seeded")
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
    tree = mutable_tree(tmp_path / "registered")
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


def test_derive_generation_applies_patch(seeded_store: GenerationStore) -> None:
    store = seeded_store
    child_root = store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""')],
    )
    text = (child_root / "agent" / "prompts.py").read_text(encoding="utf-8")
    assert "rewritten" in text
    assert "original" not in text


def test_derive_generation_leaves_parent_untouched(seeded_store: GenerationStore) -> None:
    store = seeded_store
    parent_root = store.materialize_snapshot("e1", "v0")
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
    seeded_store: GenerationStore,
) -> None:
    store = seeded_store
    with pytest.raises(ValueError):
        store.derive_generation(
            "e1",
            "v0",
            "v1",
            [_patch(pid="bad", mutation_id="does_not_exist", new_content='"""x"""')],
        )
    assert store.has_generation("e1", "v1") is False


def test_derive_generation_chain(seeded_store: GenerationStore) -> None:
    store = seeded_store
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


def test_list_tree_returns_sorted_entries(seeded_store: GenerationStore) -> None:
    store = seeded_store
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


def test_read_file_returns_bytes(seeded_store: GenerationStore) -> None:
    data = seeded_store.read_file("e1", "v0", "agent/prompts.py")
    assert isinstance(data, bytes)
    assert b"original" in data


def test_read_file_rejects_traversal(seeded_store: GenerationStore) -> None:
    with pytest.raises(ValueError):
        seeded_store.read_file("e1", "v0", "../../../etc/passwd")


def test_read_file_missing_file_raises(seeded_store: GenerationStore) -> None:
    with pytest.raises(FileNotFoundError):
        seeded_store.read_file("e1", "v0", "agent/nonexistent.py")


def test_diff_generations_reports_the_applied_source_change(
    seeded_store: GenerationStore,
) -> None:
    seeded_store.derive_generation(
        "e1",
        "v0",
        "v1",
        [_patch(pid="p1", mutation_id="instr", new_content='"""rewritten"""')],
    )
    diff = seeded_store.diff_generations("e1", "v0", "v1")
    assert "original" in diff
    assert "rewritten" in diff
    assert "agent/prompts.py" in diff


def _diff_fixture_tree(root: Path, *, revised: bool) -> Path:
    """One source tree in two versions, covering every diff-rendering case.

    Between the two versions a text file changes, one file is added and
    another removed, a file without a final newline changes, a file whose
    lines end in carriage-return–newline changes, and a file of
    non-decodable bytes changes. Nested directories cover path ordering.
    """
    tree = root / "agent"
    write_dedented(tree / "prompts.py", f'INSTR = """{"rewritten" if revised else "original"}"""\n')
    write_dedented(tree / "lib" / "util.py", "X = 1\n")
    write_dedented(tree / "notes.txt", "after" if revised else "before")
    write_dedented(tree / "crlf.txt", "one\r\n" + ("TWO\r\n" if revised else "two\r\n"))
    write_dedented(tree / ("added.py" if revised else "removed.py"), "A = 1\n")
    payload = range(256) if revised else reversed(range(256))
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    (tree / "bin" / "weights.dat").write_bytes(bytes(payload))
    return tree


def _rendered_diffs(tmp_path: Path) -> dict[str, str]:
    """Render the same two source trees through every backend."""
    original = _diff_fixture_tree(tmp_path / "original", revised=False)
    revised = _diff_fixture_tree(tmp_path / "revised", revised=True)
    rendered: dict[str, str] = {}
    for name, factory in _BACKENDS.items():
        store = factory(tmp_path / f"ws-{name}")
        store.seed_generation("e1", "v0", [original])
        store.seed_generation("e1", "v1", [revised])
        rendered[name] = store.diff_generations("e1", "v0", "v1")
    return rendered


def test_every_backend_renders_one_diff_text(tmp_path: Path) -> None:
    """The proposer reads the same diff whichever backend stores the source."""
    rendered = _rendered_diffs(tmp_path)
    assert rendered["directory"] == rendered["git"]


def test_rendered_diff_carries_the_git_style_markers(tmp_path: Path) -> None:
    """The rendered format: per-file header, binary notice, newline marker."""
    diff = _rendered_diffs(tmp_path)["git"]
    assert "diff --git a/agent/prompts.py b/agent/prompts.py\n" in diff
    assert '-INSTR = """original"""\n+INSTR = """rewritten"""\n' in diff
    assert "Binary files a/agent/bin/weights.dat and b/agent/bin/weights.dat differ\n" in diff
    assert "-before\n\\ No newline at end of file\n+after\n\\ No newline at end of file\n" in diff
    assert "--- /dev/null\n+++ b/agent/added.py\n" in diff
    assert "--- a/agent/removed.py\n+++ /dev/null\n" in diff
    assert "-two\r\n+TWO\r\n" in diff
    assert "agent/lib/util.py" not in diff


def test_prune_generations_dry_run_and_apply(seeded_store: GenerationStore) -> None:
    path = seeded_store.materialize_snapshot("e1", "v0")
    expected_bytes = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    assert seeded_store.prune_generations("e1", ["v0"], dry_run=True) == expected_bytes
    assert seeded_store.has_generation("e1", "v0")
    assert seeded_store.prune_generations("e1", ["v0"], dry_run=False) == expected_bytes
    assert not seeded_store.has_generation("e1", "v0")
    assert "v0" not in seeded_store.list_generations("e1")


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
    tree = mutable_tree(tmp_path / "src", instr="checkout-me")
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
