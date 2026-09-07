"""Canonical byte-range evidence for an accepted parent-to-child source edit.

Python resolves mutation semantics through the enumerator and applier. The
independent verifier trusts those recorded intervals, then checks their
binding to actual source inventories, patch records, and changed bytes. This
is an integrity record. Fabricating a policy together with matching dependent
evidence is outside the audit's trust model. Range findings are alarm-only.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from zicato.core.mutation import Patch
from zicato.core.types import Experiment
from zicato.epoch.genstore import GenerationStore
from zicato.epoch.journal import patch_body, read_experiment_body
from zicato.mutation.applier import apply_patches, operation_byte_spans
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy, SourceFile, source_files
from zicato.storage import atomic_write_json, atomic_write_text
from zicato.workspace.layout import WorkspaceLayout


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ByteChange:
    parent_start: int
    parent_end: int
    child_start: int
    child_end: int


@dataclass(frozen=True, slots=True)
class MutationSpan:
    id: str
    kind: str
    op: str
    start: int
    end: int
    source_sha256: str
    child_start: int | None
    child_end: int | None
    child_source_sha256: str | None
    content_hash: str
    metadata: tuple[tuple[str, str], ...]
    forbidden: bool


@dataclass(frozen=True, slots=True)
class FileContainment:
    path: str
    parent_sha256: str | None
    child_sha256: str | None
    parent_executable: bool | None
    child_executable: bool | None
    spans: tuple[MutationSpan, ...]
    changes: tuple[ByteChange, ...]


@dataclass(frozen=True, slots=True)
class PatchBinding:
    id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ContainmentManifest:
    epoch_id: str
    parent_generation_id: str
    generation_id: str
    parent_source: str
    child_source: str
    patches: tuple[PatchBinding, ...]
    files: tuple[FileContainment, ...]
    policy_sha256: str
    format_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, body: Mapping[str, Any]) -> ContainmentManifest:
        """Decode exact record fields; malformed evidence cannot imply containment."""
        try:
            files = tuple(
                FileContainment(
                    **{
                        **record,
                        "spans": tuple(
                            MutationSpan(
                                **{**span, "metadata": tuple(map(tuple, span["metadata"]))}
                            )
                            for span in record["spans"]
                        ),
                        "changes": tuple(ByteChange(**change) for change in record["changes"]),
                    }
                )
                for record in body["files"]
            )
            record = cls(
                **{
                    **body,
                    "files": files,
                    "patches": tuple(PatchBinding(**patch) for patch in body["patches"]),
                }
            )
            # JSON scalar types must survive decoding without coercion.
            if json.loads(json.dumps(record.to_dict())) != body:
                raise ValueError("record fields changed during decoding")
            return record
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed containment manifest: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ContainmentFinding:
    code: str
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class RangeAttestation:
    status: Literal["contained", "violated", "unverified"]
    findings: tuple[ContainmentFinding, ...] = ()


def _byte_changes(before: bytes, after: bytes) -> tuple[ByteChange, ...]:
    """Use line anchors, then refine changed hunks to byte coordinates."""
    if before == after:
        return ()
    prefix = 0
    common = min(len(before), len(after))
    while prefix < common and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < common - prefix and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    before = before[prefix : len(before) - suffix]
    after = after[prefix : len(after) - suffix]
    left = before.splitlines(keepends=True)
    right = after.splitlines(keepends=True)
    left_offsets = [0]
    right_offsets = [0]
    for line in left:
        left_offsets.append(left_offsets[-1] + len(line))
    for line in right:
        right_offsets.append(right_offsets[-1] + len(line))
    changes: list[ByteChange] = []
    for tag, start, end, other_start, other_end in difflib.SequenceMatcher(
        a=left, b=right, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        lo, hi = left_offsets[start], left_offsets[end]
        other_lo, other_hi = right_offsets[other_start], right_offsets[other_end]
        for kind, i, j, k, m in difflib.SequenceMatcher(
            a=before[lo:hi], b=after[other_lo:other_hi], autojunk=False
        ).get_opcodes():
            if kind != "equal":
                changes.append(
                    ByteChange(
                        prefix + lo + i,
                        prefix + lo + j,
                        prefix + other_lo + k,
                        prefix + other_lo + m,
                    )
                )
    return tuple(changes)


def _valid_fields(manifest: ContainmentManifest) -> bool:
    def digest(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        )

    if not all(
        isinstance(value, str) and value
        for value in (
            manifest.epoch_id,
            manifest.parent_generation_id,
            manifest.generation_id,
        )
    ):
        return False
    for patch in manifest.patches:
        if not isinstance(patch.id, str) or not patch.id or not digest(patch.sha256):
            return False
    for record in manifest.files:
        if not safe_relative_path(record.path):
            return False
        for hashed, executable in (
            (record.parent_sha256, record.parent_executable),
            (record.child_sha256, record.child_executable),
        ):
            if not (
                (hashed is None and executable is None)
                or (digest(hashed) and type(executable) is bool)
            ):
                return False
        for span in record.spans:
            if not isinstance(span.id, str) or not span.id or not digest(span.content_hash):
                return False
            if not all(
                len(pair) == 2 and all(isinstance(value, str) for value in pair)
                for pair in span.metadata
            ):
                return False
    return True


def build_manifest(
    policy: MutationPolicy,
    child: Path,
    *,
    workspace_root: Path,
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    patches: Sequence[Patch],
    patch_records: Mapping[str, bytes],
    policy_sha256: str,
) -> ContainmentManifest:
    """Bind observed inventories and byte differences to one captured policy."""
    errors = policy.check_parent()
    if errors:
        raise ValueError("; ".join(errors))
    parent_files = {entry.path: entry for entry in policy.parent_files}
    child_files = {entry.path: entry for entry in source_files(child)}
    spans: dict[str, list[MutationSpan]] = {}
    child_points = {point.id: point for point in enumerate_mutations(policy.roots_in(child))}
    for point in policy.points:
        path = point.file.relative_to(policy.root).as_posix()
        child_point = child_points.get(point.id)
        child_spans = {}
        if (
            child_point is not None
            and child_point.kind == point.kind
            and child_point.file.relative_to(child).as_posix() == path
        ):
            child_spans = operation_byte_spans(child_point)
        before = point.file.read_bytes()
        after = child_point.file.read_bytes() if child_point is not None else b""
        for operation, (start, end) in operation_byte_spans(point).items():
            child_start, child_end = child_spans.get(operation, (None, None))
            spans.setdefault(path, []).append(
                MutationSpan(
                    point.id,
                    point.kind,
                    operation,
                    start,
                    end,
                    _hash(before[start:end]),
                    child_start,
                    child_end,
                    _hash(after[child_start:child_end]) if child_start is not None else None,
                    point.content_hash,
                    tuple(sorted(point.metadata.items())),
                    point.id in policy.forbidden_ids,
                )
            )
    records: list[FileContainment] = []
    for path in sorted(parent_files.keys() | child_files.keys()):
        before = (policy.root / path).read_bytes() if path in parent_files else b""
        after = (child / path).read_bytes() if path in child_files else b""
        records.append(
            FileContainment(
                path,
                parent_files[path].sha256 if path in parent_files else None,
                child_files[path].sha256 if path in child_files else None,
                parent_files[path].executable if path in parent_files else None,
                child_files[path].executable if path in child_files else None,
                tuple(spans.get(path, ())),
                _byte_changes(before, after),
            )
        )
    return ContainmentManifest(
        epoch_id,
        parent_generation_id,
        generation_id,
        policy.root.relative_to(workspace_root.resolve()).as_posix(),
        child.resolve().relative_to(workspace_root.resolve()).as_posix(),
        tuple(PatchBinding(patch.id, _hash(patch_records[patch.id])) for patch in patches),
        tuple(records),
        policy_sha256,
    )


def safe_relative_path(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and "\x00" not in value
        and (
            not PurePosixPath(value).is_absolute()
            and all(part not in ("", ".", "..") for part in value.split("/"))
        )
    )


def _policy_points(policy: MutationPolicy) -> list[dict[str, Any]]:
    points = []
    for point in policy.points:
        source = point.file.read_bytes()
        for op, (start, end) in operation_byte_spans(point).items():
            points.append(
                {
                    "path": point.file.relative_to(policy.root).as_posix(),
                    "id": point.id,
                    "kind": point.kind,
                    "op": op,
                    "start": start,
                    "end": end,
                    "source_sha256": _hash(source[start:end]),
                    "content_hash": point.content_hash,
                    "metadata": [list(pair) for pair in sorted(point.metadata.items())],
                    "forbidden": point.id in policy.forbidden_ids,
                }
            )
    return points


def _read_policy_input(path: Path) -> bytes:
    if not path.is_file() or path.resolve() != path.absolute():
        raise ValueError(f"mutation policy input is not a regular canonical file: {path}")
    return path.read_bytes()


def write_mutation_policy(
    workspace_root: Path,
    *,
    epoch_id: str,
    parent_generation_id: str,
    policy: MutationPolicy,
) -> str:
    """Retain source and permissions before proposal work under their content hash.

    A digest names one immutable record. A corrupt existing record is refused;
    changing the selected roots or frozen inputs produces another identity.
    """
    from zicato.proposer.brief import load_brief  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace_root.resolve())
    errors = policy.check_parent()
    if errors:
        raise ValueError("; ".join(errors))
    brief = _read_policy_input(layout.brief(epoch_id))
    if frozenset(load_brief(layout.brief(epoch_id)).forbidden_ids) != policy.forbidden_ids:
        raise ValueError("captured mutation permissions differ from the frozen proposer brief")
    body = {
        "format_version": 1,
        "epoch_id": epoch_id,
        "parent_generation_id": parent_generation_id,
        "parent_source": policy.root.relative_to(workspace_root.resolve()).as_posix(),
        "source_files": [asdict(entry) for entry in policy.parent_files],
        "enumeration_roots": [
            path.relative_to(policy.root).as_posix() for path in policy.enumeration_roots
        ],
        "brief_sha256": _hash(brief),
        "scoring_sha256": _hash(_read_policy_input(layout.scoring(epoch_id))),
        "points": _policy_points(policy),
    }
    data = (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8")
    digest = _hash(data)
    target = layout.mutation_policy(epoch_id, parent_generation_id, digest)
    if target.resolve() != target.absolute():
        raise ValueError("immutable mutation policy location traverses a symbolic link")
    if target.exists() or target.is_symlink():
        if _read_policy_input(target) != data:
            raise ValueError("immutable mutation policy record differs from its content hash")
    else:
        atomic_write_text(target, data.decode("utf-8"))
    return digest


def _verify_policy(
    manifest: ContainmentManifest,
    workspace_root: Path,
    parent: Path,
    parent_files: Sequence[SourceFile],
) -> None:
    """Check retained policy facts without reinterpreting mutation syntax."""
    digest = manifest.policy_sha256
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("manifest has no valid retained mutation policy identity")
    layout = WorkspaceLayout.from_root(workspace_root.resolve())
    data = _read_policy_input(
        layout.mutation_policy(manifest.epoch_id, manifest.parent_generation_id, digest)
    )
    if _hash(data) != digest:
        raise ValueError("retained mutation policy differs from its content hash")
    body = json.loads(data)
    expected_fields = {
        "format_version",
        "epoch_id",
        "parent_generation_id",
        "parent_source",
        "source_files",
        "enumeration_roots",
        "brief_sha256",
        "scoring_sha256",
        "points",
    }
    if (
        not isinstance(body, dict)
        or body.keys() != expected_fields
        or type(body["format_version"]) is not int
        or body["format_version"] != 1
    ):
        raise ValueError("retained mutation policy is malformed or unsupported")
    if (body["epoch_id"], body["parent_generation_id"], body["parent_source"]) != (
        manifest.epoch_id,
        manifest.parent_generation_id,
        manifest.parent_source,
    ):
        raise ValueError("retained mutation policy names a different parent")
    if json.dumps(body["source_files"], sort_keys=True) != json.dumps(
        [asdict(entry) for entry in parent_files], sort_keys=True
    ):
        raise ValueError(
            "retained mutation policy names different source bytes or executable state"
        )
    roots = body["enumeration_roots"]
    if (
        not isinstance(roots, list)
        or not roots
        or any(
            not isinstance(path, str)
            or (path != "." and not safe_relative_path(path))
            or not (parent / path).resolve().is_relative_to(parent)
            or not (parent / path).exists()
            for path in roots
        )
    ):
        raise ValueError("retained mutation policy has invalid enumeration roots")
    if body["brief_sha256"] != _hash(_read_policy_input(layout.brief(manifest.epoch_id))) or body[
        "scoring_sha256"
    ] != _hash(_read_policy_input(layout.scoring(manifest.epoch_id))):
        raise ValueError("retained mutation policy differs from frozen contract inputs")
    points = [
        {
            "path": record.path,
            **{key: value for key, value in asdict(span).items() if not key.startswith("child_")},
        }
        for record in manifest.files
        for span in record.spans
    ]

    def key(point: dict[str, Any]) -> tuple[str, str, str]:
        return point["path"], point["id"], point["op"]

    if json.dumps(sorted(body["points"], key=key), sort_keys=True) != json.dumps(
        sorted(points, key=key), sort_keys=True
    ):
        raise ValueError("manifest permissions differ from the retained mutation policy")
    if any(
        not any((parent / point["path"]).is_relative_to(parent / root) for root in roots)
        for point in body["points"]
    ):
        raise ValueError("retained mutation points escape the selected enumeration roots")


def _within(change: ByteChange, span: MutationSpan) -> bool:
    return (
        span.start <= change.parent_start <= change.parent_end <= span.end
        and span.child_start is not None
        and span.child_end is not None
        and span.child_start <= change.child_start <= change.child_end <= span.child_end
    )


def _overlaps(change: ByteChange, span: MutationSpan) -> bool:
    if change.parent_start == change.parent_end:
        return (
            span.start < change.parent_start < span.end
            or span.start == span.end == change.parent_start
        )
    return change.parent_start < span.end and span.start < change.parent_end


def verify_manifest(
    manifest: ContainmentManifest,
    *,
    workspace_root: Path,
    parent_root: Path,
    child_root: Path,
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    patch_records: Mapping[str, bytes],
    patch_ids: Sequence[str],
) -> RangeAttestation:
    """Verify complete observed source and patch bindings before range claims."""
    findings: list[ContainmentFinding] = []

    def finding(code: str, path: str, detail: str) -> None:
        findings.append(ContainmentFinding(code, path, detail))

    if not _valid_fields(manifest):
        return RangeAttestation(
            "unverified", (ContainmentFinding("manifest_shape", "", "invalid record fields"),)
        )
    if manifest.format_version != 1 or type(manifest.format_version) is not int:
        return RangeAttestation(
            "unverified", (ContainmentFinding("manifest_version", "", "unsupported version"),)
        )
    if (manifest.epoch_id, manifest.parent_generation_id, manifest.generation_id) != (
        epoch_id,
        parent_generation_id,
        generation_id,
    ):
        finding("manifest_coordinates", "", "manifest does not name the selected parent and child")
        return RangeAttestation("unverified", tuple(findings))
    if not all(
        safe_relative_path(path) for path in (manifest.parent_source, manifest.child_source)
    ):
        finding(
            "manifest_source", "", "source locations must be relative paths inside the workspace"
        )
        return RangeAttestation("unverified", tuple(findings))
    parent = parent_root.resolve()
    child = child_root.resolve()
    root = workspace_root.resolve()
    if (
        not parent.is_relative_to(root)
        or not child.is_relative_to(root)
        or (parent.relative_to(root).as_posix(), child.relative_to(root).as_posix())
        != (manifest.parent_source, manifest.child_source)
    ):
        finding(
            "manifest_source",
            "",
            "manifest source locations differ from the selected generation store",
        )
        return RangeAttestation("unverified", tuple(findings))
    try:
        left = {entry.path: entry for entry in source_files(parent)}
        right = {entry.path: entry for entry in source_files(child)}
    except (OSError, ValueError) as exc:
        return RangeAttestation(
            "unverified", (ContainmentFinding("source_unreadable", "", str(exc)),)
        )
    try:
        _verify_policy(manifest, workspace_root, parent, tuple(left.values()))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        finding("policy_binding", "", str(exc))
    if [p.id for p in manifest.patches] != list(patch_ids) or len(set(patch_ids)) != len(patch_ids):
        finding("patch_binding", "", "patch identities differ from the experiment")
    for patch in manifest.patches:
        if patch.id not in patch_records or _hash(patch_records[patch.id]) != patch.sha256:
            finding("patch_binding", "", f"patch record {patch.id!r} differs from the manifest")
    recorded = {record.path for record in manifest.files}
    if len(recorded) != len(manifest.files) or recorded != left.keys() | right.keys():
        finding(
            "source_inventory", "", "manifest does not cover the complete observed file inventory"
        )
    operation_ids: set[tuple[str, str]] = set()
    child_operation_ids: set[tuple[str, str]] = set()
    if not manifest.patches or not any(record.spans for record in manifest.files):
        finding(
            "mutation_snapshot", "", "accepted patches and their mutation snapshot are required"
        )
    forbidden_ids = {
        span.id for record in manifest.files for span in record.spans if span.forbidden
    }
    for record in manifest.files:
        path = record.path
        if not safe_relative_path(path):
            finding("manifest_path", str(path), "invalid source path")
            continue
        before = (parent / path).read_bytes() if path in left else b""
        after = (child / path).read_bytes() if path in right else b""
        observed = (
            left[path].sha256 if path in left else None,
            right[path].sha256 if path in right else None,
            left[path].executable if path in left else None,
            right[path].executable if path in right else None,
        )
        if observed != (
            record.parent_sha256,
            record.child_sha256,
            record.parent_executable,
            record.child_executable,
        ):
            finding("source_binding", path, "observed source hashes or executable state differ")
            continue
        if path not in left or path not in right:
            finding("file_set", path, "patches do not authorize file creation or deletion")
        if record.parent_executable != record.child_executable:
            finding("metadata", path, "patches do not authorize executable state changes")
        valid_spans = True
        for span in record.spans:
            if (
                (span.id, span.op) in operation_ids
                or span.op not in ("replace", "set_numeric", "set_enum")
                or not isinstance(span.id, str)
                or not span.id
                or span.kind not in ("span", "file", "code")
                or type(span.forbidden) is not bool
                or type(span.start) is not int
                or type(span.end) is not int
                or not 0 <= span.start <= span.end <= len(before)
                or _hash(before[span.start : span.end]) != span.source_sha256
                or not (
                    (span.child_start, span.child_end, span.child_source_sha256)
                    == (None, None, None)
                    or (
                        type(span.child_start) is int
                        and type(span.child_end) is int
                        and 0 <= span.child_start <= span.child_end <= len(after)
                        and _hash(after[span.child_start : span.child_end])
                        == span.child_source_sha256
                    )
                )
            ):
                finding("mutation_snapshot", path, "invalid, repeated, or mismatched mutation span")
                valid_spans = False
            operation_ids.add((span.id, span.op))
            if span.child_start is not None:
                child_operation_ids.add((span.id, span.op))
        if not valid_spans:
            continue
        if any(
            span.forbidden and span.source_sha256 != span.child_source_sha256
            for span in record.spans
        ):
            finding("forbidden", path, "a forbidden mutation unit's bytes changed")
        parent_offset = child_offset = 0
        for change in record.changes:
            if not all(type(value) is int for value in asdict(change).values()) or not (
                parent_offset <= change.parent_start <= change.parent_end <= len(before)
                and child_offset <= change.child_start <= change.child_end <= len(after)
            ):
                finding("byte_coverage", path, "invalid or overlapping change coordinates")
                break
            if (
                before[parent_offset : change.parent_start]
                != after[child_offset : change.child_start]
            ):
                finding("byte_coverage", path, "unrecorded source bytes changed")
            if not any(not span.forbidden and _within(change, span) for span in record.spans):
                finding(
                    "outside_mutation", path, "changed bytes are outside allowed mutation units"
                )
            if any(span.forbidden and _overlaps(change, span) for span in record.spans):
                finding("forbidden", path, "changed bytes overlap a forbidden mutation unit")
            parent_offset, child_offset = change.parent_end, change.child_end
        if before[parent_offset:] != after[child_offset:]:
            finding("byte_coverage", path, "unrecorded source suffix changed")
    for patch in manifest.patches:
        try:
            body = json.loads(patch_records[patch.id])
            if body["id"] != patch.id or (body["mutation_id"], body["op"]) not in operation_ids:
                finding("patch_binding", "", "patch does not resolve against the mutation snapshot")
            elif (body["mutation_id"], body["op"]) not in child_operation_ids:
                finding(
                    "mutation_snapshot", "", "patched operation no longer resolves in the child"
                )
            elif body["mutation_id"] in forbidden_ids:
                finding("forbidden", "", "patch targets a forbidden mutation unit")
        except (KeyError, TypeError, ValueError):
            finding("patch_binding", "", "patch record is missing or malformed")
    violations = {"outside_mutation", "forbidden", "metadata", "file_set"}
    status: Literal["contained", "violated", "unverified"] = "contained"
    if findings:
        status = "violated" if all(f.code in violations for f in findings) else "unverified"
    return RangeAttestation(status, tuple(findings))


def write_containment_manifest(
    workspace_root: Path,
    *,
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    policy: MutationPolicy,
    policy_sha256: str,
    experiment: Experiment,
    genstore: GenerationStore,
) -> RangeAttestation:
    """Publish source evidence after its immutable patch records exist.

    The selected store supplies both source locations and committed bytes;
    a manifest's own locations never establish which generation was used.
    Publication records range findings without changing promotion policy.
    """
    if (experiment.epoch_id, experiment.parent_generation_id, experiment.generation_id) != (
        epoch_id,
        parent_generation_id,
        generation_id,
    ):
        raise ValueError("accepted experiment does not name the selected parent and child")
    layout = WorkspaceLayout.from_root(workspace_root)
    patch_records = {
        patch.id: layout.patch_json(epoch_id, generation_id, patch.id).read_bytes()
        for patch in experiment.patches
    }
    if any(
        json.loads(patch_records[patch.id]) != patch_body(patch) for patch in experiment.patches
    ):
        raise ValueError("recorded patches differ from the accepted patch set")
    parent = genstore.snapshot_path(epoch_id, parent_generation_id)
    child = genstore.snapshot_path(epoch_id, generation_id)
    if policy.root != parent.resolve():
        raise ValueError("captured mutation policy does not belong to the selected parent")
    with tempfile.TemporaryDirectory(prefix="ztw-containment-") as temporary:
        reconstructed = Path(temporary) / "child"
        apply_patches(
            policy.root,
            list(experiment.patches),
            reconstructed,
            enumeration_roots=policy.enumeration_roots,
        )
        if source_files(reconstructed) != source_files(child):
            raise ValueError("selected child differs from authoritative patch reconstruction")
    manifest = build_manifest(
        policy,
        child,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        parent_generation_id=parent_generation_id,
        generation_id=generation_id,
        patches=experiment.patches,
        patch_records=patch_records,
        policy_sha256=policy_sha256,
    )
    _verify_policy(manifest, workspace_root, parent, policy.parent_files)
    for gid, root in ((parent_generation_id, parent), (generation_id, child)):
        observed = {entry.path: entry for entry in source_files(root)}
        listed = {
            entry.path: entry
            for entry in genstore.list_tree(epoch_id, gid, include_bookkeeping=True)
            if not entry.is_dir
        }
        if listed.keys() != observed.keys():
            raise ValueError(
                f"materialized generation {gid!r} has a different committed file inventory"
            )
        if any(
            not entry.is_regular_file
            or observed[path]
            != SourceFile(path, _hash(genstore.read_file(epoch_id, gid, path)), entry.executable)
            for path, entry in listed.items()
        ):
            raise ValueError(f"materialized generation {gid!r} differs from committed source")
    attestation = verify_manifest(
        manifest,
        workspace_root=workspace_root,
        parent_root=parent,
        child_root=child,
        epoch_id=epoch_id,
        parent_generation_id=parent_generation_id,
        generation_id=generation_id,
        patch_records=patch_records,
        patch_ids=[patch.id for patch in experiment.patches],
    )
    atomic_write_json(layout.containment_manifest(epoch_id, generation_id), manifest.to_dict())
    return attestation


def attest_generation(
    workspace_root: Path,
    *,
    epoch_id: str,
    parent_generation_id: str,
    generation_id: str,
    parent_root: Path,
    child_root: Path,
) -> RangeAttestation:
    """Read canonical evidence without deriving trust from its source locations."""
    generation_dir = WorkspaceLayout.from_root(workspace_root).generation_dir(
        epoch_id, generation_id
    )
    try:
        data = (generation_dir / "containment.json").read_bytes()
    except OSError as exc:
        return RangeAttestation(
            "unverified", (ContainmentFinding("manifest_missing", "", str(exc)),)
        )
    try:
        manifest = ContainmentManifest.from_dict(json.loads(data))
    except (TypeError, ValueError) as exc:
        return RangeAttestation("unverified", (ContainmentFinding("manifest_shape", "", str(exc)),))
    try:
        experiment = read_experiment_body(workspace_root, epoch_id, generation_id)
        if experiment is None:
            raise ValueError("experiment record is missing")
        if (
            experiment["epoch_id"],
            experiment["parent_generation_id"],
            experiment["generation_id"],
        ) != (epoch_id, parent_generation_id, generation_id):
            return RangeAttestation(
                "unverified",
                (ContainmentFinding("manifest_coordinates", "", "experiment coordinates differ"),),
            )
        ids = experiment["patch_ids"]
        if not isinstance(ids, list) or not all(
            safe_relative_path(pid) and "/" not in pid for pid in ids
        ):
            raise ValueError("invalid patch identities")
        patches = {}
        for pid in ids:
            try:
                patches[pid] = (generation_dir / "patches" / f"{pid}.json").read_bytes()
            except OSError:
                continue
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return RangeAttestation("unverified", (ContainmentFinding("patch_binding", "", str(exc)),))
    try:
        return verify_manifest(
            manifest,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            parent_generation_id=parent_generation_id,
            generation_id=generation_id,
            parent_root=parent_root,
            child_root=child_root,
            patch_records=patches,
            patch_ids=ids,
        )
    except (OSError, TypeError, ValueError) as exc:
        return RangeAttestation("unverified", (ContainmentFinding("manifest_shape", "", str(exc)),))
