"""Mutation-site browser views for the dashboard.

This module backs the dashboard **Files** view's *mutation-site
browser* section. Where :mod:`zicato.dashboard.filetree` renders a
generation's whole source tree and applied patch set, this module
renders the **mutation surface** — the ``# zicato:mutable id="..."``
annotated spans that are zicato's editable region — and, for each, a
diff of the ``v0`` baseline content against the content in any
generation whose patch touched that mutation id.

Why re-enumerate, not re-apply
------------------------------
A generation's snapshot tree IS the post-apply state — the applier
already materialised it. So the "patched content" of a mutation id in
generation ``vN`` is simply that id's :class:`MutationPoint.content`
when :func:`zicato.mutation.enumerator.enumerate_mutations` walks
``vN``'s snapshot. There is no need to re-run the applier here: the
enumerator is deterministic and re-enumerating the materialised tree is
both cheaper and exactly faithful to what the run actually saw.

The ``v0`` enumeration gives the *original* content of every id; any
later generation's enumeration gives that id's *patched* content. The
patch records (read through the :class:`~zicato.epoch.genstore.GenerationStore`
seam) tell the view which generations *intentionally* touched a given
id, so the frontend can label the diff with the patch's rationale.

Endpoints (wired in :mod:`zicato.dashboard.server`)
---------------------------------------------------
* ``GET /api/mutations/{epoch}`` → :func:`build_mutation_index` — every
  mutation site in the epoch's baseline, each with its file, role, and
  the list of generations that patched it.
* ``GET /api/mutations/{epoch}/{mutation_id}`` →
  :func:`build_mutation_detail` — one mutation site's baseline content
  plus, per patching generation, the patched content for a diff.

Both are deterministic: same workspace bytes, same response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.core.types import MutationPoint
from zicato.epoch.genstore import GenerationStore, default_generation_store
from zicato.mutation.enumerator import enumerate_mutations
from zicato.query import WorkspacePaths

#: The seed / baseline generation id. ``v0`` is the original tree every
#: mutation-site diff is taken *against*.
_BASELINE_GENERATION = "v0"


def _store(paths: WorkspacePaths) -> GenerationStore:
    """Build the workspace's generation store (directory or git backend).

    Mirrors :func:`zicato.dashboard.filetree._store` — the mutation-site
    browser is backend-neutral by routing every read through the store
    seam, exactly like the file-tree browser it sits beside.
    """
    return default_generation_store(paths.root)


def _enumerate_generation(
    store: GenerationStore, epoch_id: str, generation_id: str
) -> dict[str, MutationPoint]:
    """Enumerate one generation's mutation surface, keyed by mutation id.

    Returns an empty mapping when the generation has no materialised
    source tree — the caller degrades to "no sites" rather than raising.
    The enumeration runs against the generation's snapshot root, so the
    :class:`MutationPoint.content` it yields is that generation's
    *current* (post-apply, for a derived generation) content.
    """
    if not store.has_generation(epoch_id, generation_id):
        return {}
    try:
        root = store.snapshot_root(epoch_id, generation_id)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not Path(root).is_dir():
        return {}
    points = enumerate_mutations([Path(root)])
    # An id is unique within a generation's surface; last-wins is just a
    # defensive tiebreak — the enumerator already dedups in practice.
    return {p.id: p for p in points}


def _rel_file(point: MutationPoint) -> str:
    """Return a mutation point's file path relative to its source root.

    The enumerator stores absolute paths; the dashboard only ever shows
    the ``/``-joined path relative to the generation's source root so the
    rendered location matches the file-tree browser's paths.
    """
    try:
        rel = Path(point.file).resolve().relative_to(Path(point.source_root).resolve())
    except ValueError:
        return Path(point.file).name
    return "/".join(rel.parts)


def _point_summary(point: MutationPoint) -> dict[str, Any]:
    """Render the identity fields of a mutation point for the wire.

    The ``role`` is lifted out of :attr:`MutationPoint.metadata` because
    the UI lists it next to the id; the rest of ``metadata`` rides along
    untouched for any future per-site annotation.
    """
    return {
        "mutation_id": point.id,
        "kind": point.kind,
        "file": _rel_file(point),
        "role": point.metadata.get("role", ""),
        "line_start": point.line_start,
        "line_end": point.line_end,
        "metadata": dict(point.metadata),
    }


def _patching_generations(
    store: GenerationStore, epoch_id: str, generation_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Map each mutation id to the generations whose patch set touched it.

    Walks every non-baseline generation's recorded patch set (through the
    :meth:`GenerationStore.list_patches` seam) and records, per mutation
    id, which generations carry a patch against it and that patch's
    rationale. A generation can appear once per id it patched.
    """
    by_mutation: dict[str, list[dict[str, Any]]] = {}
    for generation_id in generation_ids:
        if generation_id == _BASELINE_GENERATION:
            continue
        try:
            record = store.list_patches(epoch_id, generation_id)
        except (FileNotFoundError, OSError, ValueError):
            continue
        for patch in record.patches:
            mutation_id = getattr(patch, "mutation_id", None)
            if not mutation_id:
                continue
            by_mutation.setdefault(mutation_id, []).append(
                {
                    "generation_id": generation_id,
                    "patch_id": getattr(patch, "id", ""),
                    "op": getattr(patch, "op", ""),
                    "rationale": getattr(patch, "rationale", "") or "",
                }
            )
    return by_mutation


