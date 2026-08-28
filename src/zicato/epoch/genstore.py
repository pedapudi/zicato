"""The :class:`GenerationStore` seam — generation source trees as a pluggable store.

zicato persists five kinds of data (``docs/design/STORAGE.md`` §1). Four
of them are *records* — key→blob — and go through
:class:`zicato.storage.StorageBackend`. The fifth, **generation source
trees**, is not record-shaped: a generation is a whole inner-harness
source tree, and the unit of work is "the generation appears, or it does
not." This module is the seam for that fifth kind.

Why a separate seam (and not an extension of ``StorageBackend``)
----------------------------------------------------------------
``StorageBackend`` is deliberately record-level: ``write_json(key,
data)`` cannot express "materialise generation v3 as a source tree
derived from v2's tree by applying this patch set." The two seams have
genuinely different units — a record store's unit is a key→blob pair
with per-record atomicity; a generation store's unit is a source tree
plus a generation-level transaction boundary. Forcing the second
through the first would make ``StorageBackend`` carry zicato's domain
vocabulary (epochs, generations, patches) and stop being an honest
storage seam. So :class:`GenerationStore` is a **peer abstraction at the
domain layer**, not a subtype of ``StorageBackend``. The full reasoning
is ``docs/design/STORAGE.md`` §4.

Path calculation and materialization
------------------------------------
:meth:`GenerationStore.snapshot_path` is pure coordinate-to-path math.
:meth:`GenerationStore.materialize_snapshot` performs any I/O needed to
return a usable local tree. The separation matters for git: calculating a
worktree path must not silently run ``git worktree add``. Tournament workers
still receive real paths because they load and execute the inner harness from
an isolated checkout.

Backends
--------
* :class:`~zicato.epoch.git_genstore.GitGenerationStore` — the **default**
  backend (``docs/design/STORAGE.md`` §7). A generation is a commit on an
  epoch branch, materialised for a run as a content-addressed
  ``git worktree``. Because the object store dedups unchanged blobs across
  a lineage, and the worktree checkout *is* the isolated per-run tree, the
  git backend removes both the per-generation and the per-run ``copytree``
  the directory backend pays. Operators can inspect the private repository
  with ordinary Git commands; zicato does not migrate obsolete directory-
  backed workspaces.
* :class:`DirectoryGenerationStore` — the directory-snapshot backend,
  selected by ``generation_source_backend: "directory"``. A generation is a
  ``generations/{id}/snapshot/`` directory; deriving a child is a
  ``copytree`` of the parent plus an all-or-nothing patch apply. This IS
  the pre-seam directory-snapshot mechanism, byte-for-byte. It remains a
  fully supported, config-selectable backend for environments where a
  private git repo is unwanted; the git default simply removes the copy
  cost for the common case.

Every initialized workspace records ``generation_source_backend`` explicitly.
:func:`resolve_generation_store_backend` validates that field and never infers
the answer from repositories, generation records, or snapshot directories.
"""

from __future__ import annotations

import difflib
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from zicato.core.types import Patch
from zicato.core.workspace import generation_dir
from zicato.epoch.snapshot_scope import copytree_ignore, is_artifact
from zicato.workspace import WorkspaceLayout

#: Filename prefix for a run's ephemeral checkout parent directory. The
#: parent lives in the system temp dir (``tempfile.mkdtemp``) so it never
#: sits inside the workspace tree — nothing under it can be mistaken for
#: a canonical generation snapshot — and it is removed when the run ends.
#: The Rust supervisor's crash-GC (``crates/supervisor/src/reap.rs``,
#: ``SNAPSHOT_PREFIX``) reaps orphaned parents by exactly this prefix +
#: temp-dir placement, so both properties are load-bearing: every
#: backend's :meth:`GenerationStore.checkout_ephemeral` MUST place its
#: per-run tree under a ``ztw-snap-*`` mkdtemp parent in the temp dir.
EPHEMERAL_SNAPSHOT_PREFIX = "ztw-snap-"

