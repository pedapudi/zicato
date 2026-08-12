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

The path-returning shape
------------------------
:meth:`GenerationStore.snapshot_root` returns a real on-disk
:class:`~pathlib.Path`. That is not an accident of the directory
backend — it is the contract. The orchestrator and the subprocess
tournament workers genuinely need a path: a worker ``chdir``\\ s into a
generation's source tree and loads the inner-harness adapter from it. A
future git backend satisfies this by checking out a ``git worktree``
and returning *its* path; the protocol is written so that backend is a
drop-in second implementation. A record-store-shaped ``read() ->
dict[str, bytes]`` surface could not serve the worker, which is the
other half of why §4 keeps this seam separate.

Backends
--------
* :class:`~zicato.epoch.git_genstore.GitGenerationStore` — the **default**
  backend (``docs/design/STORAGE.md`` §7). A generation is a commit on an
  epoch branch, materialised for a run as a content-addressed
  ``git worktree``. Because the object store dedups unchanged blobs across
  a lineage, and the worktree checkout *is* the isolated per-run tree, the
  git backend removes both the per-generation and the per-run ``copytree``
  the directory backend pays. The operator-facing git CLI
  (``zicato repo`` / ``log`` / ``diff`` / ``show`` / ``bisect`` /
  ``blame``, ``workspace migrate-to-git``) is still on the roadmap.
* :class:`DirectoryGenerationStore` — the directory-snapshot backend,
  selected by ``storage_backend: "directory"``. A generation is a
  ``generations/{id}/snapshot/`` directory; deriving a child is a
  ``copytree`` of the parent plus an all-or-nothing patch apply. This IS
  the pre-seam directory-snapshot mechanism, byte-for-byte. It remains a
  fully supported, config-selectable backend for environments where a
  private git repo is unwanted; the git default simply removes the copy
  cost for the common case.

Which backend reads a given workspace is
:func:`resolve_generation_store_backend`'s decision: the explicit
``storage_backend`` knob first, then the workspace's own on-disk contents,
and only then the default. A workspace's backend is a durable property of
what it already holds, so the default settles it only for a workspace that
holds no generations yet.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

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
class PatchRecord:
    """One generation's applied patch set, for the dashboard patch browser.

    A backend-neutral view of the patches that derived a generation from
    its parent. The directory backend reads these from the per-patch
    JSON files under ``experiment.json``; the git backend reads them from
    the generation commit's metadata block. The dashboard renders the
    list identically either way.

    Attributes
    ----------
    generation_id:
        The generation the patches derived.
    patches:
        The applied :class:`~zicato.core.types.Patch` objects, in apply
        order. Empty for a seed (``v0``) generation.
    """

    generation_id: str
    patches: tuple[Patch, ...]


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
        :meth:`GenerationStore.snapshot_root`'s basename so any path the
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
    except OSError as exc:  # ignore_errors already swallows most
        log.debug("ephemeral checkout cleanup skipped for %s: %s", parent, exc)


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

    def snapshot_root(self, epoch_id: str, generation_id: str) -> Path:
        """Return the on-disk source-tree path for one generation.

        This is a pure coordinate→path computation — it does NOT assert
        the generation exists (use :meth:`has_generation` for that) and
        does NOT materialise anything. The returned path is what a
        tournament worker mounts as the inner harness's source root.
        """
        ...

    def has_generation(self, epoch_id: str, generation_id: str) -> bool:
        """Return ``True`` iff a materialised source tree exists for the coordinate."""
        ...

    def list_generations(self, epoch_id: str) -> list[str]:
        """Return the ids of every materialised generation under ``epoch_id``, sorted."""
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
        file (rare) is copied directly. Returns the generation's
        :meth:`snapshot_root`.

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

        Returns the child generation's :meth:`snapshot_root`.

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
        tree (:meth:`snapshot_root`): the canonical tree is what
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
          :meth:`snapshot_root`'s basename (``__file__``-derived paths
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

    def list_patches(self, epoch_id: str, generation_id: str) -> PatchRecord:
        """Return the patch set that derived ``generation_id`` from its parent.

        For a seed (``v0``) generation the returned
        :class:`PatchRecord` carries an empty ``patches`` tuple. For a
        derived generation it carries the applied patches in apply
        order. The patches are read from the canonical record (the
        per-patch JSON files for the directory backend, the commit
        metadata for the git backend) — this method does not re-derive
        anything.

        Returns an empty :class:`PatchRecord` rather than raising when
        the generation exists but has no recorded patch set.
        """
        ...


