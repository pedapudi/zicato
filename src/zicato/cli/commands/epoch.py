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
  zicato epoch gc [<epoch_id>] (--keep-last <n> | --keep-promoted-only) [--apply]

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
import sys
from pathlib import Path

import click

from zicato.core.types import ScoringWeights
from zicato.epoch import lifecycle
from zicato.epoch.contract import default_contract_paths
from zicato.epoch.lineage import render_lineage_summary
from zicato.index.ingest import rebuild_index, repair_epoch_goals
from zicato.workspace.config_io import read_workspace_config, write_workspace_config


def _prompt_for_goal() -> str:
    """Ask the operator for the epoch's goal when stdin is a TTY.

    Returns the entered line (stripped). In non-TTY contexts — piped
    input, CI, automation — returns the empty string without
    prompting. A bare ``Enter`` (or an interrupt) also yields the
    empty string, which downstream code renders as "no goal recorded".
    """
    if not sys.stdin.isatty():
        return ""
    try:
        answer = click.prompt(
            "What is the goal of this epoch? (one line, leave blank to skip)",
            default="",
            show_default=False,
        )
    except (click.Abort, EOFError, KeyboardInterrupt):
        return ""
    return str(answer).strip()


def _resolve_workspace(workspace: str) -> Path:
    """Convert a workspace CLI arg (default ``.zicato``) into a Path.

    Relative paths are interpreted against the operator's current
    working directory — the same convention as every other ``zicato``
    command.
    """
    return Path(workspace).resolve()


