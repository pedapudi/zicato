"""Read workspace-level configuration and per-epoch artifacts.

This module is the single readers' surface for everything that lives
under ``.zicato/`` — the workspace ``config.json``, the current-epoch
marker, the per-epoch ``board.jsonl`` / ``brief.md`` / ``scoring.json``
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

from zicato.board.jsonl import load_board, load_board_with_meta
from zicato.core.types import (
    BoardEntry,
    EpochConfig,
    OverfittingConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.core.workspace import board_path, epoch_dir, scoring_path
from zicato.epoch.lifecycle import current_epoch_id, load_epoch
from zicato.proposer.brief import ProposerBrief, load_brief


def _epoch_brief_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the frozen proposer brief (``brief.md``) for one epoch.

    Epochs created before the proposer-brief rename stored the file as
    ``rubric.md``; when no ``brief.md`` exists the legacy name is used so
    those epochs keep loading.
    """
    brief = epoch_dir(workspace_root, epoch_id) / "brief.md"
    if not brief.exists():
        legacy = epoch_dir(workspace_root, epoch_id) / "rubric.md"
        if legacy.exists():
            return legacy
    return brief


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
            f"{config_path}: expected a JSON object at top level, got {type(loaded).__name__}"
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
        raise FileNotFoundError(f"board not found at {path}; the current epoch is incomplete")
    return load_board(path)


def load_current_board_with_meta(
    workspace_root: Path,
) -> tuple[list[BoardEntry], tuple[Any, ...], bool]:
    """Load the current epoch's board plus its board-level metadata.

    Like :func:`load_current_board` but also returns the board-level
    ``disable_drift`` tuple and the ``judge_only`` flag parsed from the
    board's ``board_meta`` header (empty / ``False`` when the board has
    no header). The tournament runner needs the suppression set to
    thread it onto each board entry, and the judge-only flag to select
    no-steering evaluation, so this is the loader the orchestrator and
    the ``zicato tournament`` command use.
    """
    eid = _resolve_current_epoch(workspace_root)
    path = board_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(f"board not found at {path}; the current epoch is incomplete")
    return load_board_with_meta(path)


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


def load_current_tournament(workspace_root: Path) -> TournamentStructure:
    """Load the current epoch's frozen :class:`TournamentStructure`.

    Reads the ``tournament`` block out of the epoch's frozen
    ``scoring.json`` (where the structure lives — see the data-model
    design). Defaults to the gauntlet for epochs that predate the field
    or never set it, so the loader is safe against old workspaces.
    """
    eid = _resolve_current_epoch(workspace_root)
    path = scoring_path(workspace_root, eid)
    if not path.exists():
        return TournamentStructure.gauntlet()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tournament_structure_from_dict(raw.get("tournament"))


def load_current_brief(workspace_root: Path) -> ProposerBrief:
    """Load the current epoch's parsed :class:`ProposerBrief`."""
    eid = _resolve_current_epoch(workspace_root)
    path = _epoch_brief_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(f"brief.md not found at {path}; the current epoch is incomplete")
    return load_brief(path)


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
        severity_kwarg["severity_weights"] = {str(k): float(v) for k, v in raw_sev.items()}
    judge_kwargs: dict[str, Any] = {}
    raw_per_judge = d.get("per_judge_weights")
    if isinstance(raw_per_judge, dict) and raw_per_judge:
        # Per-judge weighting is optional — only forward the kwarg when
        # the on-disk scoring.json carries a non-empty mapping so a
        # legacy file with no per_judge surface still loads at the
        # dataclass default.
        judge_kwargs["per_judge_weights"] = {str(k): float(v) for k, v in raw_per_judge.items()}
    if "default_judge_weight" in d:
        judge_kwargs["default_judge_weight"] = float(d["default_judge_weight"])
    return ScoringWeights(
        drift_weight=float(d.get("drift_weight", 1.0)),
        pass_weight=float(d.get("pass_weight", 1.0)),
        per_kind_weights={str(k): float(v) for k, v in d.get("per_kind_weights", {}).items()},
        plan_revision_weight=float(d.get("plan_revision_weight", 0.5)),
        runtime_weight=float(d.get("runtime_weight", 0.0)),
        promote_margin=float(d.get("promote_margin", 0.01)),
        pass_rate_monotonicity=bool(d.get("pass_rate_monotonicity", True)),
        tournament_structure=tournament_structure_from_dict(d.get("tournament")),
        overfitting=overfitting_config_from_dict(d.get("overfitting")),
        **severity_kwarg,
        **judge_kwargs,
    )


