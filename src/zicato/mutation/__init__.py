"""zicato.mutation — annotation-driven mutation surface for systems under test.

The mutation package implements the operator-facing contract described in
``docs/design/MUTATION-SURFACE.md``. A source tree is annotated with
``# zicato:mutable id="..."`` marker comments. The enumerator walks those
markers and produces a stable list of :class:`MutationPoint` instances. The
applier consumes :class:`Patch` instances against a fresh enumeration of a
target snapshot. The validator confirms that a post-apply snapshot still
parses, still resolves every id, and still preserves required placeholders.

Public API
----------

``enumerate_mutations(source_roots)``
    Walk one or more source roots, return all :class:`MutationPoint`
    instances in a deterministic order.
``validate_patches(patches, *, source_root | enumeration)``
    Deterministically pre-validate a patch set against the mutation
    surface; return a list of every problem found (empty means clean).
``apply_patches(source_root, patches, target_root)``
    Copy the source tree into ``target_root``, run ``validate_patches``,
    and atomically apply the batch (all-or-nothing) against a fresh
    enumeration of the copied tree. Every unapplyable-patch-set
    condition — pre-check, apply-time missing anchor, post-apply syntax
    gate — raises ``ValueError``.
``apply_patches_unchecked(source_root, patches, target_root)``
    Legacy INTERNAL best-effort-sequential apply with no atomic
    pre-check, for callers that have already validated the patch set
    themselves. Keeps its ``KeyError``-on-missing-anchor contract; the
    checked surface above converts that to ``ValueError``.
``validate_post_apply(target_root, patches, pre_apply_mutations)``
    Return a list of validation errors after a patch application; empty
    list means clean.
``check_forbidden_ids(patches, forbidden_ids)``
    Return errors when patches target ids the caller has forbidden.
"""

from __future__ import annotations

from zicato.mutation.applier import (
    apply_patches,
    apply_patches_unchecked,
    replacement_source,
)
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.markers import (
    MARKER_FILE_PREFIX,
    MARKER_SPAN_PREFIX,
    ParsedMarker,
    parse_marker_line,
)
from zicato.mutation.validator import (
    check_forbidden_ids,
    validate_patches,
    validate_post_apply,
)

__all__ = [
    "enumerate_mutations",
    "apply_patches",
    "apply_patches_unchecked",
    "replacement_source",
    "validate_patches",
    "validate_post_apply",
    "check_forbidden_ids",
    "MARKER_FILE_PREFIX",
    "MARKER_SPAN_PREFIX",
    "ParsedMarker",
    "parse_marker_line",
]