class DirectoryGenerationStore:
    """The directory-snapshot :class:`GenerationStore` — shipped default.

    A generation's source tree is the ``generations/{id}/snapshot/``
    directory under its epoch. Deriving a child is a full ``copytree``
    of the parent's snapshot followed by an all-or-nothing patch apply
    (via :func:`zicato.mutation.applier.apply_patches`, whose
    deterministic pre-validation already makes the apply atomic).

    This backend IS the pre-seam mechanism the orchestrator used
    directly — the same paths, the same ``copytree``, the same applier.
    The seam formalises it so a git backend can later be substituted at
    exactly one construction site; it does not change a single on-disk
    byte.

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

    def snapshot_root(self, epoch_id: str, generation_id: str) -> Path:
        """Return ``generations/{generation_id}/snapshot/`` for the coordinate.

        Pure path math — no I/O, no assertion that the generation
        exists. Mirrors the orchestrator's pre-seam ``_snapshot_root``
        helper exactly.
        """
        return generation_dir(self._workspace_root, epoch_id, generation_id) / "snapshot"

    def has_generation(self, epoch_id: str, generation_id: str) -> bool:
        """Return ``True`` iff the generation's ``snapshot/`` directory exists."""
        return self.snapshot_root(epoch_id, generation_id).is_dir()

    def list_generations(self, epoch_id: str) -> list[str]:
        """Return the ids of every generation directory under ``epoch_id``, sorted.

        A generation counts as present when its directory exists under
        ``epochs/{epoch_id}/generations/`` — the same liveness rule the
        orchestrator's ``_next_generation_id`` and the index's directory
        walk use. Sorted lexicographically for a deterministic order.
        """
        gens_root = WorkspaceLayout.from_root(self._workspace_root).generations_dir(epoch_id)
        if not gens_root.is_dir():
            return []
        return sorted(child.name for child in gens_root.iterdir() if child.is_dir())

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
        snapshot_root = self.snapshot_root(epoch_id, generation_id)
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

        Returns the child's :meth:`snapshot_root`.
        """
        from zicato.mutation.applier import apply_patches  # noqa: PLC0415

        parent_root = self.snapshot_root(epoch_id, parent_generation_id)
        if not parent_root.is_dir():
            raise FileNotFoundError(
                f"derive_generation: parent generation {epoch_id}/"
                f"{parent_generation_id} has no source tree at {parent_root}"
            )
        child_root = self.snapshot_root(epoch_id, child_generation_id)
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

        parent_root = self.snapshot_root(epoch_id, parent_generation_id)
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
        source = self.snapshot_root(epoch_id, generation_id)
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
        root = self.snapshot_root(epoch_id, generation_id)
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
        root = self.snapshot_root(epoch_id, generation_id)
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

    def list_patches(self, epoch_id: str, generation_id: str) -> PatchRecord:
        """Read the generation's applied patch set from ``experiment.json``.

        Delegates to :func:`zicato.epoch.journal.read_experiment`, which
        resolves both the per-patch-file and legacy-inline on-disk
        shapes. A seed generation (no ``experiment.json``) yields an
        empty :class:`PatchRecord`.
        """
        from zicato.epoch.journal import read_experiment  # noqa: PLC0415

        try:
            experiment = read_experiment(self._workspace_root, epoch_id, generation_id)
        except FileNotFoundError:
            return PatchRecord(generation_id=generation_id, patches=())
        return PatchRecord(
            generation_id=generation_id,
            patches=tuple(experiment.patches),
        )


#: Workspace ``config.json`` key selecting the generation-store backend.
#: ``"git"`` or ``"directory"``.
STORAGE_BACKEND_KEY = "storage_backend"

#: The git backend's name, in the config knob and in a resolution.
GIT_BACKEND = "git"

#: The directory-snapshot backend's name.
DIRECTORY_BACKEND = "directory"

#: Every backend name the knob accepts. A knob naming anything else is a
#: typo, and is refused rather than silently resolved.
KNOWN_STORAGE_BACKENDS = (GIT_BACKEND, DIRECTORY_BACKEND)

#: The backend a workspace gets when neither the knob nor the workspace's
#: own contents decide — i.e. a workspace that holds no generations yet.
#: ``"git"``: the content-addressed worktree backend dedups blobs across a
#: lineage and its worktree checkout *is* the isolated per-run tree, so it
#: removes both the per-generation and per-run ``copytree`` the directory
#: backend pays. ``"directory"`` stays selectable for environments that do
#: not want a private git repo.
DEFAULT_STORAGE_BACKEND = GIT_BACKEND

#: Workspace subdirectory holding the git backend's private repository.
#: The presence of its ``.git`` is the on-disk evidence of a git-backed
#: workspace; :class:`~zicato.epoch.git_genstore.GitGenerationStore` reads
#: this as its ``REPO_DIRNAME``.
GIT_REPO_DIRNAME = "repo"

#: A generation's source tree under the directory backend, inside
#: ``epochs/{epoch_id}/generations/{generation_id}/``.
SNAPSHOT_DIRNAME = "snapshot"


@dataclass(frozen=True)
class BackendResolution:
    """Which generation-store backend a workspace uses, and on what grounds.

    ``source`` records *why* — ``"config"`` (the explicit knob),
    ``"evidence"`` (the workspace's own contents), or ``"default"``
    (nothing on disk to go on). ``mismatch``, when set, is an
    operator-facing sentence naming a disagreement between the resolved
    backend and what is on disk; the resolution still stands.
    """

    backend: str
    source: str
    mismatch: str | None = None


def _read_storage_backend_knob(workspace_root: Path) -> str | None:
    """Return the ``storage_backend`` knob, or ``None`` when it is absent.

    Best-effort on the *file*: a missing or malformed ``config.json`` (a
    workspace not yet ``zicato init``-ed, or one predating the knob) reads
    as no knob, because a config the loader cannot parse must not decide
    the backend. A knob that *is* present and names an unknown backend is
    a different matter — see :func:`resolve_generation_store_backend`.
    """
    config_path = Path(workspace_root) / "config.json"
    if not config_path.is_file():
        return None
    try:
        import json  # noqa: PLC0415

        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    value = str(loaded.get(STORAGE_BACKEND_KEY, "")).strip().lower()
    return value or None


def _has_git_store(workspace_root: Path) -> bool:
    """Return True iff the workspace holds the git backend's repository."""
    return (Path(workspace_root) / GIT_REPO_DIRNAME / ".git").exists()


def _generation_evidence(workspace_root: Path) -> tuple[bool, bool]:
    """Return ``(any generation record, any directory snapshot)``.

    Both backends write generation *records* (``experiment.json``,
    patches, run logs); only the source tree differs, so a record alone
    says "this workspace has produced something" — which is what makes
    the absence of a git repository informative. A ``snapshot/`` beside
    a record is the directory backend's source tree, and names the
    backend outright.
    """
    epochs_root = Path(workspace_root) / "epochs"
    any_record = False
    for epoch in _children(epochs_root):
        for generation in _children(epoch / "generations"):
            any_record = True
            if (generation / SNAPSHOT_DIRNAME).is_dir():
                return True, True
    return any_record, False


def _children(path: Path) -> Iterable[Path]:
    """Iterate ``path``'s subdirectories, yielding nothing when it is not one."""
    if not path.is_dir():
        return ()
    return (child for child in path.iterdir() if child.is_dir())


def resolve_generation_store_backend(workspace_root: Path) -> BackendResolution:
    """Decide which generation-store backend reads a workspace.

    The order is **explicit config > on-disk evidence > default**:

    1. The ``storage_backend`` knob, when present, is the operator's
       stated intent and wins. An unknown name raises :class:`ValueError`
       rather than resolving to whichever backend the fallthrough happens
       to reach.
    2. Otherwise the workspace's own contents decide. A ``repo/.git``
       means git wrote it. Generation records with *no* repository mean
       the directory backend wrote it: a git-backed workspace that has
       produced any generation necessarily has a repository, and snapshot
       GC (:mod:`zicato.epoch.gc`) prunes ``snapshot/`` directories but
       never the repository — so "records, no repo" stays directory
       evidence even after every snapshot has been pruned.
    3. A workspace holding no generations has nothing to go on, so
       :data:`DEFAULT_STORAGE_BACKEND` decides.

    Evidence outranks the default because the default is a *global*
    constant: reading a knobless workspace through it re-interprets every
    workspace ever created whenever it changes, which is exactly how a
    directory-snapshot workspace came to be read as an empty git one.
    Evidence is a property of the workspace and does not move under it.

    A resolution that contradicts the disk — an explicit knob naming a
    backend whose store is absent while the other's is present, or two
    stores present at once — still stands, but carries a ``mismatch``
    sentence naming it. :func:`default_generation_store` logs that at
    warning level so the disagreement is never silent.
    """
    knob = _read_storage_backend_knob(workspace_root)
    has_repo = _has_git_store(workspace_root)
    has_records, has_snapshots = _generation_evidence(workspace_root)

    if knob is not None:
        if knob not in KNOWN_STORAGE_BACKENDS:
            known = ", ".join(repr(name) for name in KNOWN_STORAGE_BACKENDS)
            raise ValueError(
                f"workspace {workspace_root!s}: unknown {STORAGE_BACKEND_KEY} {knob!r}; "
                f"known backends: {known}"
            )
        chosen_present, other_present = (
            (has_repo, has_snapshots) if knob == GIT_BACKEND else (has_snapshots, has_repo)
        )
        mismatch = None
        if other_present and not chosen_present:
            other = DIRECTORY_BACKEND if knob == GIT_BACKEND else GIT_BACKEND
            mismatch = (
                f"{STORAGE_BACKEND_KEY} is {knob!r} but this workspace holds a "
                f"{other} generation store and no {knob} one; generations written by "
                f"the {other} backend will not be listed"
            )
        return BackendResolution(knob, "config", mismatch)

    if has_repo:
        mismatch = None
        if has_snapshots:
            mismatch = (
                "this workspace holds both a git generation store and directory "
                f"snapshots; reading it as {GIT_BACKEND!r} — set {STORAGE_BACKEND_KEY} "
                "to pin the intended one"
            )
        return BackendResolution(GIT_BACKEND, "evidence", mismatch)

    if has_records:
        return BackendResolution(DIRECTORY_BACKEND, "evidence", None)

    return BackendResolution(DEFAULT_STORAGE_BACKEND, "default", None)


#: Mismatches already logged, so a resolution on a hot path (every
#: ``snapshot_root`` call goes through one) warns once per workspace
#: rather than once per generation read.
_reported_mismatches: set[tuple[str, str]] = set()


def default_generation_store(workspace_root: Path) -> GenerationStore:
    """Return the canonical :class:`GenerationStore` for a workspace.

    The backend comes from :func:`resolve_generation_store_backend` —
    explicit ``storage_backend`` knob, else the workspace's own contents,
    else :data:`DEFAULT_STORAGE_BACKEND`:

    * ``"git"`` → :class:`~zicato.epoch.git_genstore.GitGenerationStore`,
      the content-addressed git backend (``docs/design/STORAGE.md`` §7).
    * ``"directory"`` → :class:`DirectoryGenerationStore`, the
      directory-snapshot backend, always available as the no-git fallback.

    This function is the single seam where that choice is made — the
    generation-store mirror of :func:`zicato.storage.factory.default_backend`.
    """
    resolution = resolve_generation_store_backend(workspace_root)
    if resolution.mismatch is not None:
        seen = (str(workspace_root), resolution.mismatch)
        if seen not in _reported_mismatches:
            _reported_mismatches.add(seen)
            log.warning("generation store: %s", resolution.mismatch)
    if resolution.backend == GIT_BACKEND:
        from zicato.epoch.git_genstore import GitGenerationStore  # noqa: PLC0415

        return GitGenerationStore(workspace_root)
    return DirectoryGenerationStore(workspace_root)


__all__ = [
    "GenerationStore",
    "DirectoryGenerationStore",
    "BackendResolution",
    "EphemeralCheckout",
    "TreeEntry",
    "PatchRecord",
    "EPHEMERAL_SCRATCH_DIRNAME",
    "EPHEMERAL_SNAPSHOT_PREFIX",
    "STORAGE_BACKEND_KEY",
    "DEFAULT_STORAGE_BACKEND",
    "DIRECTORY_BACKEND",
    "GIT_BACKEND",
    "GIT_REPO_DIRNAME",
    "KNOWN_STORAGE_BACKENDS",
    "SNAPSHOT_DIRNAME",
    "copy_checkout_ephemeral",
    "default_generation_store",
    "discard_ephemeral_parent",
    "resolve_generation_store_backend",
]
