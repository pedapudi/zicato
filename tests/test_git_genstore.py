"""Git-specific tests for :class:`~zicato.epoch.git_genstore.GitGenerationStore`.

The cross-backend protocol contract is covered by
``tests/test_genstore_conformance.py``. This file pins behaviour that is
*specific* to the git backend: the domain → git mapping (branches per
epoch, tags per generation), the commit-message metadata block, worktree
materialisation, blob dedup, and config-knob selection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests._source_tree_builders import mutable_tree, write_dedented
from zicato.core.types import Patch
from zicato.epoch.genstore import default_generation_store
from zicato.epoch.git_genstore import GitGenerationStore

# Every test here drives real ``git`` subprocesses — the git-native mapping
# (branches, tags, worktrees, blobs) IS the coverage. Tagged for the opt-in
# fast lane (`-m "not slow"`); the full suite still runs them by default.
pytestmark = [pytest.mark.slow, pytest.mark.integration]


def _patch(pid: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id="instr",
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="test",
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


# ---------------------------------------------------------------------------
# Session-scoped seeded template
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _seeded_git_ws_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace with ``e1/v0`` seeded from the standard tree, built ONCE.

    Seeding costs a dozen-plus ``git`` subprocess spawns (init + identity +
    add + commit + tag + worktree); tests that only need "a seeded store"
    copy this template instead of paying that per test. Tests whose contract
    IS the seeding behaviour (epoch-branch creation, seed-from-worktree,
    custom trees) keep seeding a fresh store.
    """
    base = tmp_path_factory.mktemp("git-genstore-template")
    ws = base / "ws"
    GitGenerationStore(ws).seed_generation("e1", "v0", [mutable_tree(base / "src")])
    # A materialised worktree registers its ABSOLUTE path inside the repo,
    # which cannot survive relocation-by-copytree: drop the worktrees and
    # prune the registrations so each copy re-materialises its own on first
    # materialize_snapshot().
    shutil.rmtree(ws / GitGenerationStore.WORKTREES_DIRNAME)
    _git(ws / GitGenerationStore.REPO_DIRNAME, "worktree", "prune")
    return ws


@pytest.fixture
def seeded_store(_seeded_git_ws_template: Path, tmp_path: Path) -> GitGenerationStore:
    """A private copy of the seeded template — safe to derive/mutate."""
    ws = tmp_path / "ws"
    shutil.copytree(_seeded_git_ws_template, ws)
    return GitGenerationStore(ws)


# ---------------------------------------------------------------------------
# domain → git mapping
# ---------------------------------------------------------------------------


