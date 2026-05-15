"""Epoch lifecycle: new / close / list / switch / load.

An epoch is the unit of evaluation contract: a frozen board, a frozen
rubric, and a frozen scoring configuration. The functions in this module
are the only supported way to create, close, enumerate, and switch
between epochs on disk.

Storage layout managed here::

    {workspace_root}/
      current_epoch                # marker file, single line = epoch id
      lineage.json                 # cross-cutting DAG (see lineage.py)
      epochs/
        {epoch_id}/
          board.jsonl              # copy of board_source
          rubric.md                # copy of rubric_source
          scoring.json             # serialized ScoringWeights
          config.json              # EpochConfig serialized (id/name/created_at/closed/closed_at)
          journal.md               # appended per experiment (see journal.py)
          analysis.md              # written at close (see analysis.py)

Epoch ids are formed as ``{YYYY-MM-DD}_{short_name}`` where ``short_name``
is a filesystem-safe slug of the operator-supplied name. If the same
name is created twice on the same day the second call gets a numeric
suffix.

The module is deliberately small and procedural — there is no
``Lifecycle`` class. Functions take ``workspace_root`` explicitly so the
CLI and tests construct calls from explicit Paths without holding on to
shared state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import shutil
import sys
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from zicato.core.types import EpochConfig, ScoringWeights
from zicato.core.workspace import (
    analysis_path,
    board_path,
    epoch_dir,
    journal_path,
    rubric_path,
    scoring_path,
)

# A callable shape compatible with goldfive's call_llm:
# (system, user, model) -> awaitable[str].
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Path helpers (epoch-local; the cross-cutting ones live in workspace.py)
# ---------------------------------------------------------------------------


def _config_path(workspace_root: Path, epoch_id: str) -> Path:
    return epoch_dir(workspace_root, epoch_id) / "config.json"


def _current_marker(workspace_root: Path) -> Path:
    return workspace_root / "current_epoch"


# ---------------------------------------------------------------------------
# Id construction
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Coerce a human name into a filesystem-safe slug.

    Lowercased, non-alphanumerics collapsed to underscore, leading and
    trailing underscores stripped. An empty result raises — the operator
    must give us SOMETHING to anchor the id on.
    """
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    if not slug:
        raise ValueError(f"epoch name {name!r} produced an empty slug")
    return slug


def _today() -> str:
    """ISO date for the epoch id prefix. UTC by convention."""
    return _dt.datetime.now(_dt.UTC).date().isoformat()


