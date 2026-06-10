"""Mutation-surface types: the addressable mutable regions and edits.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Mutation surface
# ---------------------------------------------------------------------------

#: Granularity of a mutable region in the inner-harness source tree.
#:
#: * ``"span"`` — a single annotated span (typically a string literal or
#:   string-valued statement) immediately preceded by a marker comment.
#:   Span granularity is the default; it gives the proposer typed targets
#:   without exposing surrounding control flow.
#: * ``"file"`` — a whole file declared mutable as one unit via a
#:   top-of-file marker. Intended for prompt modules whose strings are
#:   tightly coupled. Validator constraints (imports survive, syntax
#:   parses) bound what a file-level rewrite can do.
#: * ``"code"`` — a pointed code region delimited by a
#:   ``# zicato:mutable:code`` opening marker and a
#:   ``# zicato:mutable:end`` closing sentinel. The content is the
#:   verbatim source lines BETWEEN the two markers (control flow, not a
#:   string literal). Unlike ``"file"`` it exposes only the annotated
#:   block — the surface needed to rewrite a tool's slugify / path
#:   logic without handing the proposer the whole module. The applier
#:   replaces the region body verbatim; the validator's post-apply
#:   syntax + import checks bound what a code-region rewrite can do.
MutationKind = Literal["span", "file", "code"]


@dataclass(frozen=True, slots=True)
class MutationPoint:
    """An annotated mutable region in an inner-harness source tree.

    Mutation points are enumerated by a ``HarnessAdapter`` and addressed
    by stable :attr:`id` from :class:`Patch` instances. The id MUST be
    stable across generations so a proposer can re-target the same span
    after a previous generation rewrote its neighborhood; the contract
    is "same logical mutable region → same id".

    Fields
    ------
    id:
        Globally unique mutation-point identifier within a generation.
        Stable across generations — adapters compute ids from a hash of
        the marker's structural position, not from the line range, so
        unrelated edits to other parts of the file do not invalidate it.
    kind:
        Granularity of the region (see :data:`MutationKind`).
    file:
        Absolute path to the source file the region lives in.
    source_root:
        Absolute path to the source-root tree this point lives under.
        A single harness may expose mutable surface across multiple
        source roots (forward-compat for the goldfive-as-target dogfood
        plan); this field disambiguates which root the patch applier
        should resolve relative paths against.
    line_start, line_end:
        1-indexed inclusive line range of the region's CURRENT content.
        Line numbers will drift as patches land; callers MUST re-enumerate
        before applying patches if they cached an older snapshot.
    content:
        Current text of the mutable region — for ``"span"`` kind, the
        span body (without the marker comment); for ``"file"`` kind, the
        whole file contents.
    content_hash:
        Hex-encoded SHA-256 of :attr:`content`. The patch applier checks
        this before applying a patch so a stale proposer round cannot
        clobber an already-rewritten region.
    metadata:
        Adapter-specific structured metadata. Common keys include
        ``"required_placeholders"`` (comma-separated f-string-style
        placeholders the rewritten content must preserve), ``"language"``
        (e.g. ``"text"`` / ``"markdown"`` / ``"python"``), and
        ``"role"`` (e.g. ``"system_prompt"`` / ``"tool_description"``).
        All values are strings to keep the structure JSON-friendly without
        per-key converters.
    """

    id: str
    kind: MutationKind
    file: Path
    source_root: Path
    line_start: int
    line_end: int
    content: str
    content_hash: str
    metadata: Mapping[str, str] = field(default_factory=dict)


#: The operation a :class:`Patch` performs on its target mutation point.
#:
#: * ``"replace"`` — overwrite :attr:`MutationPoint.content` with
#:   :attr:`Patch.new_content`. The most general op; works on both span
#:   and file mutation kinds.
#: * ``"set_numeric"`` — replace the target with the decimal rendering
#:   of :attr:`Patch.new_numeric`. Used when an adapter has typed the
#:   mutation point as numeric (e.g. a threshold, a budget) so the
#:   proposer doesn't need to handle string formatting.
#: * ``"set_enum"`` — replace the target with :attr:`Patch.new_enum`, a
#:   string the adapter has declared belongs to a finite enum (e.g. a
#:   strategy name, a routing key).
PatchOpKind = Literal["replace", "set_numeric", "set_enum"]


@dataclass(frozen=True, slots=True)
class Patch:
    """A single proposed edit to one mutation point.

    Patches are produced by the proposer, bundled into an
    :class:`Experiment`, and consumed by the patch applier. They carry
    a per-patch :attr:`id` (uuid4 hex by convention) so the journal
    can refer to individual patches when an experiment's outcome is
    ambiguous across multiple patches.

    Exactly one of :attr:`new_content`, :attr:`new_numeric`,
    :attr:`new_enum` is populated; which one is implied by :attr:`op`.
    The dataclass does not enforce that invariant — the patch applier
    raises at apply time on a mismatch. This keeps the dataclass cheap
    to construct from JSON dicts in tests and fixtures.

    Fields
    ------
    id:
        Stable per-patch identifier (uuid4 hex by convention).
    mutation_id:
        The :attr:`MutationPoint.id` this patch targets.
    op:
        The kind of edit (see :data:`PatchOpKind`).
    new_content:
        New text for ``"replace"`` ops; ``None`` otherwise.
    new_numeric:
        New numeric value for ``"set_numeric"`` ops; ``None`` otherwise.
        Floats cover both int- and float-typed mutation points; the
        applier formats them according to adapter-supplied metadata.
    new_enum:
        New enum value for ``"set_enum"`` ops; ``None`` otherwise.
    rationale:
        One-sentence reason this specific patch is being applied. Joined
        with the broader :class:`HypothesisSpec` in the journal but stored
        per-patch so multi-patch experiments don't lose granularity.
    """

    id: str
    mutation_id: str
    op: PatchOpKind
    new_content: str | None
    new_numeric: float | None
    new_enum: str | None
    rationale: str
