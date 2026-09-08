"""Read workspace-level configuration and per-epoch artifacts.

This module is the readers' surface for the per-epoch artifacts under
``.zicato/`` — the current-epoch marker and that epoch's
``board.jsonl`` / ``brief.md`` / ``scoring.json`` — plus the
raw-mapping view of the workspace ``config.json``. Callers compose
these helpers rather than walking the directory layout themselves; the
layout is owned by :mod:`zicato.core.workspace`, and the workspace
config file itself — where it is, how it parses, what shape it has — by
:mod:`zicato.workspace.config_io`.

The functions do NOT write — every helper raises a clean
:class:`FileNotFoundError` when an expected artifact is missing. The
error messages suggest the operator-side fix (``zicato init`` /
``zicato epoch new``) so the CLI's error rendering surfaces a useful
next step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.board.jsonl import load_board, load_board_with_meta
from zicato.core.scoring_config import scoring_weights_from_dict as scoring_weights_from_dict
from zicato.core.types import (
    BoardEntry,
    EpochConfig,
    LadderConfig,
    OverfittingConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.core.workspace import board_path, brief_path, scoring_path
from zicato.epoch.lifecycle import current_epoch_id, load_epoch
from zicato.proposer.brief import ProposerBrief, load_brief
from zicato.workspace.config_io import read_workspace_config


def load_workspace_config(workspace_root: Path) -> dict[str, Any]:
    """Read ``{workspace_root}/config.json`` as a plain dict.

    The raw-mapping view of the workspace config, for the callers that
    genuinely want the whole mapping: the factory modules above this
    loader (adapter, runtime config, model roles) each read several keys
    and validate them their own way, so they take the parsed object
    rather than a projection of it.

    Parsing, the file's location, and the one malformed-file error belong
    to :func:`zicato.workspace.config_io.read_workspace_config`; this
    adds the policy that an absent file is an error naming ``zicato
    init``. Callers wanting a typed field, or wanting an absent file to
    read as defaults, call that loader directly.

    Raises
    ------
    FileNotFoundError
        When ``config.json`` is absent. The message suggests running
        ``zicato init`` to bootstrap a fresh workspace.
    ValueError
        When ``config.json`` is present but unreadable / malformed.
    """
    return dict(read_workspace_config(workspace_root).require().raw)


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
    return scoring_weights_from_dict(raw)


def load_current_tournament(workspace_root: Path) -> TournamentStructure:
    """Read the tournament specification from the epoch's scoring settings."""
    return load_current_scoring(workspace_root).tournament_structure


def load_current_brief(workspace_root: Path) -> ProposerBrief:
    """Load the current epoch's parsed :class:`ProposerBrief`."""
    eid = _resolve_current_epoch(workspace_root)
    path = brief_path(workspace_root, eid)
    if not path.exists():
        raise FileNotFoundError(f"brief.md not found at {path}; the current epoch is incomplete")
    return load_brief(path)


def overfitting_config_from_dict(raw: Any) -> OverfittingConfig:
    """Validate the supplied settings using their field declarations."""
    from zicato.core.configuration import authored_dataclass_from_json  # noqa: PLC0415

    return authored_dataclass_from_json(
        OverfittingConfig, {} if raw is None else raw, path="scoring.overfitting"
    )


def ladder_config_from_dict(raw: Any) -> LadderConfig:
    """Validate the supplied settings using their field declarations."""
    from zicato.core.configuration import authored_dataclass_from_json  # noqa: PLC0415

    return authored_dataclass_from_json(
        LadderConfig, {} if raw is None else raw, path="scoring.overfitting.ladder"
    )


def overfitting_config_to_dict(cfg: OverfittingConfig) -> dict[str, Any]:
    """Serialize an :class:`OverfittingConfig` to the ``overfitting`` block.

    The inverse of :func:`overfitting_config_from_dict`; field-enumerating
    (and recursive over the nested ``ladder``) via
    :func:`zicato.core.configuration.dataclass_to_jsonable`, so every
    field is written and a newly-added field is covered automatically
    (issue #13) — the on-disk form is complete and round-trips cleanly.
    """
    from zicato.core.configuration import dataclass_to_jsonable  # noqa: PLC0415

    return dataclass_to_jsonable(cfg)


def tournament_structure_from_dict(raw: Any) -> TournamentStructure:
    """Validate the supplied settings using their field declarations."""
    from zicato.core.configuration import authored_dataclass_from_json  # noqa: PLC0415

    return authored_dataclass_from_json(
        TournamentStructure, {} if raw is None else raw, path="scoring.tournament"
    )


def activate_mutation_surface(workspace_root: Path) -> tuple[str, ...]:
    """Install the current epoch's declared mutation-surface table for this process.

    The contract's ``mutation_surface`` table decides which file types are
    enumerable (MUTATION-SURFACE.md §2.5), and enumeration re-runs deep in
    the apply loop from call sites that hold nothing but paths — so the
    declared table is installed once per invocation, here, rather than
    threaded through every walk. Propose-time and apply-time enumeration
    then cannot disagree about what is surface.

    Returns the operator-declared suffixes (empty for a workspace that
    declares none, which is every workspace that predates the table). A
    workspace with no readable epoch scoring keeps the built-ins — the
    read-only viewers call this against trees that may not have one.
    """
    from zicato.mutation.markers import install_syntax_table  # noqa: PLC0415

    try:
        weights = load_current_scoring(workspace_root)
    except (OSError, ValueError):
        return install_syntax_table(None)
    return install_syntax_table(weights.mutation_surface)


def tournament_structure_to_dict(spec: TournamentStructure) -> dict[str, Any]:
    """Serialize a :class:`TournamentStructure` to the ``tournament`` block.

    The inverse of :func:`tournament_structure_from_dict`; ``params`` is
    written verbatim as a plain dict.
    """
    return {"structure": spec.structure, "params": dict(spec.params)}


__all__ = [
    "activate_mutation_surface",
    "load_workspace_config",
    "scoring_weights_from_dict",
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
    "ladder_config_from_dict",
]
