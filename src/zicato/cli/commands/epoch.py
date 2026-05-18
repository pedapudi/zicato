"""``zicato epoch`` command group.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` opens,
closes, and rolls epochs automatically (contract-hash auto-epoching).
Reach for ``zicato epoch`` only to inspect epochs or to force an epoch
boundary by hand.

Surface:

  zicato epoch new <name> --board <path> --brief <path> [--scoring <path>]
  zicato epoch close [<epoch_id>]
  zicato epoch list
  zicato epoch switch <epoch_id>

This module is thin — every command is one Click handler that calls
into :mod:`zicato.epoch.lifecycle`. There is no business logic here;
when the surface changes that work happens in the lifecycle module and
this file just plumbs the arguments.

Contract source paths — single source of truth
-----------------------------------------------
``epoch new`` freezes a per-epoch copy of the board / proposer brief /
scoring into ``epochs/{id}/`` (the immutable snapshot). It ALSO adopts
the supplied files as the workspace's *live* contract: it copies them
to the canonical contract source location and records that location in
``config.json`` under the ``contract`` key. That canonical location is
the one — and only — place ``zicato evolve`` /
:func:`zicato.epoch.contract.resolve_contract_inputs` reads the live
contract back from. Keeping ``epoch new`` and ``evolve`` pointed at the
same files is what makes both the explicit
``init → register → epoch new → evolve`` flow and the streamlined
``init → register → (edit files) → evolve`` flow resolve the contract
end to end. Because ``epoch new`` publishes the *same* bytes it freezes,
the contract hash a later ``evolve`` derives matches the epoch's stored
hash, so ``evolve`` does not spuriously roll the epoch.

The auxiliary LLM callable required by ``epoch new --auto-close`` and
``epoch close`` is **not** wired through the CLI in this patch. A later
patch lands ``zicato config`` to bind the callable from the operator's
chosen provider; for now the CLI passes ``aux_call_llm=None`` and the
lifecycle falls back to a stub ``analysis.md``. The Python API supports
the full surface for tests.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click

from zicato.cli.common import read_workspace_config, write_workspace_config
from zicato.core.types import ScoringWeights
from zicato.epoch import lifecycle
from zicato.epoch.contract import default_contract_paths
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
            per_kind_weights={str(k): float(v) for k, v in raw.get("per_kind_weights", {}).items()},
            plan_revision_weight=float(raw.get("plan_revision_weight", 0.5)),
            runtime_weight=float(raw.get("runtime_weight", 0.0)),
            promote_margin=float(raw.get("promote_margin", 0.01)),
            pass_rate_monotonicity=bool(raw.get("pass_rate_monotonicity", True)),
        )
    return ScoringWeights(
        drift_weight=float(raw.get("drift_weight", 1.0)),
        pass_weight=float(raw.get("pass_weight", 1.0)),
        per_kind_weights={str(k): float(v) for k, v in raw.get("per_kind_weights", {}).items()},
        plan_revision_weight=float(raw.get("plan_revision_weight", 0.5)),
        runtime_weight=float(raw.get("runtime_weight", 0.0)),
        promote_margin=float(raw.get("promote_margin", 0.01)),
        pass_rate_monotonicity=bool(raw.get("pass_rate_monotonicity", True)),
    )


def _adopt_contract_sources(
    workspace_root: Path,
    *,
    board_source: Path,
    brief_source: Path,
    scoring_source: Path | None,
) -> None:
    """Publish ``epoch new``'s contract files as the workspace's live contract.

    ``epoch new`` freezes a per-epoch copy of the board / proposer brief
    / scoring into ``epochs/{id}/``. That frozen copy is the immutable
    snapshot, but it is NOT what ``zicato evolve`` reads on a subsequent
    run — :func:`zicato.epoch.contract.resolve_contract_inputs` resolves
    the *live* contract from the paths recorded in ``config.json`` under
    the ``contract`` key (defaulting to the conventional location next
    to the ``.zicato/`` directory).

    Without this step the explicit ``init → register → epoch new →
    evolve`` flow breaks: ``epoch new`` would copy the operator's files
    only into the epoch dir, then ``evolve`` would resolve the live
    contract from the (still empty) conventional location and fail with
    "board file ... is missing".

    This helper closes that gap. It:

    1. Resolves the canonical contract source paths from the workspace's
       existing ``config.json`` ``contract`` block, falling back to the
       conventional defaults when a key (or the whole block) is absent.
    2. Copies each supplied source file to its canonical path, unless
       the source already *is* that path (the streamlined flow, where
       the operator edited the live files in place).
    3. Writes the ``contract`` block back so the resolved paths are
       recorded — making ``epoch new`` agree with ``register`` and
       ``evolve`` on where the live contract lives.

    Because the bytes published here are the same bytes
    :func:`zicato.epoch.lifecycle.new_epoch` froze into the epoch dir,
    the contract hash a later ``evolve`` derives from these live files
    matches the epoch's stored hash — so ``evolve`` continues the epoch
    rather than spuriously rolling it.
    """
    config = read_workspace_config(workspace_root)
    defaults = default_contract_paths(workspace_root)
    contract = dict(config.get("contract") or {})

    board_target = Path(contract.get("board_path") or defaults["board_path"])
    # ``rubric_path`` is the on-disk key name for the proposer brief
    # (kept for back-compat); ``brief_path`` is also accepted on read.
    brief_target = Path(
        contract.get("brief_path") or contract.get("rubric_path") or defaults["rubric_path"]
    )
    scoring_target = Path(contract.get("scoring_path") or defaults["scoring_path"])

    def _publish(source: Path, target: Path) -> None:
        source = source.resolve()
        target = target.resolve()
        if source == target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    _publish(board_source, board_target)
    _publish(brief_source, brief_target)
    if scoring_source is not None:
        _publish(scoring_source, scoring_target)

    contract["board_path"] = str(board_target.resolve())
    contract["rubric_path"] = str(brief_target.resolve())
    contract["scoring_path"] = str(scoring_target.resolve())
    config["contract"] = contract
    write_workspace_config(workspace_root, config)


@click.group(
    name="epoch",
    short_help="Advanced: inspect / force epochs (evolve auto-epochs for you).",
)
def epoch_grp() -> None:
    """Advanced: manage zicato epochs — the unit of evaluation contract.

    Off the happy path. `zicato evolve` opens, closes, and rolls
    epochs on its own whenever the evaluation contract changes
    (contract-hash auto-epoching). Use this group only to inspect
    epochs (`epoch list`) or to force an epoch boundary by hand.
    """


@epoch_grp.command(
    "new",
    short_help="Create a new epoch and make it current.",
)
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
    help="Path to a board.jsonl. Frozen into the epoch and adopted as "
    "the workspace's live contract board.",
)
@click.option(
    "--brief",
    "--rubric",
    "brief_source",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a proposer brief (brief.md). Frozen into the epoch "
    "and adopted as the workspace's live contract brief. ``--rubric`` "
    "is accepted as a legacy alias.",
)
@click.option(
    "--scoring",
    "scoring_source",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to scoring.json; defaults applied if absent. When given, "
    "frozen into the epoch and adopted as the live contract scoring.",
)
def new_cmd(
    name: str,
    workspace: str,
    board_source: str,
    brief_source: str,
    scoring_source: str | None,
) -> None:
    """Advanced: create a new epoch and make it current.

    Off the happy path — `zicato evolve` auto-opens epochs. Run this
    by hand only to force an epoch boundary.

    If a previous epoch is still open it is auto-closed first; the auto
    close emits a stub analysis.md (no auxiliary LLM is wired through
    the CLI yet — see module docstring).

    The supplied contract files are both frozen into the epoch
    directory AND published as the workspace's live contract (recorded
    in config.json under `contract`), so a subsequent `zicato evolve`
    resolves the same contract and continues this epoch rather than
    failing to find the board or spuriously rolling.
    """
    ws = _resolve_workspace(workspace)
    weights = _load_weights(scoring_source)

    # Carry the registered inner-harness identity (entrypoint + mutable
    # trees) into the epoch's contract hash. `zicato evolve` derives the
    # contract hash from these same `config.json` values via
    # resolve_contract_inputs; freezing the epoch with empty identity
    # components would make the two hashes disagree and trigger a
    # spurious roll on the very first evolve.
    config = read_workspace_config(ws)
    entrypoint = str(config.get("adk_entrypoint", ""))
    raw_trees = config.get("mutable_trees") or config.get("source_roots") or []
    mutable_trees = tuple(str(t) for t in raw_trees)

    cfg = lifecycle.new_epoch(
        workspace_root=ws,
        name=name,
        board_source=Path(board_source),
        brief_source=Path(brief_source),
        weights=weights,
        auto_close_previous=True,
        aux_call_llm=None,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
    )
    # Publish the supplied files as the workspace's live contract so
    # `zicato evolve` / resolve_contract_inputs find the same contract.
    # Done after new_epoch so the workspace directory is guaranteed to
    # exist (new_epoch mkdir's it) before config.json is written.
    _adopt_contract_sources(
        ws,
        board_source=Path(board_source),
        brief_source=Path(brief_source),
        scoring_source=Path(scoring_source) if scoring_source is not None else None,
    )
    click.echo(f"Created epoch {cfg.id} (now current).")


@epoch_grp.command(
    "close",
    short_help="Close an epoch and write its analysis.md.",
)
@click.argument("epoch_id", required=False)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def close_cmd(epoch_id: str | None, workspace: str) -> None:
    """Advanced: close an epoch and (best-effort) generate analysis.md.

    Off the happy path — `zicato evolve` closes epochs on its own when
    the contract rolls. When EPOCH_ID is omitted, the current epoch is
    closed. The analysis pass runs only if an auxiliary LLM has been
    configured — until then this writes a stub analysis.md that the
    operator can regenerate later.
    """
    ws = _resolve_workspace(workspace)
    out_path = lifecycle.close_epoch(ws, epoch_id=epoch_id, aux_call_llm=None)
    click.echo(f"Closed. Wrote {out_path}.")


@epoch_grp.command(
    "list",
    short_help="List every epoch in the workspace.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def list_cmd(workspace: str) -> None:
    """List every epoch in the workspace as a markdown table."""
    ws = _resolve_workspace(workspace)
    click.echo(render_lineage_summary(ws))


@epoch_grp.command(
    "switch",
    short_help="Point the current-epoch marker at EPOCH_ID.",
)
@click.argument("epoch_id")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def switch_cmd(epoch_id: str, workspace: str) -> None:
    """Advanced: point the workspace's current_epoch marker at EPOCH_ID."""
    ws = _resolve_workspace(workspace)
    lifecycle.switch_epoch(ws, epoch_id)
    click.echo(f"Switched to {epoch_id}.")


__all__ = ["epoch_grp"]