def _load_weights(scoring_path: str | None) -> ScoringWeights:
    """Load scoring weights from JSON, or return defaults.

    Delegates to :func:`zicato.workspace_loader.scoring_weights_from_dict`
    — the SAME loader the contract canonicalizer and ``evolve`` use when
    they re-derive the live scoring — so the ``ScoringWeights`` ``epoch
    new`` freezes is byte-for-byte what a later ``evolve`` reconstructs
    from the live ``scoring.json``. A field-by-field reimplementation
    here historically dropped the ``tournament`` block, which made an
    epoch created with a tournament structure auto-roll on the very next
    ``evolve`` (the frozen hash was computed over a gauntlet default while
    ``evolve`` recomputed over the real structure). Sharing one loader
    keeps the two paths from drifting again.
    """
    if scoring_path is None:
        return ScoringWeights()
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(Path(scoring_path).read_text())
    return scoring_weights_from_dict(raw)


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

    # These three default keys are always concrete Paths (only
    # ``proposer_path`` defaults to ``None``); narrow for the type checker.
    default_board = defaults["board_path"]
    default_brief = defaults["rubric_path"]
    default_scoring = defaults["scoring_path"]
    assert default_board is not None and default_brief is not None and default_scoring is not None

    board_target = Path(contract.get("board_path") or default_board)
    # ``rubric_path`` is the on-disk key name for the proposer brief
    # (kept for back-compat); ``brief_path`` is also accepted on read.
    brief_target = Path(contract.get("brief_path") or contract.get("rubric_path") or default_brief)
    scoring_target = Path(contract.get("scoring_path") or default_scoring)

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
@click.option(
    "--goal",
    "goal",
    default=None,
    help="Free-form statement of *why* this epoch exists (the intent "
    "the operator is testing). Persisted into config.json and "
    "surfaced in the analyzer report header. When omitted and stdin "
    "is a TTY, the operator is prompted for one line; in non-TTY "
    "contexts the goal defaults to the empty string.",
)
def new_cmd(
    name: str,
    workspace: str,
    board_source: str,
    brief_source: str,
    scoring_source: str | None,
    goal: str | None,
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

    # Resolve the goal: explicit flag wins; otherwise prompt the
    # operator when stdin is a TTY (one line is enough — multi-line
    # goals are supported on the field itself, but the CLI prompt is
    # kept simple), or fall back to the empty string in non-TTY
    # contexts (CI, piped input, automation).
    resolved_goal = goal if goal is not None else _prompt_for_goal()

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
        goal=resolved_goal,
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


@epoch_grp.command(
    "gc",
    short_help="Prune settled-rejected generation source trees (dry-run by default).",
)
@click.argument("epoch_id", required=False)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--keep-last",
    "keep_last_n",
    type=int,
    default=None,
    help="Keep the N newest generations in addition to the always-kept "
    "set (promoted chain, in-flight generations, v0); prune older "
    "settled-rejected trees.",
)
@click.option(
    "--keep-promoted-only",
    is_flag=True,
    default=False,
    help="Keep only the always-kept set; prune every settled-rejected " "generation's source tree.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Actually prune. Without this flag the command is a DRY RUN "
    "that prints the plan and removes nothing.",
)
def gc_cmd(
    epoch_id: str | None,
    workspace: str,
    keep_last_n: int | None,
    keep_promoted_only: bool,
    apply_: bool,
) -> None:
    """Prune generation SOURCE TREES under an epoch; records survive.

    Reclaims the disk held by settled-rejected generations' source
    trees (directory-backend snapshot dirs; git-backend tags +
    worktrees, whose commits then become collectable). Never touches
    lineage.json, the journal, experiment/score records, or run
    telemetry — a pruned generation stays fully analysable, it just no
    longer has a browsable source tree.

    Promoted generations, in-flight generations, and the seed v0 are
    never pruned. Select a retention policy with exactly one of
    --keep-last N / --keep-promoted-only. Dry-run by default; pass
    --apply to execute. When EPOCH_ID is omitted, the current epoch is
    targeted.
    """
    from zicato.epoch.gc import prune_generations

    ws = _resolve_workspace(workspace)
    if epoch_id is None:
        epoch_id = lifecycle.current_epoch_id(ws)
        if epoch_id is None:
            raise click.UsageError("no EPOCH_ID supplied and no current_epoch marker")
    if keep_promoted_only == (keep_last_n is not None):
        raise click.UsageError("pass exactly one of --keep-last N / --keep-promoted-only")
    try:
        report = prune_generations(
            ws,
            epoch_id,
            keep_last_n=keep_last_n,
            keep_promoted_only=keep_promoted_only,
            dry_run=not apply_,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    verb = "would prune" if report.dry_run else "pruned"
    click.echo(f"Epoch {report.epoch_id} ({report.backend} backend, {report.policy}):")
    click.echo(f"  kept   : {', '.join(report.kept) or '(none)'}")
    click.echo(
        f"  {verb}: {', '.join(report.pruned) or '(none)'}"
        f"  [{report.bytes_reclaimed} tree bytes]"
    )
    if report.dry_run:
        click.echo("DRY RUN — nothing was removed. Re-run with --apply to prune.")


@epoch_grp.command(
    "set-goal",
    short_help="Set or overwrite the goal field on an existing epoch.",
)
@click.option(
    "--epoch",
    "epoch_id",
    required=True,
    help="The epoch id to mutate.",
)
@click.option(
    "--goal",
    "goal",
    required=True,
    help="The free-form goal text to write into the epoch's config.json.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def set_goal_cmd(epoch_id: str, goal: str, workspace: str) -> None:
    """Set the goal on an existing epoch and re-ingest its index row.

    Designed for the contract-hash auto-roll case: when ``zicato
    evolve`` opens a new epoch mid-run there is no opportunity to
    prompt the operator, so the goal lands as an empty string + a
    warning that recommends running this command later.

    Idempotent — writes the supplied goal into ``config.json`` and
    refreshes the ``epochs.goal`` index column. The rest of the index
    is left alone (use ``zicato reindex`` for a full rebuild).
    """
    ws = _resolve_workspace(workspace)
    try:
        cfg = lifecycle.set_epoch_goal(ws, epoch_id, goal)
    except FileNotFoundError as exc:
        raise click.UsageError(str(exc)) from exc
    # Re-ingest just this epoch's row. ``repair_epoch_goals`` walks
    # every epoch but that is the simplest idempotent path; the index
    # writes are keyed upserts so the other rows are no-ops.
    repair_epoch_goals(ws)
    click.echo(f"Set goal for epoch {cfg.id}.")


@click.command(
    name="repair-epoch-goals",
    short_help="Advanced: backfill the goal field on epochs that predate the field.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
def repair_epoch_goals_cmd(workspace: str) -> None:
    """Walk every epoch on disk and add an empty goal where missing.

    Targeted migration helper for workspaces whose per-epoch
    ``config.json`` files were written before the ``goal`` field
    landed. Defaults missing goals to the empty string (which renders
    as "no goal recorded" in the analyzer), and refreshes the
    ``epochs.goal`` column in the index database to match.

    Read-only against epochs that already have a goal value
    (including a deliberately-empty one). Idempotent: running it
    twice writes the same bytes. The index is created with the
    current schema if it does not exist yet.

    For populating the goal on an individual epoch with a real value,
    see ``zicato epoch set-goal``.
    """
    ws = _resolve_workspace(workspace)
    # Ensure the index exists with the current schema so the column is
    # present before repair_epoch_goals tries to upsert into it.
    db_path = ws / "index.db"
    if not db_path.exists():
        rebuild_index(ws)
    result = repair_epoch_goals(ws)
    click.echo(
        f"Repaired {result['scanned']} epochs at {ws}: "
        f"{result['config_patched']} config.json files patched, "
        f"{result['index_updated']} index rows refreshed."
    )


__all__ = ["epoch_grp", "repair_epoch_goals_cmd"]
