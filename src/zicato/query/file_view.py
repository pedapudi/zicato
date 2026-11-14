"""Readers of a generation's source tree, file content, patch set and diff.

These readers serve the console's diff and mutation views: one file of a
generation's source, a generation's applied patch set, and the files it
changed. They read through the
:class:`~zicato.epoch.genstore.GenerationStore` seam — so it works
**unchanged** for both storage backends. For a directory-snapshot
workspace it browses the ``generations/vN/snapshot/`` trees; for a
git-backed workspace it browses git commits / trees. The dashboard
endpoint layer never sees that difference.

Why through the store rather than raw paths
-------------------------------------
``docs/design/STORAGE.md`` §4-§5 makes the generation source tree a
pluggable store. Hard-coding ``epochs/{id}/generations/`` here would
silently break the moment a workspace flips to the git backend, where
generations live as commits rather than directories. Routing every read
through :func:`zicato.epoch.genstore.default_generation_store` keeps the
dashboard backend-agnostic by construction.

Routes (rows of :data:`zicato.dashboard.endpoints.READ_ENDPOINTS`)
------------------------------------------------------------------
* ``GET /api/files/{epoch}/{generation}/content?path=...`` →
  :func:`read_generation_file` — one file's content.
* ``GET /api/files/{epoch}/{generation}/patches`` →
  :func:`build_generation_patches` — the applied patch set.
* ``GET /api/files/{epoch}/{generation}/diff`` →
  :func:`build_generation_diff` — the files a generation changed
  relative to its parent (or the ``v0`` baseline), each with the old
  and new content for a side-by-side diff.

When a tree is gone
-------------------
Snapshot GC prunes generation source trees and keeps every record
(:mod:`zicato.epoch.gc`), so these views must answer for a generation
they cannot walk. Whole-tree browsing is the one thing records cannot
rebuild — the content view says exactly that and points
at what survives — while the DIFF's real subject, the spans the
generation's patches touched, is reconstructible and is reconstructed
(issue #194 §6). Every response says which it is: ``provenance`` plus
the caption to render.
"""

from __future__ import annotations

from typing import Any

from zicato.epoch._storage import RecordError
from zicato.epoch.genstore import GenerationStore
from zicato.epoch.journal import patch_body, read_generation_patches
from zicato.query.mutation_view import (
    FROM_RECORDS,
    FROM_SNAPSHOT,
    SPANS_CAPTION,
    SPANS_UNREACHABLE_CAPTION,
    _resolve_store,
    reconstructed_spans,
    recorded_generation_ids,
)
from zicato.query.paths import WorkspacePaths
from zicato.storage import workspace_backend
from zicato.workspace import generation_round_number

#: Files larger than this are not inlined into the content response —
#: the browser gets a truncation marker instead. The dashboard is a
#: source viewer rather than a binary inspector; generation source files are
#: KB-sized, so this only ever trips on an accidental large artifact.
_MAX_INLINE_BYTES = 512 * 1024

#: Byte-count threshold past which a file is reported as binary on a
#: failed UTF-8 decode rather than rendered.
_DECODE_ERRORS = "replace"


#: What a view says when a RECORDED generation's tree is gone. Whole-tree
#: browsing is genuinely unrecoverable — unlike the patch-touched spans,
#: which the records reconstruct (:func:`build_generation_diff`).
_PRUNED_TREE_ERROR = (
    "no source tree for {epoch_id}/{generation_id} — pruned by snapshot GC, or the "
    "workspace was archived without its snapshots. The records survive: the patch set "
    "at /api/files/{epoch_id}/{generation_id}/patches, the patched spans at "
    "/api/files/{epoch_id}/{generation_id}/diff, the site surface at "
    "/api/mutations/{epoch_id}."
)

#: …and when there is no such generation at all. Kept apart because a
#: typo'd coordinate and a collected tree call for opposite next moves,
#: and pointing an operator at records that were never written would be
#: its own small dishonesty.
_UNKNOWN_GENERATION_ERROR = "no generation {epoch_id}/{generation_id} in this workspace"

