"""Mutation-site browser views for the dashboard.

This module backs the dashboard **Files** view's *mutation-site
browser* section. Where :mod:`zicato.dashboard.filetree` renders a
generation's whole source tree and applied patch set, this module
renders the **mutation surface** — the ``# zicato:mutable id="..."``
annotated spans that are zicato's editable region — and, for each, a
diff of the ``v0`` baseline content against the content in any
generation whose patch touched that mutation id.

Why re-enumerate rather than re-apply — WHEN THE TREE IS THERE
-------------------------------------------------------
A generation's snapshot tree IS the post-apply state — the applier
already materialised it. So the "patched content" of a mutation id in
generation ``vN`` is simply that id's :class:`MutationPoint.content`
when :func:`zicato.mutation.enumerator.enumerate_mutations` walks
``vN``'s snapshot. There is no need to re-run the applier here: the
enumerator is deterministic and re-enumerating the materialised tree is
both cheaper and exactly faithful to what the run actually saw.

The ``v0`` enumeration gives the *original* content of every id; any
later generation's enumeration gives that id's *patched* content. The
patch records (read through :class:`zicato.storage.StorageBackend`) tell the
view which generations *intentionally* touched a given
id, so the frontend can label the diff with the patch's rationale.

That argument holds only where a tree exists — and it is exactly the
enrichment path below.

...and why the RECORDS come first
---------------------------------
Trees are not permanent. :mod:`zicato.epoch.gc` prunes generation source
trees and keeps every record, and an archived or relocated workspace can
arrive with no trees at all. Re-enumerating a tree that is gone yields
an empty surface. Enumerating alone would therefore blank a closed epoch's
whole mutation browser while the records that describe it sat unread
(issue #194 §6).

Two records reconstruct the surface, and neither is ever pruned:

* ``epochs/{id}/mutations.json`` — the round's own frozen enumeration
  (id, kind, file, line span, content, content hash), read through the
  ONE reader that keeps every field,
  :func:`zicato.analyzer.report_data.load_mutation_surface`.
* the per-generation patch records — a ``replace`` patch carries its
  ``new_content`` forever, and a ``set_numeric`` / ``set_enum`` patch
  carries the value the applier wrote into the site.

So: **records first, trees as enrichment, and an honest caption when the
tree is gone.** Every content-bearing part of these payloads therefore
declares its ``provenance`` — ``"snapshot"`` (re-enumerated, exact) or
``"records"`` (reconstructed) — and a records-sourced payload also
carries the caption the view must render. Never silence.

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

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from zicato.analyzer.report_data import load_mutation_surface
from zicato.core.types import MutationPoint
from zicato.epoch.genstore import (
    GIT_WORKTREES_DIRNAME,
    GenerationStore,
    default_generation_store,
)
from zicato.epoch.journal import read_generation_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.query import WorkspacePaths
from zicato.query.paths import layout_of
from zicato.storage import default_backend
from zicato.workspace_loader import activate_mutation_surface

#: The seed / baseline generation id. ``v0`` is the original tree every
#: mutation-site diff is taken *against*.
_BASELINE_GENERATION = "v0"

#: Where a content-bearing field came from. ``"snapshot"``: re-enumerated
#: from a materialised source tree, exactly what the round saw.
#: ``"records"``: reconstructed from the epoch's surviving records.
FROM_SNAPSHOT = "snapshot"
FROM_RECORDS = "records"

#: The caption a view MUST render beside a ``records``-sourced mutation
#: surface. Threaded onto the payload (``provenance_note``) rather than
#: spelled in the frontend so the server's honesty and the rendered
#: honesty cannot drift apart.
RECORDS_CAPTION = "snapshot pruned · reconstructed from records"

#: The same caption for the OTHER way a tree goes missing: the store
#: cannot reach any tree in an epoch that plainly has generations — an
#: archived or relocated workspace, or one whose declared storage backend
#: no longer matches what is on disk. Saying "pruned" there would be a
#: guess the operator can see is wrong (the snapshot directories are
#: sitting right in front of them).
UNREACHABLE_CAPTION = "snapshot unreachable · reconstructed from records"

#: The caption for a pruned generation's *file* views. Whole-tree
#: browsing is genuinely unrecoverable; the spans its patches touched
#: are. Consumed by :mod:`zicato.dashboard.filetree`.
SPANS_CAPTION = "full tree pruned by GC · patch-touched spans reconstructed"

#: The same caption for the OTHER way the tree side goes missing: the reader
#: cannot reach ANY tree — the workspace declares no generation source
#: backend, or one its source data contradicts. The spans are reconstructed
#: identically; only the explanation differs, and "pruned by GC" would be a
#: retention claim about trees that are very likely still on disk.
SPANS_UNREACHABLE_CAPTION = "snapshot unreachable · patch-touched spans reconstructed"

#: The per-generation tree directory each backend materialises under.
#: ``mutations.json`` records the ABSOLUTE path the enumerating round
#: walked, so a site's repo-relative path is whatever follows that root.
_SNAPSHOT_DIRNAME = "snapshot"
_WORKTREES_DIRNAME = GIT_WORKTREES_DIRNAME

#: A numeric / string constant in a span's own text — the shapes
#: ``set_numeric`` and ``set_enum`` rewrite. Bounded away from
#: identifiers and attribute access so ``v2`` and ``x.5`` are not read as
#: constants.
_NUMERIC_LITERAL_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])")
_STRING_LITERAL_RE = re.compile(
    r"'''.*?'''|\"\"\".*?\"\"\"|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"",
    re.DOTALL,
)


def _resolve_store(paths: WorkspacePaths) -> tuple[GenerationStore | None, str]:
    """The workspace's generation store, or ``(None, reason)`` when there is none.

    Mirrors :func:`zicato.dashboard.filetree._resolve_store` — the
    mutation-site browser is backend-neutral by routing every read through
    the store seam, exactly like the file-tree browser it sits beside.

    Naming a source backend is workspace configuration, so building the
    store can fail on the CONFIGURATION rather than on any generation: a
    workspace whose ``config.json`` predates ``generation_source_backend``,
    or whose value contradicts the source data on disk, has no store at
    all. That is a condition the reader must report rather than raise on:
    every view here already answers from records when a tree cannot be read,
    so a store that cannot be built degrades onto the same records path and
    reports the reason. The dashboard is read-only and must never answer 500
    for a workspace it was merely pointed at.
    """
    try:
        return default_generation_store(paths.root), ""
    except (FileNotFoundError, OSError, ValueError) as exc:
        return None, str(exc)


def _enumerate_generation(
    store: GenerationStore | None, epoch_id: str, generation_id: str, workspace_root: Path
) -> dict[str, MutationPoint]:
    """Enumerate one generation's mutation surface, keyed by mutation id.

    Returns an empty mapping when the generation has no materialised
    source tree — the caller degrades to "no sites" rather than raising.
    The enumeration runs against the generation's snapshot root, so the
    :class:`MutationPoint.content` it yields is that generation's
    *current* (post-apply, for a derived generation) content.

    ``workspace_root`` supplies the contract's declared syntax table
    (MUTATION-SURFACE.md §2.5) — without it the browser would show a
    narrower surface than the run enumerated whenever the workspace
    declares a file type beyond the built-ins.
    """
    activate_mutation_surface(workspace_root)
    if store is None or not store.has_generation(epoch_id, generation_id):
        return {}
    try:
        root = store.materialize_snapshot(epoch_id, generation_id)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not Path(root).is_dir():
        return {}
    points = enumerate_mutations([Path(root)])
    # An id is unique within a generation's surface; last-wins is just a
    # defensive tiebreak — the enumerator already dedups in practice.
    return {p.id: p for p in points}


def _has_tree(store: GenerationStore | None, epoch_id: str, generation_id: str) -> bool:
    """Return ``True`` when the generation still has a materialised tree.

    :meth:`GenerationStore.has_generation` is the existence test on both
    backends (a ``snapshot/`` directory, or a generation tag), and it is
    the ONE question that decides tree-or-records for a generation. No
    store at all answers the same as no tree.
    """
    if store is None:
        return False
    try:
        return store.has_generation(epoch_id, generation_id)
    except (OSError, ValueError):
        return False


def recorded_generation_ids(paths: WorkspacePaths, epoch_id: str) -> list[str]:
    """Generation ids from the epoch's RECORD directories — no trees involved.

    ``epochs/{id}/generations/{gen}/`` is written by the journal and
    survives snapshot GC by construction; it exists under BOTH storage
    backends, because the git backend relocates the trees rather than the
    records. So this is the post-hoc answer to "which generations did
    this epoch mint", and the way to tell a PRUNED generation (recorded,
    no tree) from one that never existed.
    """
    gens_root = layout_of(paths).generations_dir(epoch_id)
    try:
        return sorted(child.name for child in gens_root.iterdir() if child.is_dir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []


def _generation_ids(
    store: GenerationStore | None, paths: WorkspacePaths, epoch_id: str
) -> tuple[list[str], bool]:
    """Every generation the epoch ever minted, and whether the store saw any.

    The ids are the union of the store's listing (trees / tags) and
    :func:`recorded_generation_ids`, so a generation whose tree is gone
    still gets its column in the site × generation matrix.

    The flag is the store's own answer, kept because it distinguishes the
    two ways a tree goes missing (:func:`_records_caption`) — asking the
    store twice would cost a second git call to learn what this walk
    already knows.
    """
    try:
        from_store = [] if store is None else list(store.list_generations(epoch_id))
    except (FileNotFoundError, OSError, ValueError):
        from_store = []
    return sorted(set(from_store) | set(recorded_generation_ids(paths, epoch_id))), bool(from_store)


def _surface_error(epoch_id: str, store_error: str) -> str:
    """Name why an epoch has no mutation surface at all.

    Both reads are always attempted, so the sentence names both. When the
    generation store could not be built the tree read never had a chance,
    and the configuration reason is the actionable half — it is appended
    rather than substituted, because the ABSENT record is still a fact the
    operator needs.
    """
    sentence = (
        f"no mutation surface for {epoch_id}: no {_BASELINE_GENERATION} source tree "
        "and no mutations.json record"
    )
    return f"{sentence}; {store_error}" if store_error else sentence


def _records_caption(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_ids: list[str],
    store_saw_trees: bool,
) -> str:
    """The caption naming WHY the records had to serve.

    A tree goes missing two ways, and they are not the same news: snapshot
    GC pruned it (records-by-design, :mod:`zicato.epoch.gc`), or the configured
    store cannot reach source directories that remain on disk. The second is a
    workspace-level condition rather than a retention decision. This diagnostic scan
    never selects a backend; ``config.json`` remains the only selection source.
    """
    if generation_ids and not store_saw_trees:
        generations_root = layout_of(paths).generations_dir(epoch_id)
        worktrees_root = paths.root / GIT_WORKTREES_DIRNAME / epoch_id
        source_evidence = any(
            (generations_root / generation_id / _SNAPSHOT_DIRNAME).is_dir()
            or (worktrees_root / generation_id).is_dir()
            for generation_id in generation_ids
        )
        if source_evidence:
            return UNREACHABLE_CAPTION
    return RECORDS_CAPTION


def _split_record_path(raw: str) -> tuple[Path, str]:
    """Split a recorded mutation-point path into ``(source root, rel path)``.

    ``mutations.json`` stores the ABSOLUTE path the enumerating round
    walked — a path inside that round's generation tree, which by
    definition is gone when the record is all that is left (and
    which points somewhere else entirely once a workspace is moved or
    copied). The source root is whatever precedes the backend's
    per-generation tree directory: ``…/generations/{gen}/snapshot/`` for
    the directory backend, ``…/{worktrees}/{epoch}/{gen}/`` for the git
    backend. With neither anchor present the file's own parent serves,
    which narrows the rendered location to a bare filename — less than
    the enumeration knew, never wrong.
    """
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    if not parts:
        return Path("."), ""
    split_at = len(parts) - 1
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == _SNAPSHOT_DIRNAME:
            split_at = i + 1
            break
        if parts[i] == _WORKTREES_DIRNAME:
            split_at = min(i + 3, len(parts) - 1)
            break
    return Path(*parts[:split_at]), "/".join(parts[split_at:])


def _int_or_zero(raw: Any) -> int:
    return int(raw) if isinstance(raw, int | float) else 0


def _record_surface(paths: WorkspacePaths, epoch_id: str) -> dict[str, MutationPoint]:
    """Rebuild an epoch's mutation surface from ``mutations.json``.

    The round wrote that file from the very enumeration it fed the
    proposer, so every site's content and line span is the real thing.

    Two gaps remain, and the caller captions them rather than papering over
    them. The snapshot carries no ``metadata``, so a site's ``role`` reads
    empty. And it is the enumeration of the round's *champion*, which is
    ``v0`` for an epoch that never promoted, and the promoted parent
    otherwise.
    """
    points: dict[str, MutationPoint] = {}
    for record in load_mutation_surface(layout_of(paths), epoch_id):
        mutation_id = record.get("id")
        if not isinstance(mutation_id, str) or not mutation_id:
            continue
        source_root, rel = _split_record_path(str(record.get("file") or ""))
        kind = record.get("kind")
        points[mutation_id] = MutationPoint(
            id=mutation_id,
            kind=kind if kind in ("span", "file", "code") else "span",
            file=source_root / rel if rel else source_root,
            source_root=source_root,
            line_start=_int_or_zero(record.get("line_start")),
            line_end=_int_or_zero(record.get("line_end")),
            content=str(record.get("content") or ""),
            content_hash=str(record.get("content_hash") or ""),
            metadata={},
        )
    return points


def _baseline_surface(
    store: GenerationStore | None, paths: WorkspacePaths, epoch_id: str
) -> tuple[dict[str, MutationPoint], str]:
    """The epoch's baseline surface and where it came from.

    Tree first: while ``v0``'s snapshot is on disk its enumeration is
    exactly what the round saw, down to each site's ``role``. Once the
    tree is gone the frozen ``mutations.json`` enumeration serves in its
    place. Returns ``(points, provenance)``; the provenance reads
    ``records`` whenever the tree could not answer, including when the
    records could not either — the records WERE consulted, and the caller
    reports the empty surface naming both.
    """
    points = _enumerate_generation(store, epoch_id, _BASELINE_GENERATION, paths.root)
    if points:
        return points, FROM_SNAPSHOT
    return _record_surface(paths, epoch_id), FROM_RECORDS


def _reconstruct_content(baseline_content: str, patch: Any) -> tuple[str | None, str, bool]:
    """Reconstruct a patch's post-apply content from the patch record.

    Returns ``(content, note, from_baseline)``; ``content`` is ``None``
    when the record cannot honestly produce one, and ``note`` says why.
    ``from_baseline`` is ``True`` when the content was produced by
    substituting into ``baseline_content`` rather than carried by the
    record itself — the reconstruction is then only as right as the
    baseline it was substituted into, and the caller must say which
    baseline that was.

    * ``replace`` — the applier writes ``new_content`` into the span
      verbatim, so the record IS the new content, exactly.
    * ``set_numeric`` / ``set_enum`` — the record carries a VALUE, not
      text: the applier rewrites the first constant after the site's
      marker. Reconstructing therefore means substituting that value into
      the baseline span's own text, using the applier's own renderer so
      the two spellings cannot drift. When the baseline span holds no
      constant of the matching type the applier's target sat outside the
      enumerated span, and there is no faithful span to show.
    """
    # The applier's renderer rather than a second spelling of it: a copy here
    # would drift from what the round actually wrote into the tree.
    from zicato.mutation.applier import _format_numeric  # noqa: PLC0415

    op = getattr(patch, "op", "")
    if op == "replace":
        new_content = getattr(patch, "new_content", None)
        if new_content is None:
            return None, "the patch record carries no replacement content", False
        return str(new_content), "", False

    # The two value ops differ only in which constant they rewrite and how
    # the value renders back into source.
    value_ops: dict[str, tuple[str, str, re.Pattern[str], Callable[[Any], str]]] = {
        "set_numeric": (
            "new_numeric",
            "numeric",
            _NUMERIC_LITERAL_RE,
            lambda v: _format_numeric(float(v)),
        ),
        "set_enum": ("new_enum", "enum", _STRING_LITERAL_RE, lambda v: repr(str(v))),
    }
    spec = value_ops.get(op)
    if spec is None:
        return None, f"unknown patch op {op!r}", False
    field, label, pattern, render = spec
    value = getattr(patch, field, None)
    if value is None:
        return None, f"the patch record carries no {label} value", False
    rendered = render(value)
    if not pattern.search(baseline_content):
        return None, f"{op} {rendered} — the constant sits outside the recorded span", False
    return pattern.sub(lambda _m: rendered, baseline_content, count=1), "", True


def _recorded_patch(
    workspace_root: Path, epoch_id: str, generation_id: str, patch_id: str
) -> Any | None:
    """The recorded :class:`~zicato.core.types.Patch` behind one patch id."""
    try:
        record = read_generation_patches(default_backend(workspace_root), epoch_id, generation_id)
    except (FileNotFoundError, OSError, ValueError):
        return None
    for patch in record.patches:
        if getattr(patch, "id", "") == patch_id:
            return patch
    return None


def _rel_file(point: MutationPoint) -> str:
    """Return a mutation point's file path relative to its source root.

    The enumerator stores absolute paths; the dashboard only ever shows
    the ``/``-joined path relative to the generation's source root so the
    rendered location matches the file-tree browser's paths.

    The unresolved paths are tried first: a source root staged as
    per-file symlinks resolves each file to its origin tree, outside the
    root, which would strip the whole directory prefix. Resolving is only
    the fallback for when the unresolved forms disagree (a symlinked or
    relative root), and the bare filename the last resort.
    """
    for file_path, root in (
        (Path(point.file), Path(point.source_root)),
        (Path(point.file).resolve(), Path(point.source_root).resolve()),
    ):
        try:
            return "/".join(file_path.relative_to(root).parts)
        except ValueError:
            continue
    return Path(point.file).name


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
    workspace_root: Path, epoch_id: str, generation_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Map each mutation id to the generations whose patch set touched it.

    Walks every non-baseline generation's recorded patch set through
    ``StorageBackend`` and records, per mutation id, which generations carry
    a patch against it and that patch's rationale. A generation can appear
    once per id it patched.
    """
    by_mutation: dict[str, list[dict[str, Any]]] = {}
    backend = default_backend(workspace_root)
    for generation_id in generation_ids:
        if generation_id == _BASELINE_GENERATION:
            continue
        try:
            record = read_generation_patches(backend, epoch_id, generation_id)
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

    ``provenance`` says which read answered — ``"snapshot"`` (``v0``'s
    tree was re-enumerated) or ``"records"`` (the tree is gone and the
    epoch's frozen enumeration served) — and ``provenance_note`` carries
    the caption the view renders in the records case.

    An epoch with neither a tree nor a record yields an empty
    ``mutations`` list and an ``error`` naming both, rather than raising.
    """
    store, store_error = _resolve_store(paths)
    generation_ids, store_saw_trees = _generation_ids(store, paths, epoch_id)

    baseline, provenance = _baseline_surface(store, paths, epoch_id)
    if not baseline:
        return {
            "epoch_id": epoch_id,
            "generations": generation_ids,
            "mutations": [],
            "provenance": provenance,
            "provenance_note": "",
            "error": _surface_error(epoch_id, store_error),
        }

    patched = _patching_generations(paths.root, epoch_id, generation_ids)
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
    payload = {
        "epoch_id": epoch_id,
        "generations": generation_ids,
        "mutations": mutations,
        "provenance": provenance,
        "provenance_note": (
            _records_caption(paths, epoch_id, generation_ids, store_saw_trees)
            if provenance == FROM_RECORDS
            else ""
        ),
    }
    if store_error:
        # The records answered, so the surface renders; the configuration
        # that kept the trees out of it is still named rather than left for
        # the operator to infer from a suspiciously record-shaped view.
        payload["error"] = store_error
    return payload


def build_mutation_detail(paths: WorkspacePaths, epoch_id: str, mutation_id: str) -> dict[str, Any]:
    """Return one mutation site's baseline content and per-generation diffs.

    The response carries:

    * ``baseline`` — the original: ``{generation_id, content, file, role,
      line_start, line_end, provenance}``. ``generation_id`` is ``v0``
      when ``v0``'s tree answered; it is ``null`` on the records path,
      where the frozen enumeration is the round's champion surface and
      claiming ``v0`` would be a guess.
    * ``versions`` — one entry per generation (other than ``v0``) whose
      patch set touched this id, each with the *patched* content — as it
      stands in that generation's snapshot, or reconstructed from the
      patch record when the tree is gone — plus the patch's ``op``,
      ``rationale`` and its own ``provenance``. The frontend diffs
      ``baseline.content`` against each ``versions[i].content``.
      A reconstruction that had to substitute a recorded VALUE into a
      span's text carries ``reconstructed_against`` naming the generation
      whose text it substituted into (``v0``); anything an intermediate
      generation wrote at the site is absent from such an entry, and the
      view captions it rather than presenting it as that generation's own.
    * ``provenance_note`` — the caption to render whenever any part of
      the response was reconstructed from records; empty otherwise.

    A generation that patched a *different* id, or a generation with no
    patch set, never appears in ``versions`` — only generations that
    intentionally rewrote *this* site are shown.

    A missing baseline, or an id absent from the baseline surface, comes
    back as an ``error`` field, never an exception.
    """
    store, store_error = _resolve_store(paths)
    baseline, baseline_provenance = _baseline_surface(store, paths, epoch_id)
    if not baseline:
        return {
            "epoch_id": epoch_id,
            "mutation_id": mutation_id,
            "provenance": baseline_provenance,
            "error": _surface_error(epoch_id, store_error),
        }
    baseline_point = baseline.get(mutation_id)
    if baseline_point is None:
        return {
            "epoch_id": epoch_id,
            "mutation_id": mutation_id,
            "provenance": baseline_provenance,
            "error": f"mutation id {mutation_id!r} not found in baseline surface",
        }

    generation_ids, store_saw_trees = _generation_ids(store, paths, epoch_id)
    patched = _patching_generations(paths.root, epoch_id, generation_ids)
    touching = patched.get(mutation_id, [])

    versions: list[dict[str, Any]] = []
    # One enumeration per touching generation; cached so a generation that
    # appears twice (two patches against the same id) is enumerated once.
    enum_cache: dict[str, dict[str, MutationPoint]] = {}
    for patch_info in touching:
        generation_id = patch_info["generation_id"]
        entry = {
            "generation_id": generation_id,
            "patch_id": patch_info["patch_id"],
            "op": patch_info["op"],
            "rationale": patch_info["rationale"],
        }
        if not _has_tree(store, epoch_id, generation_id):
            # The tree is gone (GC, or an archived workspace). The patch
            # record still says what this generation wrote into the site.
            patch = _recorded_patch(paths.root, epoch_id, generation_id, patch_info["patch_id"])
            content, note, from_baseline = (
                _reconstruct_content(baseline_point.content, patch)
                if patch is not None
                else (None, "no patch record for this generation", False)
            )
            entry.update({"content": content, "provenance": FROM_RECORDS, "note": note})
            if from_baseline:
                # The value ops record a VALUE rather than text, so this content is
                # that value substituted into the BASELINE span — v0's text
                # with this generation's constant in it. Whatever a generation
                # in between wrote at the site is NOT represented, so the
                # content cannot be presented as this generation's own without
                # naming what it was reconstructed against.
                entry["reconstructed_against"] = _BASELINE_GENERATION
            versions.append(entry)
            continue
        if generation_id not in enum_cache:
            enum_cache[generation_id] = _enumerate_generation(
                store, epoch_id, generation_id, paths.root
            )
        point = enum_cache[generation_id].get(mutation_id)
        if point is None:
            # The tree IS here and its patch set named this id, but the
            # tree does not enumerate it (the patch moved or removed the
            # marker). The tree is authoritative about its own contents, so
            # report this as a finding rather than reconstructing it:
            # surface the patch metadata without a content diff.
            entry.update(
                {
                    "content": None,
                    "provenance": FROM_SNAPSHOT,
                    "error": "mutation id no longer enumerable in this generation",
                }
            )
            versions.append(entry)
            continue
        entry.update(
            {
                "content": point.content,
                "content_hash": point.content_hash,
                "file": _rel_file(point),
                "line_start": point.line_start,
                "line_end": point.line_end,
                "provenance": FROM_SNAPSHOT,
            }
        )
        versions.append(entry)

    reconstructed = baseline_provenance == FROM_RECORDS or any(
        v["provenance"] == FROM_RECORDS for v in versions
    )
    detail = _point_summary(baseline_point)
    detail.update(
        {
            "epoch_id": epoch_id,
            "baseline": {
                "generation_id": (
                    _BASELINE_GENERATION if baseline_provenance == FROM_SNAPSHOT else None
                ),
                "content": baseline_point.content,
                "content_hash": baseline_point.content_hash,
                "file": _rel_file(baseline_point),
                "role": baseline_point.metadata.get("role", ""),
                "line_start": baseline_point.line_start,
                "line_end": baseline_point.line_end,
                "provenance": baseline_provenance,
            },
            "versions": versions,
            "provenance_note": (
                _records_caption(paths, epoch_id, generation_ids, store_saw_trees)
                if reconstructed
                else ""
            ),
        }
    )
    return detail


def reconstructed_spans(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> list[dict[str, Any]]:
    """What a pruned generation can still be shown as: its patched spans.

    One entry per patch in the generation's record, shaped like a file
    diff's entries (``path``, ``status``, ``old_content``,
    ``new_content``) so one renderer covers both — but flagged
    ``reconstructed`` and carrying the ``span`` it actually describes.
    These are the mutation site's lines, NOT the file's: a tree that GC
    collected has no browsable file content and never will again, while
    the spans its patches touched are recoverable, and they are the part
    anyone opened the diff for.

    The old side is the epoch's baseline surface — ``v0``'s tree, or the
    frozen enumeration behind it. For a generation derived mid-chain that
    is one step further back than its own parent; the caller's caption
    says the whole view is reconstructed, and a chain of reconstructions
    would compound guesses rather than reduce them.

    ``new_content`` is ``None`` when the record cannot honestly produce
    one; the entry's ``note`` says why, and the view renders the note
    instead of a diff.
    """
    store, _store_error = _resolve_store(paths)
    try:
        record = read_generation_patches(default_backend(paths.root), epoch_id, generation_id)
    except (FileNotFoundError, OSError, ValueError):
        return []
    if not record.patches:
        return []
    baseline, _ = _baseline_surface(store, paths, epoch_id)

    spans: list[dict[str, Any]] = []
    for patch in record.patches:
        mutation_id = str(getattr(patch, "mutation_id", "") or "")
        point = baseline.get(mutation_id)
        old_content = point.content if point is not None else ""
        new_content, note, _ = _reconstruct_content(old_content, patch)
        spans.append(
            {
                "path": _rel_file(point) if point is not None else "",
                "status": "modified",
                "old_content": old_content,
                "new_content": new_content,
                "old_binary": False,
                "new_binary": False,
                "reconstructed": True,
                "note": note,
                "span": {
                    "mutation_id": mutation_id,
                    "op": str(getattr(patch, "op", "") or ""),
                    "rationale": str(getattr(patch, "rationale", "") or ""),
                    "line_start": point.line_start if point is not None else None,
                    "line_end": point.line_end if point is not None else None,
                },
            }
        )
    return spans


__all__ = [
    "FROM_RECORDS",
    "FROM_SNAPSHOT",
    "RECORDS_CAPTION",
    "SPANS_CAPTION",
    "build_mutation_detail",
    "build_mutation_index",
    "reconstructed_spans",
    "recorded_generation_ids",
]
