"""Shared snapshot-scope policy — what a generation source tree may contain.

A generation source tree (``docs/design/STORAGE.md`` §3) is meant to be
**code-only**: the inner-harness modules plus their support code, nothing
else. Two distinct mechanisms threaten that invariant and this module is
the single policy both consult:

1. **Run artifacts written next to the code.** The presentation-agent
   target writes its rendered webpage under ``output/`` *inside its own
   source directory*. Without a filter, every ``copytree`` that derives
   a child generation copies that ``output/`` forward — and it then
   compounds generation over generation. A long lineage exhausts disk.
   The fix is to never copy run artifacts into a generation in the
   first place.
2. **Tooling caches.** ``__pycache__``, ``.pytest_cache``,
   ``.mypy_cache``, ``.ruff_cache`` and friends are reconstructable
   noise. Copying them forward is wasted bytes and confuses a ``git
   diff`` between generations.

Both generation-store backends consume this module:

* :class:`~zicato.epoch.genstore.DirectoryGenerationStore` passes
  :func:`copytree_ignore` to every :func:`shutil.copytree` call.
* :class:`~zicato.epoch.git_genstore.GitGenerationStore` writes
  :func:`gitignore_lines` into the generation repo's ``.gitignore`` so
  the same names never enter a commit.

The policy is **name-based**, not path-based, because both consumers
need a cheap predicate they can apply at every directory level of a
walk without resolving absolute paths. A name in :data:`ARTIFACT_NAMES`
is an artifact wherever it appears in a tree.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

#: Directory / file names that are **never** part of a generation source
#: tree. Two groups:
#:
#: * Run output — ``output`` is where the vendored presentation target
#:   writes rendered pages; ``.zicato-scratch`` is the per-run scratch
#:   directory name the runner hands agents (see
#:   :data:`SCRATCH_DIR_ENV`). Neither is code.
#: * Tooling caches — reconstructable, never canonical.
#:
#: The set is deliberately small and conservative: every name here is
#: something a correct generation tree can regenerate or simply does not
#: need. Adding a name is a policy decision — it makes that name
#: invisible to *every* generation copy and *every* git commit.
ARTIFACT_NAMES: frozenset[str] = frozenset(
    {
        # --- run output -----------------------------------------------
        "output",
        ".zicato-scratch",
        # --- tooling caches -------------------------------------------
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".ipynb_checkpoints",
        # --- VCS / dependency dirs ------------------------------------
        # A registered mutable tree must never drag a nested git repo or
        # an installed-dependency directory into a generation snapshot.
        ".git",
        ".hg",
        "node_modules",
        ".venv",
        ".tox",
    }
)

#: Filename suffixes that mark a compiled / transient artifact regardless
#: of the containing directory. ``.pyc`` is the common one — a stray
#: ``.pyc`` outside a ``__pycache__`` dir is still not source.
ARTIFACT_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd")

#: Environment variable the tournament worker sets to a per-run scratch
#: directory **outside** the generation snapshot. A target that needs to
#: write run output reads this and writes there instead of next to its
#: own source. See ``zicato/adapters/base.py`` for the adapter contract
#: and ``examples/target_1_presentation/agent/agent.py`` for a consumer.
SCRATCH_DIR_ENV: str = "ZICATO_RUN_SCRATCH_DIR"

#: Basename of the per-run scratch directory the runner creates when an
#: adapter does not otherwise specify one. Kept in :data:`ARTIFACT_NAMES`
#: so that even if a scratch directory is ever created inside a tree it
#: is never copied forward.
SCRATCH_DIR_NAME: str = ".zicato-scratch"


def is_artifact(path: str | Path) -> bool:
    """Return ``True`` when ``path``'s basename is a run/cache artifact.

    The check is purely on the basename — a *name*, not a location. This
    is the predicate both generation-store backends and the dashboard
    file-tree walk use to decide whether to skip an entry. It does no
    I/O and does not require the path to exist.
    """
    name = Path(path).name
    if name in ARTIFACT_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in ARTIFACT_SUFFIXES)


def copytree_ignore(
    extra_names: Iterable[str] = (),
) -> Callable[[str, list[str]], set[str]]:
    """Return a :func:`shutil.copytree`-compatible ``ignore`` callable.

    The returned callable matches the ``copytree(ignore=...)`` contract:
    given a source directory and the list of its entries, it returns the
    subset to skip. Every name in :data:`ARTIFACT_NAMES`, every entry
    whose suffix is in :data:`ARTIFACT_SUFFIXES`, and any name in
    ``extra_names`` (an adapter-declared run-output name, say) is
    skipped — at *every* directory level of the walk, because
    :func:`shutil.copytree` invokes ``ignore`` once per directory.

    Passing the result to both :meth:`GenerationStore.seed_generation`
    and :meth:`GenerationStore.derive_generation`'s copies is what keeps
    a generation source tree code-only and stops run output compounding
    across a lineage.
    """
    extra = frozenset(extra_names)

    def _ignore(_src: str, names: list[str]) -> set[str]:
        skip: set[str] = set()
        for name in names:
            if name in ARTIFACT_NAMES or name in extra:
                skip.add(name)
            elif any(name.endswith(suffix) for suffix in ARTIFACT_SUFFIXES):
                skip.add(name)
        return skip

    return _ignore


def gitignore_lines(extra_names: Iterable[str] = ()) -> list[str]:
    """Return ``.gitignore`` lines excluding every artifact name.

    The git generation store writes these into the generation repo's
    ``.gitignore`` so the artifact set is excluded from every commit —
    the git-backend equivalent of :func:`copytree_ignore`. A name is
    emitted both bare and as ``name/`` is unnecessary: a bare entry in
    ``.gitignore`` already matches a directory of that name at any
    depth. Suffix artifacts are emitted as ``*.pyc`` style globs.

    ``extra_names`` carries adapter-declared run-output names, the same
    way :func:`copytree_ignore` accepts them, so the two backends honour
    an identical exclusion set.
    """
    lines = ["# zicato: generation snapshots are code-only — see snapshot_scope.py"]
    for name in sorted(ARTIFACT_NAMES | frozenset(extra_names)):
        lines.append(name)
    for suffix in ARTIFACT_SUFFIXES:
        lines.append(f"*{suffix}")
    return lines


__all__ = [
    "ARTIFACT_NAMES",
    "ARTIFACT_SUFFIXES",
    "SCRATCH_DIR_ENV",
    "SCRATCH_DIR_NAME",
    "is_artifact",
    "copytree_ignore",
    "gitignore_lines",
]
