"""Deterministic capture of files produced in a tournament run scratch tree."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from zicato.core.lineage import ArtifactFile, ArtifactSet
from zicato.core.loss import LossProfile, capture_matches_loss
from zicato.core.measurement import (
    MeasurementDraw,
    artifact_measurement,
    recorded_measurement,
    unit_artifact_name,
)
from zicato.epoch._storage import RecordFormatError, check_record_format
from zicato.storage import atomic_write_text

ARTIFACT_FORMAT_VERSION = 1
MAX_ARTIFACT_FILES = 1_000
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MIME_TYPES = mimetypes.MimeTypes(filenames=())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_unit_artifacts(loss_path: Path) -> Path | None:
    """Publish a complete attempt archive before clearing any reusable artifact.

    Publication is one directory rename after every copied file is flushed.
    A failed copy leaves all originals intact. After publication the loss is
    removed first, so interruption while clearing companions leaves a cache
    miss and the complete prior attempt remains recoverable from the archive.
    The caller serializes writers for the same seed and draw.
    """
    index = artifact_measurement(loss_path.name)
    if index is None:
        raise ValueError("attempt archive requires a measurement loss path")
    sources = [
        loss_path.with_name(unit_artifact_name(kind, index))
        for kind in ("loss", "events", "result", "judge_io")
    ]
    sources.extend(artifact_paths(loss_path))
    sources = [source for source in sources if source.exists()]
    if not sources:
        return None
    archive_root = loss_path.parent / "attempts"
    archive_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=archive_root))
    try:
        for source in sources:
            destination = staging / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copyfile(source, destination)
        digest = hashlib.sha256()
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                with path.open("rb") as handle:
                    body = handle.read()
                    os.fsync(handle.fileno())
                relative = path.relative_to(staging).as_posix().encode()
                digest.update(len(relative).to_bytes(8, "big") + relative)
                digest.update(len(body).to_bytes(8, "big") + body)
        for directory in [
            *sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True),
            staging,
        ]:
            _fsync_directory(directory)
        archive = archive_root / f"{loss_path.stem}-{digest.hexdigest()}"
        if archive.exists():
            shutil.rmtree(staging)
        else:
            os.rename(staging, archive)
        _fsync_directory(archive_root)
        _fsync_directory(loss_path.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Keep the existing loss-history reader supplied with the same raw profile.
    from zicato.tournament.unit_cache import archive_outgoing_unit_loss  # noqa: PLC0415

    archive_outgoing_unit_loss(loss_path)
    for source in sources:
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
    _fsync_directory(loss_path.parent)
    return archive


def artifact_paths(loss_path: Path) -> tuple[Path, Path]:
    """Return the replicate-keyed ``(tree, manifest)`` paths for a loss slot."""
    index = artifact_measurement(loss_path.name)
    if index is None:
        raise ValueError("artifact capture requires a measurement loss path")
    manifest = loss_path.with_name(unit_artifact_name("artifacts", index))
    return manifest.with_suffix(""), manifest


def _relative_artifact_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("artifact paths must be normalized relative paths")
    return value


def artifact_manifest_from_payload(payload: object) -> dict[str, Any]:
    """Validate the complete manifest shape while retaining extension fields.

    Copied-file access and paired measurement identity belong to the reader.
    This codec neither changes copied bytes nor fills missing provenance.
    """
    if not isinstance(payload, dict):
        raise ValueError("artifact manifest must be an object")
    try:
        check_record_format(
            payload,
            "artifact manifest",
            expected_version=ARTIFACT_FORMAT_VERSION,
        )
    except RecordFormatError as exc:
        raise ValueError(str(exc)) from exc
    files, skipped = payload.get("files"), payload.get("skipped")
    if not isinstance(files, list) or not isinstance(skipped, list):
        raise ValueError("artifact files and skipped entries must be lists")
    paths: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("artifact file must be an object")
        path = _relative_artifact_path(item.get("path"))
        size, sha256, media_type = item.get("size"), item.get("sha256"), item.get("media_type")
        if (
            path in paths
            or type(size) is not int
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or not isinstance(media_type, str)
            or not media_type
        ):
            raise ValueError("invalid or repeated artifact file metadata")
        paths.add(path)
        total += size
    limited = False
    for item in skipped:
        if not isinstance(item, dict):
            raise ValueError("skipped artifact must be an object")
        path = _relative_artifact_path(item.get("path"))
        reason = item.get("reason")
        if path in paths or not isinstance(reason, str) or not reason:
            raise ValueError("invalid or repeated skipped artifact")
        paths.add(path)
        limited |= reason == "capture_limit"
    if type(payload.get("total_bytes")) is not int or payload["total_bytes"] != total:
        raise ValueError("artifact total differs from its file inventory")
    if type(payload.get("truncated")) is not bool or payload["truncated"] != limited:
        raise ValueError("artifact truncation differs from its skipped inventory")
    if "measurement" in payload:
        MeasurementDraw.from_json(payload["measurement"])
        if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
            raise ValueError("artifact measurement requires its run identity")
    elif "run_id" in payload:
        raise ValueError("artifact run identity requires its measurement")
    return deepcopy(payload)


def read_artifact_manifest(
    loss_path: Path, *, expected: LossProfile | None = None
) -> dict[str, Any] | None:
    """Read one exact companion; absence returns None and defects raise ValueError.

    Unpaired historical inventories remain available for audit. A paired loss
    with a known seed requires matching measurement and run provenance. Invalid
    records and copied files remain untouched for inspection.
    """
    artifact_root, manifest_path = artifact_paths(loss_path)
    try:
        metadata = manifest_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"unreadable artifact manifest: {exc}") from exc
    try:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("artifact manifest must be a regular file")
        body = artifact_manifest_from_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
        if not capture_matches_loss(body, expected):
            raise ValueError("artifact provenance differs from the paired loss")
        if "measurement" in body:
            index = artifact_measurement(loss_path.name)
            assert index is not None  # artifact_paths validated the physical slot.
            recorded_measurement(
                index,
                measurement=MeasurementDraw.from_json(body["measurement"]),
            )
        if not stat.S_ISDIR(artifact_root.lstat().st_mode):
            raise ValueError("artifact root must be a directory without a link")
        root = artifact_root.resolve(strict=True)
        for item in body["files"]:
            path = root / item["path"]
            if path.resolve(strict=True) != path or not path.is_relative_to(root):
                raise ValueError("copied artifact path resolves outside its recorded location")
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != item["size"]:
                raise ValueError("copied artifact differs from its recorded file metadata")
        return body
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ValueError(f"unreadable artifact manifest: {exc}") from exc


def _media_type(path: str) -> str:
    guessed, _ = _MIME_TYPES.guess_type(path, strict=False)
    return guessed or "application/octet-stream"


def _copy_regular_file(source: Path, destination: Path) -> tuple[int, str]:
    """Copy without following a final-component symlink; return size and digest."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError("not a regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fd, "rb", closefd=False) as src, destination.open("wb") as dst:
            while chunk := src.read(_COPY_CHUNK_BYTES):
                dst.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    finally:
        os.close(fd)
    return size, digest.hexdigest()


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", mode=0o600
    )