def test_epoch_becomes_a_branch(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("2026-05-18_e1", "v0", [mutable_tree(tmp_path / "src")])
    branches = _git(store.repo_path, "branch", "--list")
    assert "epoch/2026-05-18_e1" in branches


def test_generation_becomes_a_tag(seeded_store: GitGenerationStore) -> None:
    store = seeded_store
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""next"""')])
    tags = _git(store.repo_path, "tag", "--list").split()
    assert "epoch/e1/v0" in tags
    assert "epoch/e1/v1" in tags


def test_generation_lineage_is_the_commit_dag(seeded_store: GitGenerationStore) -> None:
    """A child generation commit parents the parent generation commit."""
    store = seeded_store
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""next"""')])

    v0 = _git(store.repo_path, "rev-parse", "epoch/e1/v0").strip()
    v1_parent = _git(store.repo_path, "rev-parse", "epoch/e1/v1^").strip()
    assert v1_parent == v0


# ---------------------------------------------------------------------------
# commit-message metadata block
# ---------------------------------------------------------------------------


def test_patch_metadata_travels_in_the_commit_message(
    seeded_store: GitGenerationStore,
) -> None:
    store = seeded_store
    store.derive_generation("e1", "v0", "v1", [_patch("p-meta", '"""x"""')])

    message = _git(store.repo_path, "log", "-1", "--format=%B", "epoch/e1/v1")
    assert "---zicato-meta---" in message
    assert "p-meta" in message


# ---------------------------------------------------------------------------
# worktrees
# ---------------------------------------------------------------------------


def test_materialize_snapshot_creates_a_worktree(seeded_store: GitGenerationStore) -> None:
    store = seeded_store
    root = store.materialize_snapshot("e1", "v0")
    assert root.is_dir()
    assert (root / "agent" / "prompts.py").is_file()
    # The worktree is registered with git.
    worktrees = _git(store.repo_path, "worktree", "list")
    assert str(root) in worktrees


def test_materialize_snapshot_reuses_an_existing_worktree(
    seeded_store: GitGenerationStore,
) -> None:
    first = seeded_store.materialize_snapshot("e1", "v0")
    second = seeded_store.materialize_snapshot("e1", "v0")
    assert first == second


def test_snapshot_path_for_missing_generation_is_pure(tmp_path: Path) -> None:
    """An unmaterialised coordinate yields a path without creating anything."""
    store = GitGenerationStore(tmp_path / "ws")
    root = store.snapshot_path("e1", "v0")
    assert not root.exists()


# ---------------------------------------------------------------------------
# blob dedup — the motivating payoff
# ---------------------------------------------------------------------------


def test_unchanged_files_share_one_blob_across_generations(tmp_path: Path) -> None:
    """A file unchanged across generations is ONE git blob, not N copies."""
    store = GitGenerationStore(tmp_path / "ws")
    tree = tmp_path / "src" / "agent"
    write_dedented(
        tree / "prompts.py",
        '# zicato:mutable id="instr"\nINSTR = """original"""\n',
    )
    # A second, never-mutated file.
    write_dedented(tree / "stable.py", "STABLE = 42\n")
    # Seed the ``agent`` tree itself, so paths are ``agent/...``.
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""next"""')])

    # stable.py is byte-identical in v0 and v1 -> same blob hash.
    v0_blob = _git(store.repo_path, "rev-parse", "epoch/e1/v0:agent/stable.py").strip()
    v1_blob = _git(store.repo_path, "rev-parse", "epoch/e1/v1:agent/stable.py").strip()
    assert v0_blob == v1_blob
    # prompts.py DID change -> different blob.
    v0_p = _git(store.repo_path, "rev-parse", "epoch/e1/v0:agent/prompts.py").strip()
    v1_p = _git(store.repo_path, "rev-parse", "epoch/e1/v1:agent/prompts.py").strip()
    assert v0_p != v1_p


# ---------------------------------------------------------------------------
# staging: the commit is derived from the working tree's bytes
# ---------------------------------------------------------------------------


#: A fixed modification time, long before any repository this test builds.
#: Both source trees carry it, so the copy hands the second ``git add`` a
#: file whose recorded and observed modification times agree.
_PINNED_MTIME = 1_600_000_000.0


def _same_size_source(root: Path, *, instr: str, payload: bytes) -> Path:
    """A two-file source tree pinned to :data:`_PINNED_MTIME`.

    ``instr`` is written into a fixed-width slot and ``payload`` is stored
    verbatim, so two trees differing only in those arguments hold files of
    identical size at identical paths.
    """
    tree = root / "agent"
    write_dedented(tree / "prompts.py", f'INSTR = """{instr}"""\n')
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    (tree / "bin" / "weights.dat").write_bytes(payload)
    for path in tree.rglob("*"):
        if path.is_file():
            os.utime(path, (_PINNED_MTIME, _PINNED_MTIME))
    return tree


def test_seeding_over_a_generation_records_a_change_a_stale_stat_would_hide(
    tmp_path: Path,
) -> None:
    """A second seed commits the bytes on disk whatever the index cached.

    ``git add`` re-hashes a file only when its stat data differs from the
    index entry standing for it. Seeding lays a source tree over the
    previous generation's files and preserves each file's modification
    time, so an edit that leaves a file's size alone can reach ``git add``
    looking untouched, and the previous generation's blob stays staged.
    Whether that happens in a given run turns on fields the operating
    system chooses: whether the copy reused the inode freed a moment
    earlier, and whether both writes landed in the second git records.
    This test removes both from the outcome: it pins one modification time
    across both trees and sets ``core.checkStat=minimal``, which drops
    inode, ownership and change time from the comparison. What remains —
    equal path, equal size, equal modification time — is what a store
    that trusts the stat cache would call unchanged.
    """
    forward = bytes(range(256))
    backward = bytes(reversed(range(256)))
    first = _same_size_source(tmp_path / "first", instr="alpha", payload=forward)
    second = _same_size_source(tmp_path / "second", instr="omega", payload=backward)

    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [first])
    _git(store.repo_path, "config", "core.checkStat", "minimal")
    store.seed_generation("e1", "v1", [second])

    assert store.read_file("e1", "v1", "agent/bin/weights.dat") == backward
    assert b'INSTR = """omega"""' in store.read_file("e1", "v1", "agent/prompts.py")
    diff = store.diff_generations("e1", "v0", "v1")
    assert "Binary files a/agent/bin/weights.dat and b/agent/bin/weights.dat differ\n" in diff


# ---------------------------------------------------------------------------
# artifact exclusion via .gitignore
# ---------------------------------------------------------------------------


def test_repo_carries_an_artifact_gitignore(seeded_store: GitGenerationStore) -> None:
    store = seeded_store
    gitignore = store.repo_path / ".gitignore"
    assert gitignore.is_file()
    body = gitignore.read_text(encoding="utf-8")
    assert "output" in body
    assert "__pycache__" in body


# ---------------------------------------------------------------------------
# config-knob selection
# ---------------------------------------------------------------------------


def test_default_generation_store_selects_git_off_config(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text('{"generation_source_backend": "git"}', encoding="utf-8")
    store = default_generation_store(ws)
    assert isinstance(store, GitGenerationStore)


def test_default_generation_store_selects_directory_off_config(tmp_path: Path) -> None:
    # ``generation_source_backend: "directory"`` stays selectable — flipping the
    # default to git did not remove the directory backend.
    from zicato.epoch.genstore import DirectoryGenerationStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text('{"generation_source_backend": "directory"}', encoding="utf-8")
    assert isinstance(default_generation_store(ws), DirectoryGenerationStore)


def test_default_generation_store_refuses_missing_config(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="workspace config not found"):
        default_generation_store(tmp_path / "ws")


def test_default_generation_store_refuses_malformed_config(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not parse"):
        default_generation_store(ws)


# ---------------------------------------------------------------------------
# stale-tag recovery (a retried failed round)
# ---------------------------------------------------------------------------


def test_retry_after_failed_derive_succeeds(seeded_store: GitGenerationStore) -> None:
    store = seeded_store

    # A failed derive (bad patch) leaves no v1 tag.
    with pytest.raises(ValueError):
        store.derive_generation(
            "e1",
            "v0",
            "v1",
            [
                Patch(
                    id="bad",
                    mutation_id="ghost",
                    op="replace",
                    new_content='"""x"""',
                    new_numeric=None,
                    new_enum=None,
                    rationale="t",
                )
            ],
        )
    assert store.has_generation("e1", "v1") is False

    # The retry with a good patch succeeds.
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""ok"""')])
    assert store.has_generation("e1", "v1") is True


# ---------------------------------------------------------------------------
# contract-roll seed: seed a v0 from a previous generation's *worktree*
# ---------------------------------------------------------------------------


def test_seed_from_a_worktrees_children_skips_git_admin(tmp_path: Path) -> None:
    """A contract roll seeds the next epoch's v0 from the previous epoch's
    promoted-head *worktree*, handing ``seed_generation`` the result of
    ``sorted(worktree.iterdir())`` — which includes the worktree's ``.git``
    pointer file and the repo ``.gitignore``. Those git-admin entries must
    be skipped, not copied into the new generation (copying the ``.git``
    pointer makes ``git add`` resolve it as a foreign repo and abort).
    """
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e0", "v0", [mutable_tree(tmp_path / "src", instr="rolled")])

    worktree = store.materialize_snapshot("e0", "v0")
    children = sorted(worktree.iterdir())
    # The worktree really does expose the git-admin entries we must skip.
    names = {c.name for c in children}
    assert ".git" in names
    assert ".gitignore" in names

    # The roll: seed e1/v0 from those children. Must not raise.
    store.seed_generation("e1", "v0", children)
    assert store.has_generation("e1", "v0") is True

    # The seeded tree carries the source content, and NO git-admin files.
    tree_paths = {e.path for e in store.list_tree("e1", "v0")}
    assert "agent/prompts.py" in tree_paths
    assert not any(p == ".git" or p.startswith(".git/") for p in tree_paths), tree_paths

    # And a child still derives cleanly off the rolled seed.
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""after-roll"""')])
    assert "after-roll" in store.read_file("e1", "v1", "agent/prompts.py").decode("utf-8")


def test_derive_an_unchanged_child_is_a_real_generation(
    seeded_store: GitGenerationStore,
) -> None:
    """A patch that leaves the tree byte-identical to the parent (a proposer
    can propose a value that already holds) is still a legitimate
    generation — the directory backend records it, so the git backend must
    too rather than aborting with "nothing to commit".
    """
    store = seeded_store

    # Replace the marker with the SAME content it already holds (the seeded
    # template's instr value), so the child tree is byte-identical to v0.
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""original"""')])

    assert store.has_generation("e1", "v1") is True
    assert sorted(store.list_generations("e1")) == ["v0", "v1"]
    # v1 commit parents v0 even though the tree is unchanged.
    v0 = _git(store.repo_path, "rev-parse", "epoch/e1/v0").strip()
    v1_parent = _git(store.repo_path, "rev-parse", "epoch/e1/v1^").strip()
    assert v1_parent == v0


# ---------------------------------------------------------------------------
# cold-store concurrent materialisation (regression probe for the
# _materialise_worktree inside-lock re-check)
# ---------------------------------------------------------------------------


def _run_cold_materialise_rep(
    store: GitGenerationStore,
    scratch_base: Path,
    rep: int,
    threads: int,
) -> None:
    """One rep of the cold-store probe: N threads derive from a COLD parent.

    The parent worktree is removed by the caller before this runs, so every
    thread reaches the cold ``_materialise_worktree`` path together and races
    on ``git worktree add``. Asserts all N derives succeed with disjoint,
    fully-intact trees (each carrying ONLY its own content).
    """
    contents = [f"rep{rep}-slot{i}" for i in range(threads)]
    scratch_roots = [scratch_base / f"rep{rep}" / f"s{i}" / "child" for i in range(threads)]

    def _derive(i: int) -> Path:
        return store.derive_scratch(
            epoch_id="e1",
            parent_generation_id="v0",
            patches=[_patch(f"p{rep}_{i}", f'"""{contents[i]}"""')],
            scratch_root=scratch_roots[i],
        )

    with ThreadPoolExecutor(max_workers=threads) as pool:
        # ``.result()`` re-raises any per-thread failure — a racing
        # ``git worktree add`` on an already-populated dir would surface here.
        results = [f.result() for f in [pool.submit(_derive, i) for i in range(threads)]]

    for i, root in enumerate(results):
        body = (root / "agent" / "prompts.py").read_text(encoding="utf-8")
        assert contents[i] in body, f"scratch {i} missing its own content"
        for j in range(threads):
            if j != i:
                assert contents[j] not in body, f"scratch {i} contaminated by slot {j}"


def test_cold_store_concurrent_derive_never_races_on_materialise(tmp_path: Path) -> None:
    """8 concurrent cold-store ``derive_scratch`` calls, repeated, never race.

    The reviewer's probe, pinned: from a COLD store (no pre-warmed parent
    worktree) 8 threads each trigger ``_materialise_worktree`` for the SAME
    parent. Before the inside-lock re-check, the second thread's
    ``git worktree add`` hit an already-populated directory and failed
    (~134/160 failures at 8×20 from cold). Each rep is forced cold by dropping
    the parent worktree first; every derive must succeed with an intact tree.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    store = GitGenerationStore(workspace)
    store.seed_generation("e1", "v0", [mutable_tree(tmp_path / "src")])
    worktrees = workspace / GitGenerationStore.WORKTREES_DIRNAME

    threads = 8
    reps = 5
    for rep in range(reps):
        # Force COLD: drop the parent worktree + prune its registration so this
        # rep's threads all hit the cold-materialisation race together.
        shutil.rmtree(worktrees, ignore_errors=True)
        _git(store.repo_path, "worktree", "prune")
        _run_cold_materialise_rep(store, tmp_path / "scratch", rep, threads)
