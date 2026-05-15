"""``zicato epoch`` command group.

Surface:

  zicato epoch new <name> --board <path> --rubric <path> [--scoring <path>]
  zicato epoch close [<epoch_id>]
  zicato epoch list
  zicato epoch switch <epoch_id>

This module is thin — every command is one Click handler that calls
into :mod:`zicato.epoch.lifecycle`. There is no business logic here;
when the surface changes that work happens in the lifecycle module and
this file just plumbs the arguments.

The auxiliary LLM callable required by ``epoch new --auto-close`` and
``epoch close`` is **not** wired through the CLI in this patch. A later
patch lands ``zicato config`` to bind the callable from the operator's
chosen provider; for now the CLI passes ``aux_call_llm=None`` and the
lifecycle falls back to a stub ``analysis.md``. The Python API supports
the full surface for tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from zicato.core.types import ScoringWeights
from zicato.epoch import lifecycle
from zicato.epoch.lineage import render_lineage_summary


def _resolve_workspace(workspace: str) -> Path:
    """Convert a workspace CLI arg (default ``.zicato``) into a Path.

    Relative paths are interpreted against the operator's current
    working directory — the same convention as every other ``zicato``
    command.
    """
    return Path(workspace).resolve()


def _load_weights(scoring_path: str | None) -> ScoringWeights:
    """Load scoring weights from JSON, or return defaults."""
    if scoring_path is None:
        return ScoringWeights()
    raw = json.loads(Path(scoring_path).read_text())
    # We accept the same dict shape that lifecycle._scoring_to_dict
    # produces; field-by-field with sensible defaults.
    severity = raw.get("severity_weights")
    if severity:
        severity = {str(k): float(v) for k, v in severity.items()}
        return ScoringWeights(
            drift_weight=float(raw.get("drift_weight", 1.0)),
            pass_weight=float(raw.get("pass_weight", 1.0)),
            severity_weights=severity,
            per_kind_weights={
                str(k): float(v) for k, v in raw.get("per_kind_weights", {}).items()
            },
            plan_revision_weight=float(raw.get("plan_revision_weight", 0.5)),
            runtime_weight=float(raw.get("runtime_weight", 0.0)),
            promote_margin=float(raw.get("promote_margin", 0.01)),
            pass_rate_monotonicity=bool(raw.get("pass_rate_monotonicity", True)),
        )
    return ScoringWeights(
        drift_weight=float(raw.get("drift_weight", 1.0)),
        pass_weight=float(raw.get("pass_weight", 1.0)),
        per_kind_weights={
            str(k): float(v) for k, v in raw.get("per_kind_weights", {}).items()
        },
        plan_revision_weight=float(raw.get("plan_revision_weight", 0.5)),
        runtime_weight=float(raw.get("runtime_weight", 0.0)),
        promote_margin=float(raw.get("promote_margin", 0.01)),
        pass_rate_monotonicity=bool(raw.get("pass_rate_monotonicity", True)),
    )


@click.group(name="epoch")
def epoch_grp() -> None:
    """Manage zicato epochs (the unit of evaluation contract)."""


@epoch_grp.command("new")
@click.argument("name")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--board",
    "board_source",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a board.jsonl to copy into the epoch.",
)
@click.option(
    "--rubric",
    "rubric_source",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a rubric.md to copy into the epoch.",
)
@click.option(
    "--scoring",
    "scoring_source",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to scoring.json; defaults applied if absent.",
)
def new_cmd(
    name: str,
    workspace: str,
    board_source: str,
    rubric_source: str,
    scoring_source: str | None,
) -> None:
    """Create a new epoch and make it current.

    If a previous epoch is still open it is auto-closed first; the auto
    close emits a stub analysis.md (no auxiliary LLM is wired through
    the CLI yet — see module docstring).
    """
    ws = _resolve_workspace(workspace)
    weights = _load_weights(scoring_source)
    cfg = lifecycle.new_epoch(
        workspace_root=ws,
        name=name,
        board_source=Path(board_source),
        rubric_source=Path(rubric_source),
        weights=weights,
        auto_close_previous=True,
        aux_call_llm=None,
    )
    click.echo(f"Created epoch {cfg.id} (now current).")


@epoch_grp.command("close")
@click.argument("epoch_id", required=False)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def close_cmd(epoch_id: str | None, workspace: str) -> None:
    """Close an epoch and (best-effort) generate ``analysis.md``.

    When ``EPOCH_ID`` is omitted, the current epoch is closed. The
    analysis pass runs only if an auxiliary LLM has been configured —
    until then this writes a stub analysis.md that the operator can
    regenerate later.
    """
    ws = _resolve_workspace(workspace)
    out_path = lifecycle.close_epoch(ws, epoch_id=epoch_id, aux_call_llm=None)
    click.echo(f"Closed. Wrote {out_path}.")


@epoch_grp.command("list")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def list_cmd(workspace: str) -> None:
    """List every epoch in the workspace (markdown table)."""
    ws = _resolve_workspace(workspace)
    click.echo(render_lineage_summary(ws))


@epoch_grp.command("switch")
@click.argument("epoch_id")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def switch_cmd(epoch_id: str, workspace: str) -> None:
    """Point the workspace's ``current_epoch`` marker at ``EPOCH_ID``."""
    ws = _resolve_workspace(workspace)
    lifecycle.switch_epoch(ws, epoch_id)
    click.echo(f"Switched to {epoch_id}.")


__all__ = ["epoch_grp"]
