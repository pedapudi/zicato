"""Validate, prepare, and identify the source content of a baseline seed."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable
from pathlib import Path

from zicato.epoch.snapshot_scope import copytree_ignore

GIT_ADMIN_BASENAMES = frozenset({".git", ".gitignore"})


def validated_seed_sources(
    sources: Iterable[Path], *, excluded_names: frozenset[str] = frozenset()
) -> list[Path]:
    """Resolve every source and reject missing paths or colliding basenames."""
    resolved: list[Path] = []
    names: set[str] = set()
    for raw in sources:
        source = Path(raw).resolve()
        if source.name in excluded_names:
            continue
        if not source.exists():
            raise FileNotFoundError(f"seed_generation: source tree {source} does not exist on disk")
        if not source.is_file() and not source.is_dir():
            raise ValueError(f"seed_generation: unsupported source type: {source}")
        if source.name in names:
            raise ValueError(f"seed_generation: duplicate source basename {source.name!r}")
        names.add(source.name)
        resolved.append(source)
    return resolved


def prepare_seed_sources(sources: Iterable[Path], destination: Path) -> None:
    """Copy validated sources into an owned directory for later synchronization."""
    destination.mkdir(parents=True, exist_ok=False)
    for source in sources:
        target = destination / source.name
        if source.is_file():
            shutil.copy2(source, target)
        else:
            shutil.copytree(source, target, ignore=copytree_ignore())


def seed_content_identity(root: Path, *, excluded_names: frozenset[str] = frozenset()) -> str:
    """Hash relative paths, file bytes, and executability in a prepared tree."""
    if not root.is_dir():
        raise FileNotFoundError(f"prepared baseline source is missing: {root}")
    records: list[tuple[str, str, bool, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in excluded_names:
            continue
        if path.is_symlink():
            raise ValueError(f"prepared baseline source contains a symlink: {path}")
        if path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            records.append((relative.as_posix(), "file", bool(path.stat().st_mode & 0o111), digest))
        elif not path.is_dir():
            raise ValueError(f"prepared baseline source has unsupported content: {path}")
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()
