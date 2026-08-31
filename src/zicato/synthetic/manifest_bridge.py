"""Bridge a goldfive optimization manifest into zicato MutationPoint records.

Target 2 of the dogfood plan optimizes goldfive's *own* steering layer.
The mutable surface there is declared by goldfive's
``goldfive/optimization/manifest.toml`` — a typed inventory of every
prompt body and numeric threshold the optimizer is allowed to touch —
rather than by inline ``# zicato:mutable`` comment markers. The default
enumerator (:mod:`zicato.mutation.enumerator`) walks for those markers,
so without a bridge it returns zero mutation points against the
goldfive worktree even though the surface IS declared, just in a
different shape.

This module is the bridge: given a candidate source root, it looks for
a goldfive optimization manifest under one of the conventional paths
(``goldfive/optimization/manifest.toml``,
``optimization/manifest.toml``), parses it, and yields one
:class:`~zicato.core.types.MutationPoint` per manifest entry. Prompt
entries point at the markdown copy under
``goldfive/optimization/prompts/``; numeric entries point at the
``<module>.py`` file the manifest's ``source`` field names.

The bridge is intentionally tolerant — when no manifest is found, it
returns an empty list. Callers compose its output with the regular
enumerator's output without caring whether the source root is a
goldfive worktree or a regular zicato target.

Why a bridge instead of changing goldfive
-----------------------------------------
Goldfive ships the manifest as part of its public optimization surface
(see ``goldfive/optimization/__init__.py``); sprinkling
``# zicato:mutable`` markers throughout the goldfive tree would tie
the upstream's source layout to one specific downstream optimizer.
Keeping the bridge on zicato's side means goldfive's manifest stays
the canonical inventory and any other optimizer that wants to consume
it can do so without negotiating zicato-specific comment syntax.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from zicato.core.types import MutationPoint
from zicato.mutation.enumerator import _content_hash

# Conventional manifest locations relative to a candidate source root.
# Listed in priority order; the first existing path wins.
_MANIFEST_CANDIDATES: tuple[str, ...] = (
    "goldfive/optimization/manifest.toml",
    "optimization/manifest.toml",
)

# The separator the goldfive prompt-markdown files use between header
# metadata and the prompt body. Mirrors :func:`goldfive.optimization.prompts._read_disk`.
_PROMPT_BODY_SEPARATOR = "\n---\n"


def _find_manifest_with_root(source_root: Path) -> tuple[Path, Path] | None:
    """Return ``(manifest_path, effective_root)``, or ``None``.

    The effective root is the directory the manifest's ``source`` fields
    are relative to: the base a :data:`_MANIFEST_CANDIDATES` entry was
    joined to in order to find the manifest — ``source_root`` itself, or
    the child directory the one-level-deep probe matched.

    Returning the base directly is what makes BOTH conventional layouts
    work. Re-deriving it as ``manifest_path.parents[2]`` is correct for the
    three-component ``goldfive/optimization/manifest.toml`` but lands one
    level ABOVE the checkout for the two-component
    ``optimization/manifest.toml`` form, and every ``source`` under that
    layout would then resolve to a non-existent path
    and every entry was dropped. That shape enumerated zero points, so
    correcting it can only add points, never move an existing one.
    """

    root = Path(source_root).resolve()
    for base in (root, *(sorted(root.iterdir()) if root.is_dir() else ())):
        if base is not root and not base.is_dir():
            continue
        for rel in _MANIFEST_CANDIDATES:
            candidate = base / rel
            if candidate.is_file():
                return candidate, base
    return None


def find_manifest(source_root: Path) -> Path | None:
    """Return the path to a goldfive optimization manifest under ``source_root``.

    Walks the conventional candidate locations in order and returns
    the first that exists. When the source root is a *parent* directory
    that contains a goldfive worktree as a sub-directory (the typical
    ``v0/snapshot/`` layout the orchestrator's baseline-seeder produces
    when a single ``--mutable-tree /path/to/goldfive`` is registered),
    we also probe one level deep — checking each immediate child
    directory for the same conventional locations. This keeps the bridge
    invariant under the orchestrator's snapshot-into-named-subdir
    convention without requiring callers to know about it.

    Returns ``None`` when no manifest is found so the bridge can be
    invoked unconditionally without raising on non-goldfive source
    roots.
    """
    found = _find_manifest_with_root(source_root)
    return None if found is None else found[0]


def _load_manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    """Parse the manifest TOML and return its ``[[mutation]]`` array.

    Falls back to an empty list when the manifest is malformed — the
    bridge is best-effort and a broken manifest should not crash the
    orchestrator. The caller still gets the regular enumerator's output.
    """
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    entries = raw.get("mutation", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _prompt_body_range(text: str) -> tuple[int, int, str]:
    """Return ``(line_start, line_end, body)`` for a goldfive prompt markdown file.

    Lines are 1-indexed and inclusive on both ends. ``line_start`` points
    at the first body line AFTER the ``\\n---\\n`` separator;
    ``line_end`` is the last non-trailing-newline body line.

    For files without the separator (rare; goldfive's prompt files all
    have one) the whole file is treated as the body — the bridge is
    permissive so a malformed prompt file does not erase the mutation
    point entirely.
    """
    sep_idx = text.find(_PROMPT_BODY_SEPARATOR)
    if sep_idx == -1:
        body = text
        line_start = 1
    else:
        body_start_char = sep_idx + len(_PROMPT_BODY_SEPARATOR)
        body = text[body_start_char:]
        # 1-indexed line number of the first body character.
        line_start = text.count("\n", 0, body_start_char) + 1

    if body.endswith("\n"):
        body_stripped = body[:-1]
    else:
        body_stripped = body
    # Inclusive line end: line_start + (number of newlines remaining in body_stripped).
    line_end = line_start + body_stripped.count("\n")
    return line_start, line_end, body_stripped


def _prompt_mutation_point(entry: dict[str, Any], source_root: Path) -> MutationPoint | None:
    """Build a span-kind :class:`MutationPoint` for a ``kind="prompt"`` entry.

    Returns ``None`` if the entry's ``source`` field is malformed or
    the referenced markdown file is missing — defensive, the bridge
    should never crash on a half-broken manifest.
    """
    source = entry.get("source")
    mutation_id = entry.get("id")
    if not isinstance(source, str) or not isinstance(mutation_id, str):
        return None
    target = (source_root / source).resolve()
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    line_start, line_end, body = _prompt_body_range(text)
    metadata: dict[str, str] = {
        "manifest_kind": "prompt",
        "python_attr": str(entry.get("python_attr", "")),
        "language": "markdown",
    }
    raw_placeholders = entry.get("required_placeholders", [])
    if isinstance(raw_placeholders, list):
        placeholders = [str(p) for p in raw_placeholders if isinstance(p, str)]
        if placeholders:
            metadata["required_placeholders"] = ",".join(placeholders)
    raw_tags = entry.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags if isinstance(t, str)]
        if tags:
            metadata["tags"] = ",".join(tags)
    description = entry.get("description")
    if isinstance(description, str) and description:
        metadata["description"] = description
    return MutationPoint(
        id=mutation_id,
        kind="span",
        file=target,
        source_root=source_root,
        line_start=line_start,
        line_end=line_end,
        content=body,
        content_hash=_content_hash(body),
        metadata=metadata,
    )


def _resolve_numeric_target(source_root: Path, source: str) -> Path | None:
    """Resolve a numeric entry's ``source`` to a concrete .py path.

    The manifest's numeric ``source`` shape is
    ``"goldfive/<module>.py:<ATTR>"`` (regex-validated upstream); the
    target is the path before the colon, joined to ``source_root``.
    Returns ``None`` if the path is missing or malformed.
    """
    if ":" not in source:
        return None
    file_part, _, _attr = source.partition(":")
    target = (source_root / file_part).resolve()
    if not target.is_file():
        return None
    return target


def _numeric_mutation_point(entry: dict[str, Any], source_root: Path) -> MutationPoint | None:
    """Build a span-kind :class:`MutationPoint` for a ``kind="numeric"`` entry.

    Numeric mutation points cover a single line (the attribute
    assignment); the bridge sets ``line_start = line_end`` to a
    plausible placeholder of 1 because the applier's
    ``set_numeric`` path locates the constant via AST walk anyway, not
    via the recorded line range. The :attr:`MutationPoint.content` field
    captures the manifest's declared default rendered as a string so
    the proposer prompt can show the operator what the current value
    is.

    Numeric range / type metadata is stashed under the ``"min"`` /
    ``"max"`` / ``"type"`` keys for the proposer's range-check (see
    :func:`zicato.proposer.structured._validate_numeric_range`).
    """
    source = entry.get("source")
    mutation_id = entry.get("id")
    if not isinstance(source, str) or not isinstance(mutation_id, str):
        return None
    target = _resolve_numeric_target(source_root, source)
    if target is None:
        return None
    default = entry.get("default")
    content_text = str(default) if default is not None else ""
    metadata: dict[str, str] = {
        "manifest_kind": "numeric",
        "python_attr": str(entry.get("python_attr", "")),
        "type": str(entry.get("type", "")),
    }
    raw_range = entry.get("range")
    if isinstance(raw_range, list) and len(raw_range) == 2:
        try:
            lo = float(raw_range[0])
            hi = float(raw_range[1])
            metadata["min"] = str(lo)
            metadata["max"] = str(hi)
        except (TypeError, ValueError):
            pass
    raw_tags = entry.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags if isinstance(t, str)]
        if tags:
            metadata["tags"] = ",".join(tags)
    description = entry.get("description")
    if isinstance(description, str) and description:
        metadata["description"] = description
    return MutationPoint(
        id=mutation_id,
        kind="span",
        file=target,
        source_root=source_root,
        line_start=1,
        line_end=1,
        content=content_text,
        content_hash=_content_hash(content_text),
        metadata=metadata,
    )


def enumerate_manifest_points(source_roots: Iterable[Path]) -> list[MutationPoint]:
    """Return the manifest-derived :class:`MutationPoint` list for ``source_roots``.

    For each root: find an optimization manifest, parse it, and emit
    one mutation point per entry. Roots with no manifest contribute
    nothing. Roots whose manifest is malformed contribute nothing
    (best-effort).

    The "source root" of each emitted :class:`MutationPoint` is the
    directory that contains the manifest's parent ``goldfive/`` (or
    ``optimization/``) folder — i.e. the goldfive worktree itself,
    even when :func:`find_manifest` had to probe one level deep to
    discover it. This keeps the MutationPoint's ``file.relative_to(
    source_root)`` path consistent with goldfive's own layout
    convention.

    The output is sorted by ``(source_root, id)`` for deterministic
    iteration. Composes cleanly with
    :func:`zicato.mutation.enumerator.enumerate_mutations`: callers
    concatenate the two lists and feed them to the proposer / applier.
    """
    out: list[MutationPoint] = []
    for raw_root in source_roots:
        root = Path(raw_root).resolve()
        found = _find_manifest_with_root(root)
        if found is None:
            continue
        # The effective source root is the checkout the manifest's
        # ``source`` fields are relative to — ``root`` itself for a
        # root-level manifest, or the child directory the deep probe
        # matched. See :func:`_find_manifest_with_root`.
        manifest_path, effective_root = found
        for entry in _load_manifest_entries(manifest_path):
            kind = entry.get("kind")
            point: MutationPoint | None
            if kind == "prompt":
                point = _prompt_mutation_point(entry, effective_root)
            elif kind == "numeric":
                point = _numeric_mutation_point(entry, effective_root)
            else:
                point = None
            if point is not None:
                out.append(point)
    out.sort(key=lambda p: (str(p.source_root), p.id))
    return out


__all__ = ["enumerate_manifest_points", "find_manifest"]