def build_mutation_index(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Return every mutation site in an epoch's baseline surface.

    The index is the mutation-site browser's collapsed view: each entry
    carries the site's id, file, role and line span (from the ``v0``
    enumeration) plus the list of generations that patched it. The
    frontend lazy-loads a single site's diff via
    :func:`build_mutation_detail` on selection.

    A missing baseline generation yields an empty ``mutations`` list and
    an ``error`` field rather than raising — the view degrades to "no
    mutation surface" instead of a 500.
    """
    store = _store(paths)
    generation_ids = []
    try:
        generation_ids = store.list_generations(epoch_id)
    except (FileNotFoundError, OSError, ValueError):
        generation_ids = []

    baseline = _enumerate_generation(store, epoch_id, _BASELINE_GENERATION)
    if not baseline:
        return {
            "epoch_id": epoch_id,
            "generations": generation_ids,
            "mutations": [],
            "error": f"no baseline ({_BASELINE_GENERATION}) mutation surface for {epoch_id}",
        }

    patched = _patching_generations(store, epoch_id, generation_ids)
    mutations: list[dict[str, Any]] = []
    # Deterministic order: the enumerator sorts by (root, file, line, id);
    # iterate the baseline ids in that same order.
    for mutation_id in sorted(
        baseline,
        key=lambda mid: (
            _rel_file(baseline[mid]),
            baseline[mid].line_start,
            mid,
        ),
    ):
        point = baseline[mutation_id]
        entry = _point_summary(point)
        entry["patched_by"] = patched.get(mutation_id, [])
        entry["patched_generation_ids"] = [p["generation_id"] for p in entry["patched_by"]]
        mutations.append(entry)
    return {
        "epoch_id": epoch_id,
        "generations": generation_ids,
        "mutations": mutations,
    }


def build_mutation_detail(paths: WorkspacePaths, epoch_id: str, mutation_id: str) -> dict[str, Any]:
    """Return one mutation site's baseline content and per-generation diffs.

    The response carries:

    * ``baseline`` — the ``v0`` original: ``{generation_id, content,
      file, role, line_start, line_end}``.
    * ``versions`` — one entry per generation (other than ``v0``) whose
      patch set touched this id, each with the *patched* content as it
      stands in that generation's snapshot, plus the patch's ``op`` and
      ``rationale``. The frontend diffs ``baseline.content`` against each
      ``versions[i].content``.

    A generation that patched a *different* id, or a generation with no
    patch set, never appears in ``versions`` — only generations that
    intentionally rewrote *this* site are shown.

    A missing baseline, or an id absent from the baseline surface, comes
    back as an ``error`` field, never an exception.
    """
    store = _store(paths)
    baseline = _enumerate_generation(store, epoch_id, _BASELINE_GENERATION)
    if not baseline:
        return {
            "epoch_id": epoch_id,
            "mutation_id": mutation_id,
            "error": f"no baseline ({_BASELINE_GENERATION}) mutation surface for {epoch_id}",
        }
    baseline_point = baseline.get(mutation_id)
    if baseline_point is None:
        return {
            "epoch_id": epoch_id,
            "mutation_id": mutation_id,
            "error": f"mutation id {mutation_id!r} not found in baseline surface",
        }

    try:
        generation_ids = store.list_generations(epoch_id)
    except (FileNotFoundError, OSError, ValueError):
        generation_ids = []
    patched = _patching_generations(store, epoch_id, generation_ids)
    touching = patched.get(mutation_id, [])

    versions: list[dict[str, Any]] = []
    # One enumeration per touching generation; cached so a generation that
    # appears twice (two patches against the same id) is enumerated once.
    enum_cache: dict[str, dict[str, MutationPoint]] = {}
    for patch_info in touching:
        generation_id = patch_info["generation_id"]
        if generation_id not in enum_cache:
            enum_cache[generation_id] = _enumerate_generation(store, epoch_id, generation_id)
        point = enum_cache[generation_id].get(mutation_id)
        if point is None:
            # The generation's patch set named this id but the materialised
            # tree no longer enumerates it (the patch moved/removed the
            # marker). Surface the patch metadata without a content diff.
            versions.append(
                {
                    "generation_id": generation_id,
                    "patch_id": patch_info["patch_id"],
                    "op": patch_info["op"],
                    "rationale": patch_info["rationale"],
                    "content": None,
                    "error": "mutation id no longer enumerable in this generation",
                }
            )
            continue
        versions.append(
            {
                "generation_id": generation_id,
                "patch_id": patch_info["patch_id"],
                "op": patch_info["op"],
                "rationale": patch_info["rationale"],
                "content": point.content,
                "content_hash": point.content_hash,
                "file": _rel_file(point),
                "line_start": point.line_start,
                "line_end": point.line_end,
            }
        )

    detail = _point_summary(baseline_point)
    detail.update(
        {
            "epoch_id": epoch_id,
            "baseline": {
                "generation_id": _BASELINE_GENERATION,
                "content": baseline_point.content,
                "content_hash": baseline_point.content_hash,
                "file": _rel_file(baseline_point),
                "role": baseline_point.metadata.get("role", ""),
                "line_start": baseline_point.line_start,
                "line_end": baseline_point.line_end,
            },
            "versions": versions,
        }
    )
    return detail


__all__ = [
    "build_mutation_index",
    "build_mutation_detail",
]
