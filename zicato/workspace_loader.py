"""Read workspace-level configuration and per-epoch artifacts.

This module is the single readers' surface for everything that lives
under ``.zicato/`` — the workspace ``config.json``, the current-epoch
marker, the per-epoch ``board.jsonl`` / ``rubric.md`` / ``scoring.json``
artifacts. Callers compose these helpers rather than walking the
directory layout themselves; the layout is owned by
:mod:`zicato.core.workspace`.

The functions deliberately do NOT write — every helper raises a clean
:class:`FileNotFoundError` when an expected artifact is missing. The
error messages suggest the operator-side fix (``zicato init`` /
``zicato epoch new``) so the CLI's error rendering surfaces a useful
next step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zicato.board.jsonl import load_board
from zicato.core.types import BoardEntry, EpochConfig, ScoringWeights
from zicato.core.workspace import board_path, rubric_path, scoring_path
from zicato.epoch.lifecycle import current_epoch_id, load_epoch
from zicato.proposer.rubric import Rubric, load_rubric


def load_workspace_config(workspace_root: Path) -> dict[str, Any]:
    """Read ``{workspace_root}/config.json``.

    The workspace config is shared across every epoch under one
    workspace and carries cross-cutting bookkeeping (adapter
    entrypoint, runtime dotted paths, etc.). The factory modules
    above this loader build the typed views (adapter, runtime config)
    from this raw dict.

    Raises
    ------
    FileNotFoundError
        When ``config.json`` is absent. The message suggests running
        ``zicato init`` to bootstrap a fresh workspace.
    ValueError
        When ``config.json`` is present but unreadable / malformed.
    """
    config_path = workspace_root / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"workspace config not found at {config_path}; "
            f"run `zicato init --workspace {workspace_root}` to bootstrap"
        )
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse {config_path}: {exc.msg}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"{config_path}: expected a JSON object at top level, got "
            f"{type(loaded).__name__}"
        )
    return dict(loaded)


def _resolve_current_epoch(workspace_root: Path) -> str:
    """Resolve the current epoch id or raise with a clear message."""
    eid = current_epoch_id(workspace_root)
    if eid is None:
        raise FileNotFoundError(
            f"no current_epoch marker under {workspace_root}; "
            "run `zicato epoch new <name> ...` to create one"
        )
    return eid


def load_current_epoch_config(workspace_root: Path) -> EpochConfig:
    """Load the current epoch's :class:`EpochConfig`.

    Reads the ``current_epoch`` marker file, then defers to
    :func:`zicato.epoch.lifecycle.load_epoch` for the typed view of
    the per-epoch ``config.json``.
    """
    eid = _resolve_current_epoch(workspace_root)
    return load_epoch(workspace_root, eid)


def load_current_board(workspace_root: Path) -> list[BoardEntry]:
    """Load the current epoch's ``board.jsonl`` into a list of entries."""
    eid = _resolve_current_epoch(workspace_root)
    path = board_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(
            f"board not found at {path}; the current epoch is incomplete"
        )
    return load_board(path)


def load_current_scoring(workspace_root: Path) -> ScoringWeights:
    """Load the current epoch's frozen :class:`ScoringWeights`.

    Reads ``scoring.json`` directly rather than going through
    :class:`EpochConfig` so the helper is usable from contexts that
    only want weights (the runtime factory, tournament setup).
    """
    eid = _resolve_current_epoch(workspace_root)
    path = scoring_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(
            f"scoring.json not found at {path}; the current epoch is incomplete"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _scoring_weights_from_dict(raw)


def load_current_rubric(workspace_root: Path) -> Rubric:
    """Load the current epoch's parsed :class:`Rubric`."""
    eid = _resolve_current_epoch(workspace_root)
    path = rubric_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(
            f"rubric.md not found at {path}; the current epoch is incomplete"
        )
    return load_rubric(path)


def _scoring_weights_from_dict(d: Mapping[str, Any]) -> ScoringWeights:
    """Build a :class:`ScoringWeights` from a JSON-shaped dict.

    Mirrors :func:`zicato.epoch.lifecycle._scoring_from_dict` so the
    on-disk format is fully shared. Local copy lives here because
    importing the private helper across modules would couple this
    loader to a lifecycle implementation detail.
    """
    raw_sev = d.get("severity_weights")
    severity_kwarg: dict[str, Any] = {}
    if raw_sev:
        severity_kwarg["severity_weights"] = {
            str(k): float(v) for k, v in raw_sev.items()
        }
    return ScoringWeights(
        drift_weight=float(d.get("drift_weight", 1.0)),
        pass_weight=float(d.get("pass_weight", 1.0)),
        per_kind_weights={
            str(k): float(v) for k, v in d.get("per_kind_weights", {}).items()
        },
        plan_revision_weight=float(d.get("plan_revision_weight", 0.5)),
        runtime_weight=float(d.get("runtime_weight", 0.0)),
        promote_margin=float(d.get("promote_margin", 0.01)),
        pass_rate_monotonicity=bool(d.get("pass_rate_monotonicity", True)),
        **severity_kwarg,
    )


__all__ = [
    "load_workspace_config",
    "load_current_epoch_config",
    "load_current_board",
    "load_current_scoring",
    "load_current_rubric",
]
