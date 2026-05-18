"""Tests for :mod:`zicato.epoch.snapshot_scope` — the artifact-exclusion policy.

The snapshot-scope module is the single policy both generation-store
backends consult to keep a generation source tree code-only. These
tests pin its observable contract: which names count as artifacts, the
``shutil.copytree``-compatible ignore callable, and the ``.gitignore``
line generation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from zicato.epoch.snapshot_scope import (
    ARTIFACT_NAMES,
    SCRATCH_DIR_ENV,
    copytree_ignore,
    gitignore_lines,
    is_artifact,
)

# ---------------------------------------------------------------------------
# is_artifact
# ---------------------------------------------------------------------------


def test_output_dir_is_an_artifact() -> None:
    assert is_artifact("output") is True
    assert is_artifact("/some/path/output") is True


def test_caches_are_artifacts() -> None:
    for name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        assert is_artifact(name) is True, name


def test_pyc_files_are_artifacts() -> None:
    assert is_artifact("module.pyc") is True
    assert is_artifact("pkg/sub/thing.pyo") is True


def test_source_files_are_not_artifacts() -> None:
    assert is_artifact("agent.py") is False
    assert is_artifact("prompts.py") is False
    assert is_artifact("README.md") is False


def test_scratch_dir_name_is_an_artifact() -> None:
    """The per-run scratch dir name is excluded even if it lands in a tree."""
    assert is_artifact(".zicato-scratch") is True


# ---------------------------------------------------------------------------
# copytree_ignore
# ---------------------------------------------------------------------------


def test_copytree_ignore_drops_artifacts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "agent").mkdir(parents=True)
    (src / "agent" / "prompts.py").write_text("X = 1\n")
    (src / "output").mkdir()
    (src / "output" / "page.html").write_text("noise")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "agent.cpython-311.pyc").write_text("bytecode")

    dst = tmp_path / "dst"
    shutil.copytree(src, dst, ignore=copytree_ignore())

    assert (dst / "agent" / "prompts.py").exists()
    assert not (dst / "output").exists()
    assert not (dst / "__pycache__").exists()


def test_copytree_ignore_honours_extra_names(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.py").write_text("X = 1\n")
    (src / "renders").mkdir()
    (src / "renders" / "out.html").write_text("noise")

    dst = tmp_path / "dst"
    shutil.copytree(src, dst, ignore=copytree_ignore(extra_names=["renders"]))

    assert (dst / "keep.py").exists()
    assert not (dst / "renders").exists()


def test_copytree_ignore_drops_artifacts_at_every_level(tmp_path: Path) -> None:
    """The ignore predicate fires at every nested directory, not just the root."""
    src = tmp_path / "src"
    (src / "a" / "b" / "output").mkdir(parents=True)
    (src / "a" / "b" / "output" / "deep.html").write_text("noise")
    (src / "a" / "b" / "code.py").write_text("Y = 2\n")

    dst = tmp_path / "dst"
    shutil.copytree(src, dst, ignore=copytree_ignore())

    assert (dst / "a" / "b" / "code.py").exists()
    assert not (dst / "a" / "b" / "output").exists()


# ---------------------------------------------------------------------------
# gitignore_lines
# ---------------------------------------------------------------------------


def test_gitignore_lines_cover_every_artifact_name() -> None:
    lines = set(gitignore_lines())
    for name in ARTIFACT_NAMES:
        assert name in lines, f"{name} missing from .gitignore lines"


def test_gitignore_lines_include_pyc_glob() -> None:
    assert "*.pyc" in gitignore_lines()


def test_gitignore_lines_honour_extra_names() -> None:
    lines = gitignore_lines(extra_names=["renders"])
    assert "renders" in lines


# ---------------------------------------------------------------------------
# scratch-dir env var contract
# ---------------------------------------------------------------------------


def test_scratch_dir_env_var_name_is_stable() -> None:
    """The env var name is a cross-zone contract — pin it."""
    assert SCRATCH_DIR_ENV == "ZICATO_RUN_SCRATCH_DIR"