#: Basename of the per-run scratch directory inside the ephemeral
#: checkout parent. The worker exports it to the harness under test via
#: :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` so run output is
#: routed OUTSIDE the source tree.
EPHEMERAL_SCRATCH_DIRNAME = "run-scratch"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One node in a generation source tree, for the dashboard file browser.

    The dashboard's file-tree view (``zicato/dashboard/``) renders a
    generation's source as a tree without knowing whether it is backed
    by a directory snapshot or a git commit. :meth:`GenerationStore.list_tree`
    returns these — a backend-neutral description of one tree node.

    Attributes
    ----------
    path:
        ``/``-separated path **relative to the generation's source
        root**. Always forward-slash, on every platform, so the wire
        shape the dashboard consumes is stable.
    is_dir:
        ``True`` for a directory node, ``False`` for a file.
    size:
        File size in bytes; ``0`` for a directory.
    """

    path: str
    is_dir: bool
    size: int


@dataclass(frozen=True, slots=True)
class EphemeralCheckout:
    """One run's throwaway working copy of a generation source tree.

    Returned by :meth:`GenerationStore.checkout_ephemeral`. The shape is
    a triple rather than a bare ``(working_dir, cleanup)`` pair because
    the per-run scratch directory is part of the same contract: run
    output must be routed OUTSIDE the source tree (the
    :data:`~zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` contract), the
    scratch directory's placement is backend-owned (it shares the
    checkout's crash-reapable ``ztw-snap-*`` parent so one cleanup — or
    the supervisor's reaper — removes both), and under a shared-tree
    backend design it could not be derived from ``working_dir`` at all.

    Attributes
    ----------
    working_dir:
        The isolated per-run source tree the worker mounts as the inner
        harness's root. Its basename matches the canonical
        :meth:`GenerationStore.snapshot_path`'s basename so any path the
        agent derives from ``__file__`` looks the same as it would under
        the canonical tree.
    scratch_dir:
        The per-run scratch directory the worker exports via
        :data:`~zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV`. A sibling
        of ``working_dir`` under the same ``ztw-snap-*`` parent.
    cleanup:
        Idempotent, best-effort teardown removing the whole checkout
        (working dir AND scratch dir). Callers invoke it from a
        ``finally`` block; a cleanup failure must never turn a finished
        run into a crash. Crash-safety does not depend on it — the
        supervisor's reaper GCs orphaned ``ztw-snap-*`` parents.
    """

    working_dir: Path
    scratch_dir: Path
    cleanup: Callable[[], None] = field(compare=False)


def discard_ephemeral_parent(parent: Path) -> None:
    """Best-effort removal of one ephemeral checkout's ``ztw-snap-*`` parent.

    The shared cleanup mechanism behind every backend's
    :attr:`EphemeralCheckout.cleanup`: removes the whole mkdtemp parent
    (working dir and scratch dir together) so no empty temp directory is
    left behind. Idempotent, and never raises — a cleanup failure must
    not turn a finished run into a crash.
    """
    try:
        shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass  # Best-effort cleanup must not turn a completed run into a failure.


def copy_checkout_ephemeral(source_root: Path, run_id: str) -> EphemeralCheckout:
    """Materialise an ephemeral checkout by copying ``source_root``.

    The ``copytree``-based mechanism — the directory backend's
    :meth:`~DirectoryGenerationStore.checkout_ephemeral` and the
    runner's fallback for a store-unmanaged generation (an ad-hoc caller
    whose :class:`~zicato.core.epoch.Generation` points at an arbitrary
    tree). This IS the historical per-run ephemeral-snapshot behaviour,
    byte-for-byte:

    * a single :func:`tempfile.mkdtemp` parent named
      ``ztw-snap-{run_id}-*`` in the OS temp dir, deliberately OUTSIDE
      the workspace tree (and exactly the shape the Rust supervisor's
      ``reapable_snapshot_root`` guard reaps after a crash);
    * the copy goes *into a child of the parent keeping the source
      tree's own basename*, so any path the agent derives from
      ``__file__`` looks the same as it would under the canonical
      snapshot;
    * the copy is filtered through the shared snapshot-scope ignore so
      run artifacts are never carried in;
    * a sibling ``run-scratch`` directory under the same parent, so one
      cleanup removes both.
    """
    parent = Path(tempfile.mkdtemp(prefix=f"{EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-"))
    working_dir = parent / Path(source_root).name
    shutil.copytree(source_root, working_dir, ignore=copytree_ignore())
    scratch_dir = parent / EPHEMERAL_SCRATCH_DIRNAME
    scratch_dir.mkdir()

    def _cleanup() -> None:
        discard_ephemeral_parent(parent)

    return EphemeralCheckout(working_dir=working_dir, scratch_dir=scratch_dir, cleanup=_cleanup)


def source_tree_bytes(root: Path) -> int:
    """Return a best-effort byte count for regular files under ``root``."""
    if not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _diff_materialized_trees(from_root: Path, to_root: Path) -> str:
    """Return a deterministic unified diff between two local source trees."""
    from_files = {
        path.relative_to(from_root).as_posix(): path
        for path in from_root.rglob("*")
        if path.is_file()
        and not any(is_artifact(part) for part in path.relative_to(from_root).parts)
    }
    to_files = {
        path.relative_to(to_root).as_posix(): path
        for path in to_root.rglob("*")
        if path.is_file() and not any(is_artifact(part) for part in path.relative_to(to_root).parts)
    }
    lines: list[str] = []
    for rel_path in sorted(from_files.keys() | to_files.keys()):
        before = from_files.get(rel_path)
        after = to_files.get(rel_path)
        before_lines = (
            before.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if before is not None
            else []
        )
        after_lines = (
            after.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if after is not None
            else []
        )
        lines.extend(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel_path}" if before is not None else "/dev/null",
                tofile=f"b/{rel_path}" if after is not None else "/dev/null",
            )
        )
    return "".join(lines)


@runtime_checkable
class GenerationStore(Protocol):
    """The pluggable store for generation source trees.

    Every method is keyed by an ``(epoch_id, generation_id)`` coordinate
    — the same string identifiers the filesystem layout and the
    analytical index use. A store implementation owns *how* a generation
    is materialised and *where* its source tree lives; callers
    (the orchestrator, the baseline seeder) compose those coordinates
    and never reach past this seam.

    The protocol is intentionally small: enough to seed a baseline,
    derive a child from a parent, and hand a worker a path to mount.
    Anything record-shaped (``experiment.json``, ``gen_score.json``, the
    journal) is NOT here — that is :class:`zicato.storage.StorageBackend`
    territory.
    """

    @property
    def backend_name(self) -> str:
        """Return the configured generation-source backend name."""
        ...

    def snapshot_path(self, epoch_id: str, generation_id: str) -> Path:
        """Return the backend-owned local path for one generation snapshot.

        This is a pure coordinate→path computation — it does NOT assert
        the generation exists (use :meth:`has_generation` for that) and
        does NOT materialise anything.
        """
        ...

    def materialize_snapshot(self, epoch_id: str, generation_id: str) -> Path:
        """Ensure a generation has a local source tree and return its path.

        Raises :class:`FileNotFoundError` when the generation does not exist.
        """
        ...

    def has_generation(self, epoch_id: str, generation_id: str) -> bool:
        """Return ``True`` iff source state exists and can be materialized."""
        ...

    def list_generations(self, epoch_id: str) -> list[str]:
        """Return every source-backed generation id under ``epoch_id``, sorted."""
        ...

    def seed_generation(
        self,
        epoch_id: str,
        generation_id: str,
        sources: Iterable[Path],
    ) -> Path:
        """Materialise a seed generation from one or more source trees.

        Each path in ``sources`` is copied into the generation's source
        tree under its own basename — the ``v0`` layout the orchestrator
        produces from the registered ``mutable_trees``. A single source
        file (rare) is copied directly. Returns the materialized source path.

        A seed generation has no parent and no patch set — it is the
        epoch's baseline. Deriving every *subsequent* generation goes
        through :meth:`derive_generation`.
        """
        ...

    def derive_generation(
        self,
        epoch_id: str,
        parent_generation_id: str,
        child_generation_id: str,
        patches: Sequence[Patch],
    ) -> Path:
        """Materialise a child generation by applying ``patches`` to the parent.

        This is the generation-level transaction boundary the record
        seam cannot express. The child's source tree is derived from the
        parent's by applying the patch set **all-or-nothing**: if any
        patch is malformed, nothing is materialised and the call raises.
        The child generation appears in full, or not at all — there is
        never a half-applied child tree.

        Returns the child's materialized source path.

        Raises
        ------
        FileNotFoundError
            When the parent generation has no materialised source tree.
        ValueError
            When the patch set fails validation — no child tree is left
            behind.
        """
        ...

    def derive_scratch(
        self,
        epoch_id: str,
        parent_generation_id: str,
        patches: Sequence[Patch],
        scratch_root: Path,
    ) -> Path:
        """Materialise a THROWAWAY child tree at ``scratch_root``, off-namespace.

        The concurrency-safe sibling of :meth:`derive_generation`. It
        applies ``patches`` to the parent's source tree all-or-nothing into
        the caller-owned ``scratch_root`` — a disjoint temp directory the
        caller allocates and cleans up — and does NOT touch the generation
        namespace: it creates NO commit, NO tag, NO branch/working-tree
        state, and never writes under ``generations/`` (directory backend)
        or the epoch branch (git backend). A scratch tree is therefore
        **provably invisible to every walker** — :meth:`list_generations`,
        the GC, the reindex, the lineage reader and the dashboard readers
        all enumerate ``generations/`` directories (directory backend) or
        ``epoch/{id}/*`` tags (git backend), never a caller temp dir.

        This is what makes the best-of-N slate loop gatherable: each slot
        validates into its own ``scratch_root``, so two concurrent slots
        derive against fully disjoint trees and race on nothing (the shared
        ``next_id`` derive that blocked the gather is done exactly ONCE, for
        the chosen candidate, via :meth:`derive_generation` after selection).

        ``scratch_root`` is cleared first if it exists (an idempotent retry
        re-derives cleanly, exactly like :meth:`derive_generation`'s child
        clear). Returns ``scratch_root``.

        Raises
        ------
        FileNotFoundError
            When the parent generation has no materialised source tree.
        ValueError
            When the patch set fails validation — no scratch tree is left
            behind.
        """
        ...

    def checkout_ephemeral(
        self,
        epoch_id: str,
        generation_id: str,
        run_id: str,
    ) -> EphemeralCheckout:
        """Materialise an ISOLATED per-run working copy of a generation.

        A tournament worker is never pointed at the canonical source
        tree (:meth:`materialize_snapshot`): the canonical tree is what
        :meth:`derive_generation` derives every child from, so any
        runtime write into it would accumulate across the whole lineage.
        Instead each run mounts a throwaway checkout this method
        materialises — a stray write that lands next to the agent's own
        code pollutes only the checkout, which is discarded when the run
        ends.

        Contract (every backend):

        * the checkout lives under a fresh ``ztw-snap-{run_id}-*``
          parent in the OS temp dir (:data:`EPHEMERAL_SNAPSHOT_PREFIX`),
          the shape the supervisor's crash-reaper GCs;
        * ``working_dir``'s basename equals the canonical
          :meth:`snapshot_path`'s basename (``__file__``-derived paths
          look identical);
        * a per-run ``scratch_dir`` sibling is created for the
          :data:`~zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` contract;
        * concurrent checkouts of the SAME generation are mutually
          isolated;
        * ``cleanup()`` is idempotent and best-effort.

        Raises :class:`FileNotFoundError` when the generation has no
        materialised source tree, and :class:`OSError` when the checkout
        could not be materialised (callers degrade that to an aborted
        run, never a crashed tournament).
        """
        ...

    # ------------------------------------------------------------------
    # Read surface — the dashboard file-tree / file-browser API.
    #
    # These methods are *read-only* and backend-neutral: the dashboard
    # (``zicato/dashboard/``) calls them to render a generation's source
    # tree and its applied patches without knowing whether the backend
    # is a directory snapshot or a git repo. The directory backend walks
    # the snapshot directory; the git backend reads a commit's tree.
    # ------------------------------------------------------------------

    def list_tree(self, epoch_id: str, generation_id: str) -> list[TreeEntry]:
        """Return every file and directory in a generation's source tree.

        Each :class:`TreeEntry` carries a ``/``-separated path relative
        to the generation's source root. Run artifacts
        (:mod:`zicato.epoch.snapshot_scope`) are excluded — they are not
        part of a generation. The list is sorted for a deterministic
        render order.

        Raises :class:`FileNotFoundError` when the generation has no
        materialised source tree.
        """
        ...

    def read_file(self, epoch_id: str, generation_id: str, rel_path: str) -> bytes:
        """Return the raw bytes of one file in a generation's source tree.

        ``rel_path`` is a ``/``-separated path relative to the
        generation's source root — the ``path`` of a file
        :class:`TreeEntry`. Implementations MUST reject any ``rel_path``
        that escapes the source root (``..`` traversal) with a
        :class:`ValueError`.

        Raises
        ------
        FileNotFoundError
            When the generation or the file does not exist.
        ValueError
            When ``rel_path`` escapes the generation source root, or
            names a directory rather than a file.
        """
        ...

    def diff_generations(
        self,
        epoch_id: str,
        from_generation_id: str,
        to_generation_id: str,
    ) -> str:
        """Return a unified source diff between two generations."""
        ...

    def prune_generations(
        self,
        epoch_id: str,
        generation_ids: Sequence[str],
        *,
        dry_run: bool,
    ) -> int:
        """Prune selected generation source trees and return reclaimable bytes.

        Record-shaped generation data is outside this seam and must remain
        untouched. A dry run computes the byte count without changing state.
        The batch shape lets a backend perform shared maintenance once after
        removing all selected source trees.
        """
        ...


class DirectoryGenerationStore:
    """The directory-snapshot :class:`GenerationStore` implementation.

    A generation's source tree is the ``generations/{id}/snapshot/``
    directory under its epoch. Deriving a child is a full ``copytree``
    of the parent's snapshot followed by an all-or-nothing patch apply
    (via :func:`zicato.mutation.applier.apply_patches`, whose
    deterministic pre-validation already makes the apply atomic).

    This backend is the pre-seam mechanism the orchestrator used
    directly — the same paths, the same ``copytree``, and the same
    applier. The seam lets callers use it interchangeably with the git
    backend without changing its on-disk representation.

    The store is rooted at a workspace directory (the ``.zicato/``
    directory) and computes every path under it via
    :func:`zicato.core.workspace.generation_dir` — there is one
    definition of the workspace path layout and this backend uses it.
    """

    def __init__(self, workspace_root: Path) -> None:
        """Create a store rooted at ``workspace_root`` (the ``.zicato/`` dir)."""
        self._workspace_root = Path(workspace_root)

    @property
    def workspace_root(self) -> Path:
        """The workspace directory every generation path resolves under."""
        return self._workspace_root

    @property
    def backend_name(self) -> str:
        """Return the workspace config name for this backend."""
        return DIRECTORY_BACKEND

    def snapshot_path(self, epoch_id: str, generation_id: str) -> Path:
        """Return ``generations/{generation_id}/snapshot/`` for the coordinate.

        Pure path math — no I/O, no assertion that the generation
        exists. Mirrors the orchestrator's pre-seam ``_snapshot_root``
        helper exactly.
        """
        return generation_dir(self._workspace_root, epoch_id, generation_id) / "snapshot"

    def materialize_snapshot(self, epoch_id: str, generation_id: str) -> Path:
        """Return an existing directory snapshot without creating it."""
        path = self.snapshot_path(epoch_id, generation_id)
        if not path.is_dir():
            raise FileNotFoundError(
                f"generation {epoch_id}/{generation_id} has no source tree at {path}"
            )
        return path

    def has_generation(self, epoch_id: str, generation_id: str) -> bool:
        """Return ``True`` iff the generation's ``snapshot/`` directory exists."""
        return self.snapshot_path(epoch_id, generation_id).is_dir()

    def list_generations(self, epoch_id: str) -> list[str]:
        """Return the ids of every available directory snapshot, sorted.

        Record-only generation directories survive source pruning and are not
        part of this source-store listing. Record consumers enumerate them
        through the workspace layout or ``StorageBackend`` instead.
        """
        gens_root = WorkspaceLayout.from_root(self._workspace_root).generations_dir(epoch_id)
        if not gens_root.is_dir():
            return []
        return sorted(
            child.name
            for child in gens_root.iterdir()
            if child.is_dir() and (child / SNAPSHOT_DIRNAME).is_dir()
        )

    def seed_generation(
        self,
        epoch_id: str,
        generation_id: str,
        sources: Iterable[Path],
    ) -> Path:
        """Copy each source tree into the generation's ``snapshot/`` directory.

        Each source is copied under its own basename — a directory via
        :func:`shutil.copytree`, a single file via :func:`shutil.copy2`.
        This is the ``v0`` baseline layout the orchestrator's
        ``_ensure_baseline_snapshot`` produced inline before the seam
        existed.

        Raises
        ------
        FileNotFoundError
            When a source path does not exist on disk.
        """
        snapshot_root = self.snapshot_path(epoch_id, generation_id)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        for raw in sources:
            source = Path(raw).resolve()
            if not source.exists():
                raise FileNotFoundError(
                    f"seed_generation: source tree {source} does not exist on disk"
                )
            target = snapshot_root / source.name
            if source.is_file():
                shutil.copy2(source, target)
            else:
                # The copy is filtered through the shared snapshot-scope
                # policy: run artifacts (``output/``, caches) are never
                # copied into a generation. Without this a registered
                # tree's existing ``output/`` would seed v0 and then
                # compound across the whole lineage.
                shutil.copytree(source, target, ignore=copytree_ignore())
        return snapshot_root

    def derive_generation(
        self,
        epoch_id: str,
        parent_generation_id: str,
        child_generation_id: str,
        patches: Sequence[Patch],
    ) -> Path:
        """Copy the parent snapshot and apply ``patches`` all-or-nothing.

        Behaviour:

        1. Resolve the parent's ``snapshot/`` directory; raise
           :class:`FileNotFoundError` if it is absent.
        2. If a stale child ``snapshot/`` directory exists (a previous
           failed round), remove it — the applier refuses to overwrite,
           and a half-built tree from a crashed round must not block a
           retry.
        3. Delegate to :func:`zicato.mutation.applier.apply_patches`,
           which copies the parent tree to the child path and applies
           the patch set with deterministic pre-validation — so a
           malformed patch set leaves *no* child tree behind.

        Returns the child's materialized source path.
        """
        from zicato.mutation.applier import apply_patches  # noqa: PLC0415

        parent_root = self.snapshot_path(epoch_id, parent_generation_id)
        if not parent_root.is_dir():
            raise FileNotFoundError(
                f"derive_generation: parent generation {epoch_id}/"
                f"{parent_generation_id} has no source tree at {parent_root}"
            )
        child_root = self.snapshot_path(epoch_id, child_generation_id)
        if child_root.exists():
            # A previous failed round may have left a partial snapshot.
            # The applier refuses to overwrite, so clear the tree first.
            # Only the snapshot subdirectory is removed — any sibling
            # debug data under the generation directory is left alone.
            shutil.rmtree(child_root)
        child_root.parent.mkdir(parents=True, exist_ok=True)
        # apply_patches defaults to the shared snapshot-scope ignore, so
        # the parent-to-child copy stays code-only.
        apply_patches(
            source_root=parent_root,
            patches=list(patches),
            target_root=child_root,
        )
        return child_root

    def derive_scratch(
        self,
        epoch_id: str,
        parent_generation_id: str,
        patches: Sequence[Patch],
        scratch_root: Path,
    ) -> Path:
        """Apply ``patches`` to the parent snapshot into ``scratch_root``.

        Off-namespace: it reads the parent's canonical ``snapshot/`` tree but
        writes ONLY the caller-owned ``scratch_root`` — no ``generations/``
        directory is created, so :meth:`list_generations` and every walker
        that scans that directory never sees it. Byte-for-byte the same
        ``apply_patches`` the real :meth:`derive_generation` runs, minus the
        canonical child path.
        """
        from zicato.mutation.applier import apply_patches  # noqa: PLC0415

        parent_root = self.snapshot_path(epoch_id, parent_generation_id)
        if not parent_root.is_dir():
            raise FileNotFoundError(
                f"derive_scratch: parent generation {epoch_id}/"
                f"{parent_generation_id} has no source tree at {parent_root}"
            )
        scratch_root = Path(scratch_root)
        if scratch_root.exists():
            shutil.rmtree(scratch_root)
        scratch_root.parent.mkdir(parents=True, exist_ok=True)
        apply_patches(
            source_root=parent_root,
            patches=list(patches),
            target_root=scratch_root,
        )
        return scratch_root

    def checkout_ephemeral(
        self,
        epoch_id: str,
        generation_id: str,
        run_id: str,
    ) -> EphemeralCheckout:
        """Copy the canonical snapshot into a fresh per-run temp checkout.

        Exactly the historical per-run ephemeral-snapshot mechanism (a
        ``ztw-snap-{run_id}-*`` mkdtemp parent OUTSIDE the workspace, a
        basename-preserving artifact-filtered ``copytree``, a sibling
        ``run-scratch`` dir), moved behind the store seam — see
        :func:`copy_checkout_ephemeral`. Code snapshots are KB-sized, so
        a copy per run is cheap.
        """
        source = self.materialize_snapshot(epoch_id, generation_id)
        if not source.is_dir():
            raise FileNotFoundError(
                f"checkout_ephemeral: generation {epoch_id}/{generation_id} "
                f"has no source tree at {source}"
            )
        return copy_checkout_ephemeral(source, run_id)

    # ------------------------------------------------------------------
    # Read surface — the dashboard file-tree / file-browser API.
    # ------------------------------------------------------------------

    def list_tree(self, epoch_id: str, generation_id: str) -> list[TreeEntry]:
        """Walk the generation's ``snapshot/`` directory into :class:`TreeEntry` rows.

        Artifacts (:func:`zicato.epoch.snapshot_scope.is_artifact`) are
        skipped — a defensive second line behind the copy-time filter,
        so a tree that somehow acquired an ``output/`` post-copy still
        renders clean. Paths are ``/``-joined relative to the snapshot
        root and the result is sorted for a deterministic render.
        """
        root = self.materialize_snapshot(epoch_id, generation_id)
        if not root.is_dir():
            raise FileNotFoundError(
                f"list_tree: generation {epoch_id}/{generation_id} has no source tree at {root}"
            )
        entries: list[TreeEntry] = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            # Skip anything whose path contains an artifact component.
            if any(is_artifact(part) for part in rel.parts):
                continue
            is_dir = path.is_dir()
            entries.append(
                TreeEntry(
                    path="/".join(rel.parts),
                    is_dir=is_dir,
                    size=0 if is_dir else path.stat().st_size,
                )
            )
        return entries

    def read_file(self, epoch_id: str, generation_id: str, rel_path: str) -> bytes:
        """Return the bytes of ``rel_path`` inside the generation's ``snapshot/``.

        Rejects ``..`` traversal and absolute paths — the resolved file
        must sit under the snapshot root.
        """
        root = self.materialize_snapshot(epoch_id, generation_id)
        if not root.is_dir():
            raise FileNotFoundError(
                f"read_file: generation {epoch_id}/{generation_id} has no source tree at {root}"
            )
        target = (root / rel_path).resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise ValueError(f"read_file: rel_path {rel_path!r} escapes the generation source root")
        if target.is_dir():
            raise ValueError(f"read_file: {rel_path!r} is a directory, not a file")
        if not target.is_file():
            raise FileNotFoundError(
                f"read_file: {rel_path!r} not found in {epoch_id}/{generation_id}"
            )
        return target.read_bytes()

    def diff_generations(
        self,
        epoch_id: str,
        from_generation_id: str,
        to_generation_id: str,
    ) -> str:
        """Return a unified diff between two directory snapshots."""
        return _diff_materialized_trees(
            self.materialize_snapshot(epoch_id, from_generation_id),
            self.materialize_snapshot(epoch_id, to_generation_id),
        )

    def prune_generations(
        self,
        epoch_id: str,
        generation_ids: Sequence[str],
        *,
        dry_run: bool,
    ) -> int:
        """Remove selected directory snapshots while preserving sibling records."""
        snapshots = [
            self.snapshot_path(epoch_id, generation_id) for generation_id in generation_ids
        ]
        reclaimed = sum(source_tree_bytes(snapshot) for snapshot in snapshots)
        if not dry_run:
            for snapshot in snapshots:
                shutil.rmtree(snapshot, ignore_errors=True)
        return reclaimed


#: Workspace ``config.json`` key selecting the generation source-tree backend.
#: This is deliberately distinct from the generic record-store abstraction.
GENERATION_SOURCE_BACKEND_KEY = "generation_source_backend"

#: The git backend's name, in the config knob and in a resolution.
GIT_BACKEND = "git"

#: The directory-snapshot backend's name.
DIRECTORY_BACKEND = "directory"

#: Every backend name the knob accepts. A knob naming anything else is a
#: typo, and is refused rather than silently resolved.
KNOWN_GENERATION_SOURCE_BACKENDS = (GIT_BACKEND, DIRECTORY_BACKEND)

#: The backend written explicitly into new workspaces by ``zicato init``.
#: ``"git"``: the content-addressed worktree backend dedups blobs across a
#: lineage and its worktree checkout *is* the isolated per-run tree, so it
#: removes both the per-generation and per-run ``copytree`` the directory
#: backend pays. ``"directory"`` stays selectable for environments that do
#: not want a private git repo.
DEFAULT_GENERATION_SOURCE_BACKEND = GIT_BACKEND

#: Workspace subdirectory holding the git backend's private repository.
GIT_REPO_DIRNAME = "repo"

#: Workspace subdirectory holding reusable Git generation worktrees.
GIT_WORKTREES_DIRNAME = "repo-worktrees"

#: A generation's source tree under the directory backend, inside
#: ``epochs/{epoch_id}/generations/{generation_id}/``.
SNAPSHOT_DIRNAME = "snapshot"


def resolve_generation_store_backend(workspace_root: Path) -> str:
    """Return the explicitly configured generation-source backend.

    Initialized workspaces must carry :data:`GENERATION_SOURCE_BACKEND_KEY`.
    Missing, blank, malformed, and unknown values are errors.  The resolver
    never guesses from repositories, generation records, or snapshot paths;
    choosing a source backend is workspace configuration, not evidence
    discovery.
    """
    from zicato.workspace_loader import load_workspace_config  # noqa: PLC0415

    config = load_workspace_config(workspace_root)
    raw = config.get(GENERATION_SOURCE_BACKEND_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"workspace {workspace_root!s}: config.json must define "
            f"{GENERATION_SOURCE_BACKEND_KEY!r} as 'git' or 'directory'"
        )
    backend = raw.strip().lower()
    if backend not in KNOWN_GENERATION_SOURCE_BACKENDS:
        known = ", ".join(repr(name) for name in KNOWN_GENERATION_SOURCE_BACKENDS)
        raise ValueError(
            f"workspace {workspace_root!s}: unknown {GENERATION_SOURCE_BACKEND_KEY} "
            f"{backend!r}; known backends: {known}"
        )
    return backend


def default_generation_store(workspace_root: Path) -> GenerationStore:
    """Return the canonical :class:`GenerationStore` for a workspace.

    The backend comes exclusively from the workspace's explicit
    ``generation_source_backend`` config field:

    * ``"git"`` → :class:`~zicato.epoch.git_genstore.GitGenerationStore`,
      the content-addressed git backend (``docs/design/STORAGE.md`` §7).
    * ``"directory"`` → :class:`DirectoryGenerationStore`, the
      directory-snapshot backend, always available as the no-git fallback.

    This function is the single seam where that choice is made — the
    generation-store mirror of :func:`zicato.storage.factory.default_backend`.
    """
    backend = resolve_generation_store_backend(workspace_root)
    if backend == GIT_BACKEND:
        from zicato.epoch.git_genstore import GitGenerationStore  # noqa: PLC0415

        return GitGenerationStore(workspace_root)
    return DirectoryGenerationStore(workspace_root)


__all__ = [
    "GenerationStore",
    "DirectoryGenerationStore",
    "EphemeralCheckout",
    "TreeEntry",
    "EPHEMERAL_SCRATCH_DIRNAME",
    "EPHEMERAL_SNAPSHOT_PREFIX",
    "GENERATION_SOURCE_BACKEND_KEY",
    "DEFAULT_GENERATION_SOURCE_BACKEND",
    "DIRECTORY_BACKEND",
    "GIT_BACKEND",
    "GIT_REPO_DIRNAME",
    "GIT_WORKTREES_DIRNAME",
    "KNOWN_GENERATION_SOURCE_BACKENDS",
    "SNAPSHOT_DIRNAME",
    "copy_checkout_ephemeral",
    "default_generation_store",
    "discard_ephemeral_parent",
    "resolve_generation_store_backend",
    "source_tree_bytes",
]
