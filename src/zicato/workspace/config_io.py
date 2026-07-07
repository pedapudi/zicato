"""Raw JSON I/O for the workspace root's ``config.json``.

The workspace config is shared across every epoch under one workspace
and carries cross-cutting bookkeeping (adapter entrypoint, runtime
dotted paths, model blocks, etc.). This module owns the *raw dict*
seam for that file — tolerant read, atomic write, initialized check —
so the CLI, the builder, and any other consumer share one
implementation. The typed, error-raising read lives one level up in
:func:`zicato.workspace_loader.load_workspace_config`; path-math for
*inside* the workspace lives in :mod:`zicato.core.workspace`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"
LINEAGE_FILENAME = "lineage.json"


def _config_path(workspace_root: Path) -> Path:
    return workspace_root / CONFIG_FILENAME


def write_workspace_config(workspace_root: Path, config: dict[str, Any]) -> None:
    """Atomically write ``config.json`` under ``workspace_root``.

    The workspace directory must already exist. Writes through a
    temp-and-rename so a partial write never leaves the file truncated.
    """
    if not workspace_root.exists():
        raise FileNotFoundError(
            f"workspace {workspace_root!s} does not exist; run `zicato init` first"
        )
    target = _config_path(workspace_root)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)


def read_workspace_config(workspace_root: Path) -> dict[str, Any]:
    """Read ``config.json`` from ``workspace_root`` and return it.

    Returns an empty dict if the file is missing — callers that *require*
    an initialized workspace should check existence explicitly via
    :func:`workspace_is_initialized`.
    """
    target = _config_path(workspace_root)
    if not target.exists():
        return {}
    result: dict[str, Any] = json.loads(target.read_text())
    return result


def workspace_is_initialized(workspace_root: Path) -> bool:
    """Return True iff ``workspace_root/config.json`` exists."""
    return _config_path(workspace_root).exists()


__all__ = [
    "CONFIG_FILENAME",
    "LINEAGE_FILENAME",
    "read_workspace_config",
    "workspace_is_initialized",
    "write_workspace_config",
]