def _now_iso() -> str:
    """ISO-8601 UTC second-precision timestamp."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _make_epoch_id(workspace_root: Path, name: str) -> str:
    """Construct ``{date}_{slug}`` with a numeric suffix if necessary."""
    base = f"{_today()}_{_slugify(name)}"
    candidate = base
    suffix = 2
    while epoch_dir(workspace_root, candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _scoring_to_dict(weights: ScoringWeights) -> dict[str, Any]:
    return {
        "drift_weight": weights.drift_weight,
        "pass_weight": weights.pass_weight,
        "severity_weights": dict(weights.severity_weights),
        "per_kind_weights": dict(weights.per_kind_weights),
        "plan_revision_weight": weights.plan_revision_weight,
        "runtime_weight": weights.runtime_weight,
        "promote_margin": weights.promote_margin,
        "pass_rate_monotonicity": weights.pass_rate_monotonicity,
    }


def _scoring_from_dict(d: dict[str, Any]) -> ScoringWeights:
    raw_sev = d.get("severity_weights")
    if raw_sev:
        severity = {str(k): float(v) for k, v in raw_sev.items()}
        return ScoringWeights(
            drift_weight=float(d.get("drift_weight", 1.0)),
            pass_weight=float(d.get("pass_weight", 1.0)),
            severity_weights=severity,
            per_kind_weights={
                str(k): float(v) for k, v in d.get("per_kind_weights", {}).items()
            },
            plan_revision_weight=float(d.get("plan_revision_weight", 0.5)),
            runtime_weight=float(d.get("runtime_weight", 0.0)),
            promote_margin=float(d.get("promote_margin", 0.01)),
            pass_rate_monotonicity=bool(d.get("pass_rate_monotonicity", True)),
        )
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
    )


def _config_to_dict(cfg: EpochConfig) -> dict[str, Any]:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "created_at": cfg.created_at,
        "board_path": str(cfg.board_path),
        "rubric_path": str(cfg.rubric_path),
        "scoring": _scoring_to_dict(cfg.scoring),
        "closed": cfg.closed,
        "closed_at": cfg.closed_at,
    }


def _config_from_dict(d: dict[str, Any]) -> EpochConfig:
    return EpochConfig(
        id=d["id"],
        name=d["name"],
        created_at=d["created_at"],
        board_path=Path(d["board_path"]),
        rubric_path=Path(d["rubric_path"]),
        scoring=_scoring_from_dict(d.get("scoring", {})),
        closed=bool(d.get("closed", False)),
        closed_at=d.get("closed_at", ""),
    )


def _write_config(workspace_root: Path, cfg: EpochConfig) -> None:
    path = _config_path(workspace_root, cfg.id)
    path.write_text(json.dumps(_config_to_dict(cfg), indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Current-epoch marker
# ---------------------------------------------------------------------------


def current_epoch_id(workspace_root: Path) -> str | None:
    """Read the workspace's ``current_epoch`` marker file.

    Returns ``None`` when there is no marker (fresh workspace, or the
    marker was removed by hand). Returns the stripped contents otherwise.
    """
    marker = _current_marker(workspace_root)
    if not marker.exists():
        return None
    text = marker.read_text().strip()
    return text or None


def switch_epoch(workspace_root: Path, epoch_id: str) -> None:
    """Point the ``current_epoch`` marker at ``epoch_id``.

    The target epoch directory MUST exist; we refuse to dangle the
    marker. Use :func:`new_epoch` to create an epoch and switch in one
    step — that path is the common one.
    """
    if not epoch_dir(workspace_root, epoch_id).exists():
        raise FileNotFoundError(
            f"epoch {epoch_id!r} does not exist under {workspace_root}"
        )
    workspace_root.mkdir(parents=True, exist_ok=True)
    _current_marker(workspace_root).write_text(epoch_id + "\n")


# ---------------------------------------------------------------------------
# Listing / loading
# ---------------------------------------------------------------------------


def load_epoch(workspace_root: Path, epoch_id: str) -> EpochConfig:
    """Read one epoch's ``config.json`` back into an :class:`EpochConfig`."""
    path = _config_path(workspace_root, epoch_id)
    if not path.exists():
        raise FileNotFoundError(
            f"epoch {epoch_id!r} has no config.json under {workspace_root}"
        )
    return _config_from_dict(json.loads(path.read_text()))


def list_epochs(workspace_root: Path) -> list[EpochConfig]:
    """Enumerate every epoch known to the workspace, sorted by ``created_at``.

    Directories under ``epochs/`` without a readable ``config.json`` are
    skipped silently — they are presumed to be in-progress writes from a
    crashed ``epoch new`` and the operator can clean them up by hand.
    """
    epochs_root = workspace_root / "epochs"
    if not epochs_root.exists():
        return []
    out: list[EpochConfig] = []
    for child in sorted(epochs_root.iterdir()):
        if not child.is_dir():
            continue
        cfg_path = child / "config.json"
        if not cfg_path.exists():
            continue
        try:
            out.append(_config_from_dict(json.loads(cfg_path.read_text())))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    out.sort(key=lambda c: (c.created_at, c.id))
    return out


# ---------------------------------------------------------------------------
# new_epoch / close_epoch
# ---------------------------------------------------------------------------


