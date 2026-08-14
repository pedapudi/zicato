"""Deterministic capture of files produced in a tournament run scratch tree."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from zicato.core.lineage import ArtifactFile, ArtifactSet

ARTIFACT_FORMAT_VERSION = 1
MAX_ARTIFACT_FILES = 1_000
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MIME_TYPES = mimetypes.MimeTypes(filenames=())


def artifact_paths(loss_path: Path) -> tuple[Path, Path]:
    """Return the replicate-keyed ``(tree, manifest)`` paths for a loss slot."""
    name = loss_path.name
    slot = "" if name == "loss.json" else name[len("loss") : -len(".json")]
    return (
        loss_path.with_name(f"artifacts{slot}"),
        loss_path.with_name(f"artifacts{slot}.json"),
    )


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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def capture_run_artifacts(
    scratch_root: Path,
    loss_path: Path,
    *,
    max_files: int = MAX_ARTIFACT_FILES,
    max_total_bytes: int = MAX_ARTIFACT_BYTES,
) -> ArtifactSet:
    """Persist and inventory regular files found beneath ``scratch_root``.

    Discovery is output-driven: callers do not declare filenames or extensions.
    Entries are considered in sorted relative-path order. Symlinks, special files,
    unreadable files, and files beyond the capture bounds are recorded as skipped
    and never followed.
    """
    artifact_root, manifest_path = artifact_paths(loss_path)
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

        shutil.rmtree(artifact_root, ignore_errors=True)
        os.replace(staging, artifact_root)
        payload = {
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
    "capture_run_artifacts",
]