def capture_run_artifacts(
    scratch_root: Path,
    loss_path: Path,
    *,
    max_files: int = MAX_ARTIFACT_FILES,
    max_total_bytes: int = MAX_ARTIFACT_BYTES,
    measurement: MeasurementDraw | None = None,
    run_id: str | None = None,
) -> ArtifactSet:
    """Persist and inventory regular files found beneath ``scratch_root``.

    Discovery is output-driven: callers do not declare filenames or extensions.
    Entries are considered in sorted relative-path order. Symlinks, special files,
    unreadable files, and files beyond the capture bounds are recorded as skipped
    and never followed.
    """
    artifact_root, manifest_path = artifact_paths(loss_path)
    if measurement is not None:
        index = artifact_measurement(loss_path.name)
        assert index is not None  # artifact_paths validated the physical slot.
        recorded_measurement(index, measurement=measurement)
    loss_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{artifact_root.name}.", dir=loss_path.parent))
    files: list[ArtifactFile] = []
    skipped: list[dict[str, str]] = []
    total_bytes = 0
    truncated = False

    try:
        candidates = sorted(
            scratch_root.rglob("*"), key=lambda path: path.relative_to(scratch_root).as_posix()
        )
        for source in candidates:
            relative = source.relative_to(scratch_root).as_posix()
            try:
                metadata = source.lstat()
            except OSError:
                skipped.append({"path": relative, "reason": "unreadable"})
                continue
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skipped.append({"path": relative, "reason": "unsupported_file_type"})
                continue
            if len(files) >= max_files or metadata.st_size > max_total_bytes - total_bytes:
                skipped.append({"path": relative, "reason": "capture_limit"})
                truncated = True
                continue
            try:
                size, sha256 = _copy_regular_file(source, staging / relative)
            except OSError:
                skipped.append({"path": relative, "reason": "unreadable"})
                continue
            if size > max_total_bytes - total_bytes:
                (staging / relative).unlink(missing_ok=True)
                skipped.append({"path": relative, "reason": "capture_limit"})
                truncated = True
                continue
            files.append(
                ArtifactFile(
                    path=relative,
                    size=size,
                    sha256=sha256,
                    media_type=_media_type(relative),
                )
            )
            total_bytes += size

        payload: dict[str, Any] = {
            "format_version": ARTIFACT_FORMAT_VERSION,
            "files": [
                {
                    "path": item.path,
                    "size": item.size,
                    "sha256": item.sha256,
                    "media_type": item.media_type,
                }
                for item in files
            ],
            "skipped": skipped,
            "total_bytes": total_bytes,
            "truncated": truncated,
        }
        if measurement is not None:
            payload["measurement"] = measurement.to_json()
        if run_id is not None:
            payload["run_id"] = run_id
        payload = artifact_manifest_from_payload(payload)
        shutil.rmtree(artifact_root, ignore_errors=True)
        os.replace(staging, artifact_root)
        _write_manifest(manifest_path, payload)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return ArtifactSet(
        root=artifact_root,
        manifest_path=manifest_path,
        files=tuple(files),
        total_bytes=total_bytes,
        truncated=truncated,
    )


__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_FILES",
    "artifact_paths",
    "artifact_manifest_from_payload",
    "capture_run_artifacts",
    "read_artifact_manifest",
]