#: …and when NO generation's tree is reachable because the workspace has no
#: generation store. Kept apart from the pruned sentence because the trees
#: may be intact on disk: saying "pruned by snapshot GC" there would be an
#: explanation the operator can see is false.
_UNREACHABLE_TREE_ERROR = (
    "no source tree for {epoch_id}/{generation_id} — this workspace has no generation "
    "source store, so no tree is reachable: {reason}. The records survive: the patch set "
    "at /api/files/{epoch_id}/{generation_id}/patches, the patched spans at "
    "/api/files/{epoch_id}/{generation_id}/diff, the site surface at "
    "/api/mutations/{epoch_id}."
)


def _missing_tree_error(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, store_error: str = ""
) -> str:
    """Name what actually happened to a tree the store cannot walk.

    Three readings, and they call for opposite next moves:

    * no store at all — the workspace's configured source backend is
      missing or contradicts the disk, so NO tree is reachable and the
      trees may well still be sitting there. The configuration reason is
      the answer; blaming retention would be an invented explanation.
    * a record directory, and a store that simply has no tree under the
      coordinate — the generation ran and its tree was collected.
    * neither — the coordinate names nothing.
    """
    if store_error:
        return _UNREACHABLE_TREE_ERROR.format(
            epoch_id=epoch_id, generation_id=generation_id, reason=store_error
        )
    template = (
        _PRUNED_TREE_ERROR
        if generation_id in recorded_generation_ids(paths, epoch_id)
        else _UNKNOWN_GENERATION_ERROR
    )
    return template.format(epoch_id=epoch_id, generation_id=generation_id)


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
    store, store_error = _resolve_store(paths)
    if store is None:
        return {"path": rel_path, "error": store_error}
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


