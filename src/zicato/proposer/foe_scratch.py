"""Project a disposable proposal copy into patches that reproduce its source.

The mutation enumerator and patch applier own the editable units. Acceptance
requires the reconstructed tree to match the working copy's canonical files,
bytes, and executable state. A line intersecting a mutation point alone is
not evidence that the point owns every edit on that line.

The proposal owner waits for host shutdown and process group termination
before leaving the working-copy context. The copy is then removed on every
exit path. The parent snapshot is never mounted writable.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from zicato.core.types import Patch
from zicato.epoch.snapshot_scope import copytree_ignore
from zicato.mutation.applier import apply_patches, replacement_source
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy, SourceFile, source_files
from zicato.mutation.validator import duplicate_mutation_ids

SCRATCH_PREFIX = "ztw-pscratch-"


class EditOutsideMutationPointError(Exception):
    """Working source cannot be reproduced under the captured mutation policy."""

    def __init__(self, findings: Sequence[str]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(self.findings))


@contextmanager
def scratch_working_copy(snapshot_root: Path) -> Iterator[Path]:
    """Yield a writable source copy and remove it when its owner exits."""
    root = Path(tempfile.gettempdir()) / f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        shutil.copytree(snapshot_root, root, symlinks=True, ignore=copytree_ignore())
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def project_working_copy(policy: MutationPolicy, scratch_root: Path) -> list[Patch]:
    """Read one patch per changed unit and prove that no source edit disappears.

    Added or deleted files and executable-bit changes cannot be expressed by
    the patch schema. Unchanged binary files remain source; changed binary
    files fail reconstruction. Artifact names use the generation store's
    scope. Read failures and unsupported file types are explicit findings.
    """
    try:
        return _project(policy, scratch_root)
    except (OSError, ValueError, SyntaxError) as exc:
        raise EditOutsideMutationPointError([str(exc)]) from exc


def _project(policy: MutationPolicy, scratch_root: Path) -> list[Patch]:
    errors = policy.check_parent()
    if errors:
        raise EditOutsideMutationPointError(errors)
    working_files = source_files(scratch_root)
    edited = enumerate_mutations(policy.roots_in(scratch_root))
    if duplicate_mutation_ids(edited):
        raise ValueError("working copy contains ambiguous mutation ids")
    edited_by_id = {point.id: point for point in edited}
    replacements: dict[str, str] = {}
    unresolved = []
    for point in policy.points:
        after = edited_by_id.get(point.id)
        if after is None:
            unresolved.append((point, "no longer resolves in the working copy"))
            continue
        if point.kind != after.kind or point.file.relative_to(
            policy.root
        ) != after.file.relative_to(scratch_root):
            unresolved.append((point, "changed its declared location or kind"))
            continue
        value = replacement_source(after)
        if replacement_source(point) != value:
            replacements[point.id] = value

    # A whole-file replacement carries changes to nested declarations. The
    # policy still checks protected points, and reconstruction checks all bytes.
    replaced_files = {
        point.file for point in policy.points if point.kind == "file" and point.id in replacements
    }
    for point, reason in unresolved:
        if point.kind == "file" or point.file not in replaced_files:
            raise ValueError(f"mutation point {point.id!r} {reason}")
    patches = [
        Patch(
            id=uuid.uuid4().hex,
            mutation_id=point.id,
            op="replace",
            new_content=replacements[point.id],
            new_numeric=None,
            new_enum=None,
            rationale="Read back from the proposer's working copy.",
        )
        for point in sorted(policy.points, key=lambda p: p.id)
        if point.id in replacements and (point.kind == "file" or point.file not in replaced_files)
    ]
    errors = policy.check_patches(patches) + policy.check_child(scratch_root)
    if errors:
        raise EditOutsideMutationPointError(errors)
    if not patches:
        _require_same_files(policy.parent_files, working_files)
        return []
    with tempfile.TemporaryDirectory(prefix="ztw-preconstruct-") as temporary:
        child = Path(temporary) / "child"
        apply_patches(policy.root, patches, child, enumeration_roots=policy.enumeration_roots)
        _require_same_files(source_files(child), working_files)
    errors = policy.check_parent()
    if errors:
        raise EditOutsideMutationPointError(errors)
    return patches


def _require_same_files(expected: tuple[SourceFile, ...], actual: tuple[SourceFile, ...]) -> None:
    left = {entry.path: entry for entry in expected}
    right = {entry.path: entry for entry in actual}
    findings = [
        f"{path}: working source differs from the reconstructed patch source; "
        "restore edits outside the declared mutation units, file set, and executable state"
        for path in sorted(left.keys() | right.keys())
        if left.get(path) != right.get(path)
    ]
    if findings:
        raise EditOutsideMutationPointError(findings)


__all__ = [
    "SCRATCH_PREFIX",
    "EditOutsideMutationPointError",
    "project_working_copy",
    "scratch_working_copy",
]
