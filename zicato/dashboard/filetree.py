"""File-tree and file-browser views for the dashboard.

This module backs the dashboard's **Files** view: a tree of every
generation's source, every generation's applied patch set, with a
file-content browser. It reads through the
:class:`~zicato.epoch.genstore.GenerationStore` seam — so it works
**unchanged** for both storage backends. For a directory-snapshot
workspace it browses the ``generations/vN/snapshot/`` trees; for a
git-backed workspace it browses git commits / trees. The dashboard
endpoint layer never sees that difference.

Why through the store, not raw paths
-------------------------------------
``docs/design/STORAGE.md`` §4-§5 makes the generation source tree a
pluggable store. Hard-coding ``epochs/{id}/generations/`` here would
silently break the moment a workspace flips to the git backend, where
generations live as commits, not directories. Routing every read
through :func:`zicato.epoch.genstore.default_generation_store` keeps the
dashboard backend-agnostic by construction.

Endpoints (wired in :mod:`zicato.dashboard.server`)
---------------------------------------------------
* ``GET /api/files`` → :func:`build_file_index` — every epoch and its
  generations, each generation's tree node count and patch count.
* ``GET /api/files/{epoch}/{generation}/tree`` →
  :func:`build_generation_tree` — the full source tree.
* ``GET /api/files/{epoch}/{generation}/content?path=...`` →
  :func:`read_generation_file` — one file's content.
* ``GET /api/files/{epoch}/{generation}/patches`` →
  :func:`build_generation_patches` — the applied patch set.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from zicato.dashboard.state_reader import WorkspacePaths
from zicato.epoch.genstore import GenerationStore, default_generation_store

#: Files larger than this are not inlined into the content response —
#: the browser gets a truncation marker instead. The dashboard is a
#: source viewer, not a binary inspector; generation source files are
#: KB-sized, so this only ever trips on an accidental large artifact.
_MAX_INLINE_BYTES = 512 * 1024

#: Byte-count threshold past which a file is reported as binary on a
#: failed UTF-8 decode rather than rendered.
_DECODE_ERRORS = "replace"


def _store(paths: WorkspacePaths) -> GenerationStore:
    """Build the workspace's generation store (directory or git backend).

    ``WorkspacePaths.root`` is the ``.zicato/`` directory — exactly the
    ``workspace_root`` :func:`default_generation_store` expects, so the
    config-knob backend selection happens for free.
    """
    return default_generation_store(paths.root)


def _list_epoch_ids(paths: WorkspacePaths) -> list[str]:
    """Return every epoch id under the workspace, sorted.

    Walks ``epochs/`` directly — epoch directories exist for both
    storage backends (the git backend only changes where *generation
    source trees* live, not the per-epoch record directories).
    """
    if not paths.epochs.is_dir():
        return []
    return sorted(d.name for d in paths.epochs.iterdir() if d.is_dir())


def build_file_index(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the top-level Files-view index: epochs → generations.

    Each generation entry carries enough for the tree's collapsed view —
    a file count and a patch count — without shipping the whole tree.
    The dashboard lazy-loads a generation's full tree on expansion via
    :func:`build_generation_tree`.
    """
    store = _store(paths)
    epochs: list[dict[str, Any]] = []
    for epoch_id in _list_epoch_ids(paths):
        generations: list[dict[str, Any]] = []
        for generation_id in store.list_generations(epoch_id):
            try:
                tree = store.list_tree(epoch_id, generation_id)
                file_count = sum(1 for e in tree if not e.is_dir)
            except (FileNotFoundError, OSError, ValueError):
                file_count = 0
            try:
                patch_count = len(store.list_patches(epoch_id, generation_id).patches)
            except (FileNotFoundError, OSError, ValueError):
                patch_count = 0
            generations.append(
                {
                    "generation_id": generation_id,
                    "file_count": file_count,
                    "patch_count": patch_count,
                }
            )
        epochs.append({"epoch_id": epoch_id, "generations": generations})
    return {"epochs": epochs}


def build_generation_tree(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Return one generation's full source tree as a flat entry list.

    The shape is a flat list of ``{path, is_dir, size}`` — the frontend
    builds the nested tree from the ``/``-separated paths. A flat list
    keeps the wire format trivial and the same for both backends.

    On a missing generation the response carries ``"error"`` and an
    empty ``entries`` list rather than raising, so the dashboard
    degrades to an empty tree instead of a 500.
    """
    store = _store(paths)
    try:
        entries = store.list_tree(epoch_id, generation_id)
    except FileNotFoundError:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "entries": [],
            "error": f"no source tree for {epoch_id}/{generation_id}",
        }
    except (OSError, ValueError) as exc:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "entries": [],
            "error": str(exc),
        }
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entries": [asdict(e) for e in entries],
    }


def read_generation_file(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    rel_path: str,
) -> dict[str, Any]:
    """Return one file's content from a generation's source tree.

    The response carries ``{path, content, size, truncated, binary}``.
    A file over :data:`_MAX_INLINE_BYTES` is truncated; a file that does
    not decode as UTF-8 is reported ``binary=True`` with a placeholder
    body. Path traversal and missing files come back as an ``"error"``
    field — never an exception out of this function.
    """
    store = _store(paths)
    try:
        raw = store.read_file(epoch_id, generation_id, rel_path)
    except FileNotFoundError:
        return {"path": rel_path, "error": "file not found"}
    except ValueError as exc:
        return {"path": rel_path, "error": str(exc)}
    except OSError as exc:
        return {"path": rel_path, "error": f"read failed: {exc}"}

    size = len(raw)
    truncated = size > _MAX_INLINE_BYTES
    body = raw[:_MAX_INLINE_BYTES] if truncated else raw
    try:
        text = body.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = body.decode("utf-8", _DECODE_ERRORS)
        binary = True
    return {
        "path": rel_path,
        "content": text,
        "size": size,
        "truncated": truncated,
        "binary": binary,
    }


def _patch_to_dict(patch: Any) -> dict[str, Any]:
    """Render a :class:`~zicato.core.types.Patch` for the wire.

    ``Patch`` is a frozen dataclass; :func:`dataclasses.asdict` gives a
    JSON-serialisable mapping with every field.
    """
    return asdict(patch)


def build_generation_patches(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Return the patch set that derived a generation from its parent.

    A seed (``v0``) generation yields an empty ``patches`` list. The
    response is the same shape for both storage backends — the directory
    backend reads the per-patch JSON files, the git backend reads the
    commit metadata block, but :meth:`GenerationStore.list_patches`
    normalises both.
    """
    store = _store(paths)
    try:
        record = store.list_patches(epoch_id, generation_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "patches": [],
            "error": str(exc),
        }
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "patches": [_patch_to_dict(p) for p in record.patches],
    }


__all__ = [
    "build_file_index",
    "build_generation_tree",
    "read_generation_file",
    "build_generation_patches",
]
