"""Git-specific tests for :class:`~zicato.epoch.git_genstore.GitGenerationStore`.

The cross-backend protocol contract is covered by
``tests/test_genstore_conformance.py``. This file pins behaviour that is
*specific* to the git backend: the domain → git mapping (branches per
epoch, tags per generation), the commit-message metadata block, worktree
materialisation, blob dedup, and config-knob selection.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from zicato.core.types import Patch
from zicato.epoch.genstore import default_generation_store
from zicato.epoch.git_genstore import GitGenerationStore


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _mutable_tree(root: Path, *, instr: str = "original") -> Path:
    tree = root / "agent"
    _write(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="instr"
        INSTR = """{instr}"""
        ''',
    )
    return tree


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
# domain → git mapping
# ---------------------------------------------------------------------------


def test_epoch_becomes_a_branch(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("2026-05-18_e1", "v0", [_mutable_tree(tmp_path / "src")])
    branches = _git(store.repo_path, "branch", "--list")
    assert "epoch/2026-05-18_e1" in branches


def test_generation_becomes_a_tag(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""next"""')])
    tags = _git(store.repo_path, "tag", "--list").split()
    assert "epoch/e1/v0" in tags
    assert "epoch/e1/v1" in tags


def test_generation_lineage_is_the_commit_dag(tmp_path: Path) -> None:
    """A child generation commit parents the parent generation commit."""
    store = GitGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""next"""')])

    v0 = _git(store.repo_path, "rev-parse", "epoch/e1/v0").strip()
    v1_parent = _git(store.repo_path, "rev-parse", "epoch/e1/v1^").strip()
    assert v1_parent == v0


# ---------------------------------------------------------------------------
# commit-message metadata block
# ---------------------------------------------------------------------------


def test_patch_metadata_travels_in_the_commit_message(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    tree = _mutable_tree(tmp_path / "src")
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [_patch("p-meta", '"""x"""')])

    message = _git(store.repo_path, "log", "-1", "--format=%B", "epoch/e1/v1")
    assert "---zicato-meta---" in message
    assert "p-meta" in message

    # And it round-trips back through the read API.
    record = store.list_patches("e1", "v1")
    assert [p.id for p in record.patches] == ["p-meta"]


def test_seed_generation_has_no_patches(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
    assert store.list_patches("e1", "v0").patches == ()


# ---------------------------------------------------------------------------
# worktrees
# ---------------------------------------------------------------------------


def test_snapshot_root_materialises_a_worktree(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
    root = store.snapshot_root("e1", "v0")
    assert root.is_dir()
    assert (root / "agent" / "prompts.py").is_file()
    # The worktree is registered with git.
    worktrees = _git(store.repo_path, "worktree", "list")
    assert str(root) in worktrees


def test_snapshot_root_reuses_an_existing_worktree(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
    first = store.snapshot_root("e1", "v0")
    second = store.snapshot_root("e1", "v0")
    assert first == second


def test_snapshot_root_for_missing_generation_is_pure_path(tmp_path: Path) -> None:
    """An unmaterialised coordinate yields a path without creating anything."""
    store = GitGenerationStore(tmp_path / "ws")
    root = store.snapshot_root("e1", "v0")
    assert not root.exists()


# ---------------------------------------------------------------------------
# blob dedup — the motivating payoff
# ---------------------------------------------------------------------------


def test_unchanged_files_share_one_blob_across_generations(tmp_path: Path) -> None:
    """A file unchanged across generations is ONE git blob, not N copies."""
    store = GitGenerationStore(tmp_path / "ws")
    tree = tmp_path / "src" / "agent"
    _write(
        tree / "prompts.py",
        '# zicato:mutable id="instr"\nINSTR = """original"""\n',
    )
    # A second, never-mutated file.
    _write(tree / "stable.py", "STABLE = 42\n")
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
# artifact exclusion via .gitignore
# ---------------------------------------------------------------------------


def test_repo_carries_an_artifact_gitignore(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
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
    (ws / "config.json").write_text('{"storage_backend": "git"}', encoding="utf-8")
    store = default_generation_store(ws)
    assert isinstance(store, GitGenerationStore)


def test_default_generation_store_defaults_to_directory(tmp_path: Path) -> None:
    from zicato.epoch.genstore import DirectoryGenerationStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text('{"storage_backend": "directory"}', encoding="utf-8")
    assert isinstance(default_generation_store(ws), DirectoryGenerationStore)


def test_default_generation_store_no_config_is_directory(tmp_path: Path) -> None:
    from zicato.epoch.genstore import DirectoryGenerationStore

    assert isinstance(default_generation_store(tmp_path / "ws"), DirectoryGenerationStore)


def test_default_generation_store_malformed_config_is_directory(
    tmp_path: Path,
) -> None:
    from zicato.epoch.genstore import DirectoryGenerationStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text("{not json", encoding="utf-8")
    assert isinstance(default_generation_store(ws), DirectoryGenerationStore)


# ---------------------------------------------------------------------------
# stale-tag recovery (a retried failed round)
# ---------------------------------------------------------------------------


def test_retry_after_failed_derive_succeeds(tmp_path: Path) -> None:
    store = GitGenerationStore(tmp_path / "ws")
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])

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
