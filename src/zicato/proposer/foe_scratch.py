"""The proposer's disposable working copy, and the diff it is read back as.

A proposal is an edit, not a document about an edit. The proposer is
granted write on a throwaway copy of the parent generation's snapshot,
changes files in it with ordinary editing tools, checks its work, and
answers when the checks pass. Zicato then reads the copy back as a patch
set by diffing it against the snapshot and *projecting* each change onto
the enumerated mutation points.

The projection is the rule that keeps a free-form edit loop inside the
operator's declared surface. Every changed line range must fall entirely
within one declared mutation point; a change outside every point blocks
the round with the path and the line range, so the finding names a
location rather than a verdict. What comes out is an ordinary
:class:`~zicato.core.types.Patch` set over mutation ids — the
:class:`~zicato.core.types.Experiment` schema is untouched, and everything
downstream of the proposer sees what it always saw.

The copy lives in the OS temp root under the ``ztw-pscratch-`` prefix,
distinct from the ``ztw-pvalidate-`` trees the patch verifier writes and
the ``ztw-slate-`` trees a best-of-N slate uses, so no sweep over one
family reaps another. It is removed on every exit path, including the one
where the episode raised in the middle of an edit: the snapshot itself is
never mounted writable, so the worst a lost tree costs is disk.
"""

from __future__ import annotations

import difflib
import shutil
import tempfile
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from zicato.core.types import MutationPoint, Patch
from zicato.mutation.enumerator import enumerate_mutations

#: Prefix of the proposer's working copies. Distinct from every other
#: scratch family so a sweep over one cannot reap another.
SCRATCH_PREFIX = "ztw-pscratch-"


class EditOutsideMutationPointError(Exception):
    """A change in the working copy lies outside every declared point.

    :attr:`findings` names each offending change by path and line range,
    which is what the round's blocked message carries.
    """

    def __init__(self, findings: Sequence[str]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(self.findings))


@dataclass(frozen=True, slots=True)
class ChangedRange:
    """One contiguous run of lines a working copy changed.

    ``start`` and ``end`` are 1-indexed and inclusive, and address the
    ORIGINAL file, because that is the coordinate system a mutation point
    is declared in. A pure insertion changes no original line, so it is
    attributed to the line it was inserted after, clamped into the file.
    """

    path: Path
    start: int
    end: int
    replacement: str

    def describe(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


@contextmanager
def scratch_working_copy(snapshot_root: Path) -> Iterator[Path]:
    """Yield a disposable writable copy of ``snapshot_root``.

    The copy is removed when the block exits, however it exits. The
    snapshot is only ever read here, so an episode that dies mid-edit
    leaves the tree the round is about to patch untouched.
    """
    parent = Path(tempfile.gettempdir())
    root = parent / f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        shutil.copytree(snapshot_root, root, symlinks=True)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def changed_ranges(snapshot_root: Path, scratch_root: Path) -> list[ChangedRange]:
    """Every line range the working copy changed, against the snapshot.

    Files the copy added or deleted are reported as whole-file changes,
    so the projection refuses them unless a ``file`` mutation point
    covers them — which is the same rule every other change follows.
    """
    ranges: list[ChangedRange] = []
    for relative in sorted(_union_of_files(snapshot_root, scratch_root)):
        before = _read_lines(snapshot_root / relative)
        after = _read_lines(scratch_root / relative)
        if before == after:
            continue
        ranges.extend(_ranges_between(snapshot_root / relative, before, after))
    return ranges


def project_onto_mutation_points(
    ranges: Iterable[ChangedRange],
    points: Iterable[MutationPoint],
    scratch_root: Path,
) -> list[Patch]:
    """Turn changed ranges into one patch per touched mutation point.

    A range inside a point makes that point a touched one, and the
    point's new value is read from the working copy's OWN enumeration
    rather than from its lines. The enumerator is the one authority on
    what a point's content is — for a span it is the string literal's
    body, not the statement around it — so re-reading the copy through it
    is what keeps a projected patch from splicing code into a literal.

    A point is replaced as a unit, so several edits inside one point are
    one patch rather than several conflicting ones. A range inside no
    point raises :class:`EditOutsideMutationPointError` naming every
    offender, so the proposer is told what to undo rather than which rule
    it broke; so does an edit that made a declared point stop resolving,
    because a marker the copy no longer carries is a change outside every
    point that a line range cannot see.
    """
    by_file: dict[Path, list[MutationPoint]] = {}
    for point in points:
        by_file.setdefault(Path(point.file).resolve(), []).append(point)

    touched: list[str] = []
    outside: list[str] = []
    for changed in ranges:
        owner = _owning_point(changed, by_file.get(Path(changed.path).resolve(), ()))
        if owner is None:
            outside.append(
                f"{changed.describe()} lies outside every declared mutation point; "
                "the proposer may only change what a declared point covers"
            )
        elif owner.id not in touched:
            touched.append(owner.id)
    if outside:
        raise EditOutsideMutationPointError(outside)

    edited = {point.id: point for point in enumerate_mutations([scratch_root])}
    missing = [
        f"mutation point {mutation_id!r} no longer resolves in the working copy; "
        "the marker that declares it must survive the edit"
        for mutation_id in sorted(touched)
        if mutation_id not in edited
    ]
    if missing:
        raise EditOutsideMutationPointError(missing)

    return [
        Patch(
            id=uuid.uuid4().hex,
            mutation_id=mutation_id,
            op="replace",
            new_content=edited[mutation_id].content,
            new_numeric=None,
            new_enum=None,
            rationale="Read back from the proposer's working copy.",
        )
        for mutation_id in sorted(touched)
    ]


def _owning_point(changed: ChangedRange, points: Iterable[MutationPoint]) -> MutationPoint | None:
    """The one declared point whose line range wholly contains ``changed``."""
    for point in points:
        if point.line_start <= changed.start and changed.end <= point.line_end:
            return point
    return None


def _union_of_files(left: Path, right: Path) -> set[Path]:
    return _relative_files(left) | _relative_files(right)


def _relative_files(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def _read_lines(path: Path) -> list[str]:
    """A file's lines with their endings, or empty when it is not there.

    A file the working copy added reads as empty on the snapshot side and
    a file it deleted reads as empty on the copy's side, so both appear
    to the diff as an ordinary change and meet the same projection rule.
    """
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return []


def _ranges_between(path: Path, before: list[str], after: list[str]) -> list[ChangedRange]:
    """The changed line ranges between two versions of one file."""
    ranges: list[ChangedRange] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # `i1`/`i2` are a half-open 0-indexed span of the ORIGINAL file.
        # An insertion has i1 == i2 and so covers no original line; it is
        # attributed to the line it follows, clamped into the file, so it
        # still has to fall inside a declared point.
        start = min(i1 + 1, max(len(before), 1))
        end = max(i2, start)
        ranges.append(
            ChangedRange(path=path, start=start, end=end, replacement="".join(after[j1:j2]))
        )
    return ranges


__all__ = [
    "SCRATCH_PREFIX",
    "ChangedRange",
    "EditOutsideMutationPointError",
    "changed_ranges",
    "project_onto_mutation_points",
    "scratch_working_copy",
]