def build_generation_patches(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Return the patch set that derived a generation from its parent.

    A seed (``v0``) generation yields an empty ``patches`` list. The
    response is independent of the generation-source backend because patch
    records are read through ``StorageBackend``.
    """
    try:
        backend = workspace_backend(paths.root, start=True)
        record = read_generation_patches(backend, epoch_id, generation_id)
    except (FileNotFoundError, OSError, RecordError, ValueError) as exc:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "patches": [],
            "error": str(exc),
        }
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "patches": [patch_body(p) for p in record.patches],
    }


def _recorded_parent(paths: WorkspacePaths, epoch_id: str, generation_id: str) -> str | None:
    """The lineage edge from the generation's OWN record — no tree needed.

    ``experiment.json`` outlives the tree it describes, so this answers
    "what was this derived from" for a pruned generation too. Separate
    from :func:`_resolve_parent_generation` because the two questions
    differ once trees go missing: this one is the truth, that one is the
    best available tree to diff against.
    """
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    try:
        experiment = read_experiment(paths.root, epoch_id, generation_id)
    except (FileNotFoundError, OSError, RecordError, ValueError):
        return None
    recorded = (experiment.parent_generation_id or "").strip()
    if not recorded or recorded == generation_id:
        return None
    return recorded


def _resolve_parent_generation(
    paths: WorkspacePaths,
    store: GenerationStore,
    epoch_id: str,
    generation_id: str,
) -> str | None:
    """Resolve the generation a diff should be taken against.

    The diff of "what a generation changed" is taken relative to the
    generation it was derived from. Resolution order:

    1. The ``parent_generation_id`` recorded in the generation's
       ``experiment.json`` (the authoritative lineage edge).
    2. For a ``v<N>`` id with ``N > 0``, the numerically-preceding
       generation ``v<N-1>`` when that generation has a source tree.
    3. The ``v0`` baseline, when it exists and is not the generation
       itself.

    Returns ``None`` when no parent can be resolved — a seed generation,
    or a workspace with only the one generation. The caller then treats
    every file as added.
    """
    recorded = _recorded_parent(paths, epoch_id, generation_id)
    if recorded is not None and store.has_generation(epoch_id, recorded):
        return recorded

    index = generation_round_number(generation_id)
    if index is not None and index > 0:
        candidate = f"v{index - 1}"
        if store.has_generation(epoch_id, candidate):
            return candidate

    if generation_id != "v0" and store.has_generation(epoch_id, "v0"):
        return "v0"
    return None


def _read_text(
    store: GenerationStore, epoch_id: str, generation_id: str, rel_path: str
) -> tuple[str, bool]:
    """Read a tree file as text. Returns ``(text, binary)``.

    A file that does not decode as UTF-8 comes back ``binary=True`` with
    a best-effort replacement-decoded body; a missing file comes back as
    an empty string (so a side-only file diffs cleanly against "").
    """
    try:
        raw = store.read_file(epoch_id, generation_id, rel_path)
    except (FileNotFoundError, OSError, ValueError):
        return "", False
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", _DECODE_ERRORS), True


def _file_paths(store: GenerationStore, epoch_id: str, generation_id: str) -> set[str]:
    """Return the set of (non-directory) file paths in a generation tree."""
    try:
        return {e.path for e in store.list_tree(epoch_id, generation_id) if not e.is_dir}
    except (FileNotFoundError, OSError, ValueError):
        return set()


def build_generation_diff(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Return the files a generation changed relative to its parent.

    The diff is taken against the generation's parent (see
    :func:`_resolve_parent_generation`); a seed generation with no
    parent diffs against the empty tree, so every file reads as added.

    The response shape::

        {
          "epoch_id", "generation_id",
          "parent_generation_id": str | null,
          "files": [
            { "path", "status": "added"|"modified"|"removed",
              "old_content", "new_content",
              "old_binary", "new_binary" },
            ...
          ],
          "error"?: str
        }

    ``files`` lists only files that differ, sorted by path. Each carries
    the old and new content so the dashboard can render a side-by-side
    split diff without a second round trip. The store seam keeps this
    backend-neutral — it works for the directory-snapshot and the
    git-backed workspace identically.

    Once a tree is gone the whole-file diff is not recoverable, but the
    spans the generation's patches touched are: the response then carries
    :func:`zicato.query.mutation_view.reconstructed_spans` entries,
    ``provenance: "records"`` and the caption the view renders. Two
    conditions take that path — the generation's own tree is missing, or
    the tree of the parent it was RECORDED as derived from is. The second
    matters because falling back to whichever older tree still exists
    would silently answer a different question ("what changed since
    ``v0``") under the same heading. A workspace with no generation store
    at all takes it too, for the same reason and with the configuration
    named rather than retention blamed.
    """
    store, store_error = _resolve_store(paths)
    recorded_parent = _recorded_parent(paths, epoch_id, generation_id)
    if (
        store is None
        or not store.has_generation(epoch_id, generation_id)
        or (recorded_parent is not None and not store.has_generation(epoch_id, recorded_parent))
    ):
        spans = reconstructed_spans(paths, epoch_id, generation_id)
        payload: dict[str, Any] = {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "parent_generation_id": recorded_parent,
            "files": spans,
            "provenance": FROM_RECORDS,
            "provenance_note": (SPANS_UNREACHABLE_CAPTION if store_error else SPANS_CAPTION),
        }
        if not spans:
            payload["error"] = _missing_tree_error(paths, epoch_id, generation_id, store_error)
        return payload

    parent_id = _resolve_parent_generation(paths, store, epoch_id, generation_id)
    new_paths = _file_paths(store, epoch_id, generation_id)
    old_paths = _file_paths(store, epoch_id, parent_id) if parent_id is not None else set()

    files: list[dict[str, Any]] = []
    for rel_path in sorted(new_paths | old_paths):
        in_new = rel_path in new_paths
        in_old = rel_path in old_paths
        new_text, new_binary = (
            _read_text(store, epoch_id, generation_id, rel_path) if in_new else ("", False)
        )
        old_text, old_binary = (
            _read_text(store, epoch_id, parent_id, rel_path)
            if in_old and parent_id is not None
            else ("", False)
        )
        if in_new and not in_old:
            status = "added"
        elif in_old and not in_new:
            status = "removed"
        elif old_text == new_text:
            # Present on both sides with identical content — unchanged.
            continue
        else:
            status = "modified"
        files.append(
            {
                "path": rel_path,
                "status": status,
                "old_content": old_text,
                "new_content": new_text,
                "old_binary": old_binary,
                "new_binary": new_binary,
            }
        )

    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "parent_generation_id": parent_id,
        "files": files,
        "provenance": FROM_SNAPSHOT,
        "provenance_note": "",
    }


__all__ = [
    "read_generation_file",
    "build_generation_patches",
    "build_generation_diff",
]
