"""zicato.mutation — annotation-driven mutation surface for inner harnesses.

The mutation package implements the operator-facing contract described in
``project_zicato_mutation_surface``: a source tree is annotated with
``# zicato:mutable id="..."`` marker comments; the enumerator walks those
markers and produces a stable list of :class:`MutationPoint` instances; the
applier consumes :class:`Patch` instances against a fresh enumeration of a
target snapshot; the validator confirms that a post-apply snapshot still
parses, still resolves every id, and still preserves required placeholders.

Public API
----------

``enumerate_mutations(source_roots)``
    Walk one or more source roots, return all :class:`MutationPoint`
    instances in a deterministic order.
``apply_patches(source_root, patches, target_root)``
    Copy the source tree into ``target_root`` and apply patches against a
    fresh enumeration of the copied tree. Raise on unresolved ids.
``validate_post_apply(target_root, patches, pre_apply_mutations)``
    Return a list of validation errors after a patch application; empty
    list means clean.
``check_forbidden_ids(patches, forbidden_ids)``
    Return errors when patches target ids the caller has forbidden.
"""

from __future__ import annotations

from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.markers import (
    MARKER_FILE_PREFIX,
    MARKER_SPAN_PREFIX,
    ParsedMarker,
    parse_marker_line,
)
from zicato.mutation.validator import check_forbidden_ids, validate_post_apply

__all__ = [
    "enumerate_mutations",
    "apply_patches",
    "validate_post_apply",
    "check_forbidden_ids",
    "MARKER_FILE_PREFIX",
    "MARKER_SPAN_PREFIX",
    "ParsedMarker",
    "parse_marker_line",
]