def new_epoch(
    workspace_root: Path,
    name: str,
    board_source: Path,
    rubric_source: Path,
    weights: ScoringWeights,
    auto_close_previous: bool = True,
    aux_call_llm: _AuxCallLLM | None = None,
) -> EpochConfig:
    """Create a new epoch directory and switch to it.

    Steps:
      1. If ``auto_close_previous`` and the current epoch is open, close
         it first (warning to stderr). ``aux_call_llm`` is required for
         that close — the analysis pass runs on it.
      2. Compute the epoch id from ``name`` and today's date.
      3. Create ``.zicato/epochs/{id}/`` and copy the board + rubric in.
      4. Serialize ``weights`` to ``scoring.json``.
      5. Write ``config.json`` and update ``lineage.json``.
      6. Update the ``current_epoch`` marker.

    Returns the constructed :class:`EpochConfig`.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)

    # 1. Auto-close previous if open.
    prev_id = current_epoch_id(workspace_root)
    if auto_close_previous and prev_id is not None:
        try:
            prev_cfg = load_epoch(workspace_root, prev_id)
        except FileNotFoundError:
            prev_cfg = None
        if prev_cfg is not None and not prev_cfg.closed:
            print(
                f"WARNING: previous epoch {prev_id!r} was not closed manually; "
                "auto-closing now. analysis.md may be shorter / lower quality "
                "than a manual close.",
                file=sys.stderr,
            )
            warnings.warn(
                f"auto-closing previous epoch {prev_id!r}",
                stacklevel=2,
            )
            close_epoch(workspace_root, prev_id, aux_call_llm=aux_call_llm)

    # 2. Construct the new id.
    epoch_id = _make_epoch_id(workspace_root, name)

    # 3. Create the directory and copy contracts in.
    edir = epoch_dir(workspace_root, epoch_id)
    edir.mkdir(parents=True, exist_ok=False)
    target_board = board_path(workspace_root, epoch_id)
    target_rubric = rubric_path(workspace_root, epoch_id)
    shutil.copyfile(board_source, target_board)
    shutil.copyfile(rubric_source, target_rubric)

    # 4. Scoring weights.
    scoring_path(workspace_root, epoch_id).write_text(
        json.dumps(_scoring_to_dict(weights), indent=2, sort_keys=True)
    )

    # 5. Config + lineage.
    cfg = EpochConfig(
        id=epoch_id,
        name=name,
        created_at=_now_iso(),
        board_path=target_board,
        rubric_path=target_rubric,
        scoring=weights,
        closed=False,
        closed_at="",
    )
    _write_config(workspace_root, cfg)

    # Lineage update (imported lazily to avoid a circular import at module
    # load time — lineage.py wants to read this module's helpers).
    from zicato.epoch import lineage as _lineage

    _lineage.register_epoch(workspace_root, cfg, parent_epoch_id=prev_id)

    # 6. Marker.
    switch_epoch(workspace_root, epoch_id)
    return cfg


def close_epoch(
    workspace_root: Path,
    epoch_id: str | None = None,
    aux_call_llm: _AuxCallLLM | None = None,
) -> Path:
    """Mark an epoch closed and generate ``analysis.md`` for it.

    If ``epoch_id`` is ``None`` we close the current epoch. If
    ``aux_call_llm`` is ``None`` we still mark the epoch closed and
    write a stub ``analysis.md`` (the operator can re-run the analysis
    pass later by hand). The return value is the analysis path so the
    caller can render it / chmod it / etc.
    """
    if epoch_id is None:
        epoch_id = current_epoch_id(workspace_root)
        if epoch_id is None:
            raise RuntimeError(
                "close_epoch: no epoch_id supplied and no current_epoch marker"
            )

    cfg = load_epoch(workspace_root, epoch_id)
    if not cfg.closed:
        from dataclasses import replace

        cfg = replace(cfg, closed=True, closed_at=_now_iso())
        _write_config(workspace_root, cfg)

    # Update lineage's per-epoch closed_at.
    from zicato.epoch import lineage as _lineage

    _lineage.mark_closed(workspace_root, epoch_id, cfg.closed_at)

    # Generate analysis.md. If no aux callable was provided we still
    # leave a placeholder so callers see a non-empty file — the analysis
    # pass is rerunnable.
    out_path = analysis_path(workspace_root, epoch_id)
    if aux_call_llm is not None:
        from zicato.epoch import analysis as _analysis

        asyncio.run(
            _analysis.generate_analysis(
                workspace_root,
                epoch_id,
                aux_call_llm,
                model="",
            )
        )
    else:
        if not out_path.exists():
            jpath = journal_path(workspace_root, epoch_id)
            journal_content = jpath.read_text() if jpath.exists() else "(no journal entries)"
            out_path.write_text(
                f"# Epoch analysis: {epoch_id}\n\n"
                "_No auxiliary LLM was supplied at close; this is a stub. "
                "Re-run `zicato epoch close` with an `aux_call_llm` configured "
                "to regenerate._\n\n"
                "## Journal snapshot\n\n"
                f"{journal_content}\n"
            )
    return out_path


__all__ = [
    "new_epoch",
    "close_epoch",
    "list_epochs",
    "switch_epoch",
    "current_epoch_id",
    "load_epoch",
]
