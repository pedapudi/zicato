"""The persisted state of an editable contract publication.

The contract draft owns publication and recovery. Live readers use the revision
to reject a partial publication or a publication crossing their read operation.
Completed records retain only the revision; authored files remain canonical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID


def contract_publication_path(workspace_root: Path) -> Path:
    return workspace_root / "contract-publication.json"


def read_contract_publication(workspace_root: Path) -> dict[str, Any] | None:
    """Read a publication record, refusing malformed revision metadata."""
    path = contract_publication_path(workspace_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return decode_contract_publication(text, path)


def decode_contract_publication(text: str, path: Path) -> dict[str, Any]:
    """Decode publication metadata from its canonical or retained content."""
    record = json.loads(text)
    if not isinstance(record, dict) or record.get("version") != 1:
        raise ValueError(f"{path}: invalid contract publication record")
    try:
        UUID(record["revision"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{path}: invalid contract publication revision") from exc
    if record.get("state") not in {"pending", "complete"}:
        raise ValueError(f"{path}: invalid contract publication state")
    return record


def assert_contract_publication_complete(workspace_root: Path) -> str | None:
    """Return the completed revision, or refuse partially published live inputs."""
    record = read_contract_publication(workspace_root)
    if record is None:
        return None
    if record["state"] != "complete":
        raise ValueError(
            "editable contract publication is pending; resume publication under the "
            "workspace writer before reading live contract inputs"
        )
    return str(record["revision"])


def require_contract_revision(workspace_root: Path, revision: str | None) -> None:
    """Refuse a live read that crosses a completed contract publication."""
    if assert_contract_publication_complete(workspace_root) != revision:
        raise ValueError("editable contract changed during reading; reload the live contract")
