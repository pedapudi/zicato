"""Advanced commands for explicit epoch boundaries and read-only inspection.

Explicit creation captures supplied contract files under the workspace writer.
The epoch retains accepted live-source writes so recovery publishes the same
contract before making the epoch current.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import click

from zicato.contract_draft.publication import (
    ContractSource,
    capture_contract_source,
    prepare_contract_publication,
    recover_contract_publication,
)
from zicato.core.types import ScoringWeights
from zicato.epoch import lifecycle
from zicato.epoch.lineage import render_lineage_summary
from zicato.index.ingest import rebuild_index, repair_epoch_goals
from zicato.runtime.lock import WorkspaceLock, acquire_workspace_lock


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


def _load_weights(scoring_text: str) -> ScoringWeights:
    """Decode authored scoring using the same admission rules as live execution."""
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    return scoring_weights_from_dict(json.loads(scoring_text))


def _prepare_contract_sources(
    source: ContractSource, accepted: dict[str, str], *, writer: WorkspaceLock
) -> str:
    """Retain accepted writes at the workspace's registered live destinations."""
    config = dict(source.config.raw)
    contract = dict(config.get("contract") or {})
    for component in ("board", "brief", "scoring"):
        key = "rubric_path" if component == "brief" else f"{component}_path"
        contract[key] = str(source.file(component).path)
    if "brief_path" in contract:
        contract["brief_path"] = str(source.file("brief").path)
    config["contract"] = contract
    accepted = {**accepted, "config": json.dumps(config, indent=2, sort_keys=True) + "\n"}
    return prepare_contract_publication(source, accepted, writer=writer)


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
    type=click.Path(dir_okay=False),
    help="Path to a board.jsonl. Frozen into the epoch and adopted as "
    "the workspace's live contract board.",
)
@click.option(
    "--brief",
    "--rubric",
    "brief_source",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to a proposer brief (brief.md). Frozen into the epoch "
    "and adopted as the workspace's live contract brief. ``--rubric`` "
    "is accepted as a legacy alias.",
)
@click.option(
    "--scoring",
    "scoring_source",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path to scoring.json; defaults applied if absent. The accepted "
    "scoring is frozen into the epoch and adopted as the live contract scoring.",
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
    """Create an epoch and adopt its contract for subsequent evolve runs."""
    ws = _resolve_workspace(workspace)
    resolved_goal = goal if goal is not None else _prompt_for_goal()
    with acquire_workspace_lock(ws, "epoch-publication") as writer:
        recover_contract_publication(ws, writer=writer)
        cfg = lifecycle.recover_epoch_publication(ws, writer=writer)
        if cfg is None or cfg.name != name:
            source = capture_contract_source(ws)
            scoring = (
                Path(scoring_source).read_bytes().decode("utf-8")
                if scoring_source is not None
                else "{}\n"
            )
            weights = _load_weights(scoring)
            accepted = {
                "board": Path(board_source).read_bytes().decode("utf-8"),
                "brief": Path(brief_source).read_bytes().decode("utf-8"),
                "scoring": scoring,
            }
            adoption = _prepare_contract_sources(source, accepted, writer=writer)
            with TemporaryDirectory(prefix="zicato-epoch-inputs-") as captured:
                board = Path(captured) / "board.jsonl"
                board.write_text(accepted["board"], encoding="utf-8")
                cfg = lifecycle.new_epoch(
                    workspace_root=ws,
                    name=name,
                    board_source=board,
                    brief_source=accepted["brief"],
                    weights=weights,
                    auto_close_previous=True,
                    aux_call_llm=None,
                    contract=source.inputs,
                    goal=resolved_goal,
                    writer=writer,
                    contract_adoption=adoption,
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
    closed. The analysis pass runs only if an evaluation LLM has been
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
    is left alone (use ``zicato repair index`` for a full rebuild).
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


@epoch_grp.command(
    "rounds",
    short_help="Verify every round of an epoch actually produced a measurement.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--epoch",
    "epoch_id",
    default=None,
    help="The epoch to verify. Defaults to the workspace's current epoch.",
)
@click.option(
    "--verify",
    is_flag=True,
    default=False,
    help="Make the verdict load-bearing: exit 1 when the epoch is NOT "
    "accepted (any void round) OR has no rounds at all. Without this flag "
    "the command is pure inspection and always exits 0.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the report as JSON (rounds, per-status counts, and the "
    "acceptance verdict) for a measurement protocol to consume.",
)
def rounds_cmd(workspace: str, epoch_id: str | None, verify: bool, as_json: bool) -> None:
    """Check ROUND-BY-ROUND that an epoch measured what it claims to have.

    A clean exit proves the loop ran; this command checks what it
    measured. A loop that exits cleanly, and even one that reached the
    model, can still have settled rounds that produced no duel — an
    endpoint outage mid-run leaves earlier rounds intact and later ones
    empty, and a mean built from the survivors is a different
    measurement rather than a smaller one. This reads the durable
    per-round event logs
    (`epochs/{epoch}/rounds/{round}/round_log.jsonl`) and classifies
    every round as `complete` (the round settled with a promotion-gate
    decision), `settled_degraded` (the round settled without a gate
    decision because the proposer returned an invalid patch, which is a
    real measurement of the proposer), or `void` (a torn log, a hard
    credential/transport failure, or a round that closed with neither a
    measurement nor an explanation).

    The cell-acceptance rule: an epoch is ACCEPTED iff it contains zero
    void rounds. Pass --verify to make that verdict the exit code.
    --verify ALSO fails an epoch with no rounds at all: zero rounds is
    vacuously free of void rounds, and letting emptiness read as health
    is how an unmeasured cell sneaks through a sweep.

    Read-only — it opens no network connection and writes nothing.
    """
    from zicato.epoch.round_integrity import epoch_round_integrity, render_round_integrity

    ws = _resolve_workspace(workspace)
    if epoch_id is None:
        epoch_id = lifecycle.current_epoch_id(ws)
        if epoch_id is None:
            raise click.UsageError("no --epoch supplied and no current_epoch marker")

    report = epoch_round_integrity(ws, epoch_id)
    if as_json:
        click.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        click.echo(render_round_integrity(report))

    # An epoch with zero rounds is vacuously "accepted" (there is no void
    # round in an empty set), so gating on `accepted` alone would pass a
    # cell whose evolve died before it ever wrote a round log — the exact
    # shape of the failure this command exists to catch, one level up.
    if verify and (report.no_rounds or not report.accepted):
        raise SystemExit(1)


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