def overfitting_config_from_dict(raw: Any) -> OverfittingConfig:
    """Parse the ``overfitting`` block of a ``scoring.json`` into a config.

    Absent (``None``) or a non-mapping ⇒ the fully-defaulted (default-on)
    :class:`~zicato.core.types.OverfittingConfig` — so every epoch on disk
    today, and every operator who never touches the knob, gets the
    anti-overfitting machine on, with a safe auto-degrade on small boards.

    A present block forwards each recognised key; unknown keys are
    ignored. Range/validity is enforced by ``OverfittingConfig``'s
    ``__post_init__``. Shared by :func:`_scoring_weights_from_dict` (used
    by the contract canonicalizer) and the lifecycle serializer so the
    on-disk format is fully shared between the two loaders.
    """
    if not isinstance(raw, Mapping):
        return OverfittingConfig.defaults()
    kwargs: dict[str, Any] = {}
    if "enabled" in raw:
        kwargs["enabled"] = bool(raw["enabled"])
    if "holdout_fraction" in raw:
        kwargs["holdout_fraction"] = float(raw["holdout_fraction"])
    if "min_board_size_for_split" in raw:
        kwargs["min_board_size_for_split"] = int(raw["min_board_size_for_split"])
    if "restrict_proposer_visibility" in raw:
        kwargs["restrict_proposer_visibility"] = bool(raw["restrict_proposer_visibility"])
    return OverfittingConfig(**kwargs)


def overfitting_config_to_dict(cfg: OverfittingConfig) -> dict[str, Any]:
    """Serialize an :class:`OverfittingConfig` to the ``overfitting`` block.

    The inverse of :func:`overfitting_config_from_dict`; every field is
    written so the on-disk form is complete and round-trips cleanly.
    """
    return {
        "enabled": cfg.enabled,
        "holdout_fraction": cfg.holdout_fraction,
        "min_board_size_for_split": cfg.min_board_size_for_split,
        "restrict_proposer_visibility": cfg.restrict_proposer_visibility,
    }


def tournament_structure_from_dict(raw: Any) -> TournamentStructure:
    """Parse the ``tournament`` block of a ``scoring.json`` into a spec.

    Absent (``None``) or a non-mapping ⇒ the fully-defaulted gauntlet
    spec — the back-compat contract: every epoch on disk today, and every
    operator who never touches the knob, gets the gauntlet.

    A present block must carry a valid ``structure`` token (validated by
    :class:`~zicato.core.types.TournamentStructure`'s ``__post_init__``,
    which lists the valid tokens on error) and an optional ``params``
    object. ``params`` is stored verbatim as an opaque mapping; per-key
    semantics are the selection strategy's responsibility.

    Shared by :func:`_scoring_weights_from_dict` (used by the contract
    canonicalizer) and the lifecycle serializer so the on-disk format is
    fully shared between the two loaders.
    """
    if not isinstance(raw, Mapping):
        return TournamentStructure.gauntlet()
    structure = str(raw.get("structure", "gauntlet"))
    raw_params = raw.get("params", {})
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, Mapping) else {}
    return TournamentStructure(structure=structure, params=params)


def tournament_structure_to_dict(spec: TournamentStructure) -> dict[str, Any]:
    """Serialize a :class:`TournamentStructure` to the ``tournament`` block.

    The inverse of :func:`tournament_structure_from_dict`; ``params`` is
    written verbatim as a plain dict.
    """
    return {"structure": spec.structure, "params": dict(spec.params)}


__all__ = [
    "load_workspace_config",
    "load_current_epoch_config",
    "load_current_board",
    "load_current_board_with_meta",
    "load_current_scoring",
    "load_current_tournament",
    "load_current_brief",
    "tournament_structure_from_dict",
    "tournament_structure_to_dict",
    "overfitting_config_from_dict",
    "overfitting_config_to_dict",
]
