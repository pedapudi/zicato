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
  the directory backend pays. It is the default unless the
  ``storage_backend`` config knob selects otherwise (see
  :func:`default_generation_store`). The operator-facing git CLI
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
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from zicato.core.types import Patch
from zicato.core.workspace import generation_dir
from zicato.epoch.snapshot_scope import copytree_ignore, is_artifact


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
        gens_root = self._workspace_root / "epochs" / epoch_id / "generations"
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
#: ``"git"`` (the default) or ``"directory"``.
STORAGE_BACKEND_KEY = "storage_backend"

#: The backend selected when the knob is absent or empty. ``"git"``: the
#: content-addressed worktree backend dedups blobs across a lineage and
#: its worktree checkout *is* the isolated per-run tree, so it removes
#: both the per-generation and per-run ``copytree`` the directory backend
#: pays. ``"directory"`` stays selectable for environments that do not
#: want a private git repo.
DEFAULT_STORAGE_BACKEND = "git"


def _read_storage_backend_knob(workspace_root: Path) -> str:
    """Read the ``storage_backend`` knob from a workspace ``config.json``.

    Best-effort: a missing or malformed ``config.json`` (a workspace not
    yet ``zicato init``-ed, or one predating the knob) yields the
    :data:`DEFAULT_STORAGE_BACKEND` default. The generation store must
    never fail to construct just because the config is absent.
    """
    config_path = Path(workspace_root) / "config.json"
    if not config_path.is_file():
        return DEFAULT_STORAGE_BACKEND
    try:
        import json  # noqa: PLC0415

        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_STORAGE_BACKEND
    if not isinstance(loaded, dict):
        return DEFAULT_STORAGE_BACKEND
    value = loaded.get(STORAGE_BACKEND_KEY, DEFAULT_STORAGE_BACKEND)
    return str(value).strip().lower() or DEFAULT_STORAGE_BACKEND


def default_generation_store(workspace_root: Path) -> GenerationStore:
    """Return the canonical :class:`GenerationStore` for a workspace.

    The backend is selected off the workspace ``config.json``'s
    :data:`STORAGE_BACKEND_KEY` knob, defaulting to
    :data:`DEFAULT_STORAGE_BACKEND` (``"git"``):

    * ``"git"`` (the default, including for a missing/blank knob) →
      :class:`~zicato.epoch.git_genstore.GitGenerationStore`, the
      content-addressed git backend (``docs/design/STORAGE.md`` §7).
    * ``"directory"`` → :class:`DirectoryGenerationStore`, the
      directory-snapshot backend, always available as the no-git fallback.

    This function is the single seam where that choice is made — the
    generation-store mirror of :func:`zicato.storage.factory.default_backend`.
    """
    backend = _read_storage_backend_knob(workspace_root)
    if backend == "git":
        from zicato.epoch.git_genstore import GitGenerationStore  # noqa: PLC0415

        return GitGenerationStore(workspace_root)
    return DirectoryGenerationStore(workspace_root)


__all__ = [
    "GenerationStore",
    "DirectoryGenerationStore",
    "TreeEntry",
    "PatchRecord",
    "STORAGE_BACKEND_KEY",
    "DEFAULT_STORAGE_BACKEND",
    "default_generation_store",
]
