"""Shared utilities for zicato CLI subcommands.

Provides:

* :func:`shared_options` — decorator that attaches the three flags every
  subcommand wants (``--workspace``, ``--verbose``, ``--instance-id``).
* :func:`get_workspace_root` — coerces an argument or a click context
  into a workspace :class:`Path`.
* :func:`read_workspace_config` / :func:`write_workspace_config` — JSON
  I/O for ``{workspace}/config.json``.

Path-math for *inside* the workspace lives in
:mod:`zicato.core.workspace`; the helpers here only deal with the
workspace root itself and its top-level ``config.json``.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

CONFIG_FILENAME = "config.json"
LINEAGE_FILENAME = "lineage.json"


def shared_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that adds the three universal CLI flags to a command.

    Adds (in this order so the resulting parameter list reads naturally
    on ``--help``):

    * ``--workspace`` (default ``.zicato``)
    * ``--instance-id`` (default ``"default"``)
    * ``--verbose`` / ``-v`` (flag, off by default)

    Each subcommand receives these as keyword arguments named
    ``workspace``, ``instance_id``, ``verbose``.
    """

    @click.option(
        "--verbose",
        "-v",
        is_flag=True,
        default=False,
        help="Emit more diagnostic output to stderr.",
    )
    @click.option(
        "--instance-id",
        "instance_id",
        default="default",
        show_default=True,
        help="Logical instance identifier.",
    )
    @click.option(
        "--workspace",
        default=".zicato",
        show_default=True,
        type=click.Path(file_okay=False, dir_okay=True),
        help="Path to the .zicato/ workspace.",
    )
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    return wrapper


def get_workspace_root(ctx_or_path: Any) -> Path:
    """Coerce a click context, string, or :class:`Path` to a workspace
    :class:`Path`.

    Accepts:

    * a :class:`click.Context` whose ``params`` contains ``workspace``;
    * a :class:`str` or :class:`os.PathLike` path;
    * an already-resolved :class:`Path`.
    """
    if isinstance(ctx_or_path, click.Context):
        ws = ctx_or_path.params.get("workspace")
        if ws is None:
            # fall back to default if a parent group set nothing
            ws = ".zicato"
        return Path(ws)
    if isinstance(ctx_or_path, Path):
        return ctx_or_path
    return Path(str(ctx_or_path))


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
    "shared_options",
    "get_workspace_root",
    "read_workspace_config",
    "write_workspace_config",
    "workspace_is_initialized",
]
