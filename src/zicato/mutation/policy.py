"""The parent source and mutation permissions a proposal must preserve."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from zicato.core.mutation import MutationPoint, Patch
from zicato.epoch.snapshot_scope import is_artifact
from zicato.mutation.applier import operation_byte_spans
from zicato.mutation.enumerator import enumerate_mutations, relocate_enumeration_roots
from zicato.mutation.validator import check_forbidden_ids, duplicate_mutation_ids, validate_patches


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Canonical file identity: relative path, bytes, and executable state.

    Timestamps, directory entries, and non-executable permission bits are
    not generation content. Symbolic links and special files are refused:
    the patch applier cannot reproduce their identity across source stores.
    """

    path: str
    sha256: str
    executable: bool


def source_files(root: Path) -> tuple[SourceFile, ...]:
    """Read generation content without following links or hiding read failures."""
    if not root.is_dir():
        raise ValueError(f"source directory is missing: {root}")
    files: list[SourceFile] = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            if is_artifact(path):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                visit(path)
            elif stat.S_ISREG(mode):
                files.append(
                    SourceFile(
                        path.relative_to(root).as_posix(),
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        bool(mode & 0o111),
                    )
                )
            else:
                raise ValueError(f"unsupported source file type: {path.relative_to(root)}")

    visit(root)
    return tuple(files)


def _point_key(point: MutationPoint, root: Path) -> tuple[object, ...]:
    return (
        point.id,
        point.kind,
        point.file.resolve().relative_to(root.resolve()).as_posix(),
        point.line_start,
        point.line_end,
        point.content,
        point.content_hash,
        tuple(sorted(point.metadata.items())),
    )


def _operation_sources(point: MutationPoint) -> dict[str, bytes]:
    raw = point.file.read_bytes()
    return {
        operation: raw[start:end] for operation, (start, end) in operation_byte_spans(point).items()
    }


@dataclass(frozen=True, slots=True)
class MutationPolicy:
    """An immutable parent identity, declared surface, and forbidden set."""

    root: Path
    points: tuple[MutationPoint, ...]
    forbidden_ids: frozenset[str]
    parent_files: tuple[SourceFile, ...]
    enumeration_roots: tuple[Path, ...]

    @classmethod
    def capture(
        cls,
        root: Path,
        points: Sequence[MutationPoint],
        forbidden_ids: Iterable[str] = (),
        *,
        enumeration_roots: Sequence[Path] | None = None,
    ) -> MutationPolicy:
        root = root.resolve()
        selected = tuple(relocate_enumeration_roots(root, root, enumeration_roots))
        frozen = tuple(replace(p, metadata=MappingProxyType(dict(p.metadata))) for p in points)
        policy = cls(root, frozen, frozenset(forbidden_ids), source_files(root), selected)
        current = enumerate_mutations(list(selected))
        if duplicate_mutation_ids(frozen) or duplicate_mutation_ids(current):
            raise ValueError("mutation snapshot contains ambiguous mutation ids")
        declared = {p.id: _point_key(p, root) for p in frozen}
        live = {p.id: _point_key(p, root) for p in current}
        changed = sorted(
            mid for mid in declared.keys() | live.keys() if declared.get(mid) != live.get(mid)
        )
        if changed:
            raise ValueError(
                "mutation snapshot is missing, malformed, or stale against parent source: "
                + ", ".join(changed)
            )
        return policy

    def roots_in(self, child: Path) -> list[Path]:
        """The captured selection translated into a child source tree."""
        return relocate_enumeration_roots(self.root, child, self.enumeration_roots)

    def check_parent(self) -> list[str]:
        try:
            if source_files(self.root) != self.parent_files:
                return ["parent source changed after the mutation policy was captured"]
        except (OSError, ValueError) as exc:
            return [str(exc)]
        return []

    def check_patches(self, patches: Sequence[Patch]) -> list[str]:
        return (
            self.check_parent()
            + validate_patches(list(patches), enumeration=list(self.points))
            + check_forbidden_ids(list(patches), list(self.forbidden_ids))
        )

    def check_child(self, child: Path) -> list[str]:
        """Catch a permitted outer replacement changing a forbidden inner point."""
        errors = self.check_parent()
        try:
            edited = enumerate_mutations(self.roots_in(child))
            duplicates = duplicate_mutation_ids(edited)
            by_id = {p.id: p for p in edited}
            for point in self.points:
                if point.id not in self.forbidden_ids:
                    continue
                after = by_id.get(point.id)
                if (
                    point.id in duplicates
                    or after is None
                    or point.kind != after.kind
                    or point.file.relative_to(self.root) != after.file.relative_to(child)
                    or point.metadata != after.metadata
                    or _operation_sources(point) != _operation_sources(after)
                ):
                    errors.append(f"forbidden mutation point {point.id!r} changed in child source")
        except (OSError, ValueError, SyntaxError) as exc:
            errors.append(f"cannot verify child mutation policy: {exc}")
        return errors
