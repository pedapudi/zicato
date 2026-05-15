"""``zicato board`` — CLI subcommand group for board file operations.

The board lives at ``{workspace}/epochs/{epoch_id}/board.jsonl`` and is
the frozen-per-epoch list of evaluations the inner harness is scored
against. This command group exposes the three operations the operator
needs in a single epoch:

* ``zicato board add ENTRY_PATH`` — append one validated entry from a
  JSON file.
* ``zicato board list`` — render the current board with key fields.
* ``zicato board remove ENTRY_ID`` — drop an entry by id.

The "current epoch" is resolved by reading ``{workspace}/lineage.json``
when present; in its absence the command degrades to a single-epoch
fallback (epoch id ``"default"``) so a fresh workspace works without
operator intervention. The integration agent's epoch CLI will replace
the fallback with a proper lineage read.

All commands print human-readable output on success and exit non-zero
on validation errors with a one-line error message. The intent is that
the CLI is scriptable but also tolerable to read.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from zicato.board.jsonl import append_entry, load_board, remove_entry
from zicato.core.types import validate_board_entry
from zicato.core.workspace import board_path


def _resolve_epoch_id(workspace_root: Path) -> str:
    """Resolve the current epoch id from the workspace.

    Looks for ``lineage.json`` and reads its ``current_epoch`` field
    when present. Otherwise returns ``"default"`` so the command can
    still operate on a freshly initialised workspace.
    """
    lineage = workspace_root / "lineage.json"
    if lineage.exists():
        try:
            payload = json.loads(lineage.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "default"
        current = payload.get("current_epoch")
        if isinstance(current, str) and current:
            return current
    return "default"


def _resolve_board_path(workspace: str) -> Path:
    """Return the board.jsonl path under the given workspace root."""
    workspace_root = Path(workspace).resolve()
    epoch_id = _resolve_epoch_id(workspace_root)
    return board_path(workspace_root, epoch_id)


@click.group(name="board")
@click.pass_context
def board_grp(ctx: click.Context) -> None:
    """Manage the per-epoch board.jsonl file."""
    # No shared state — every subcommand resolves the board path on
    # its own. The pass_context wiring exists so the integration CLI
    # can stash global flags (workspace overrides, verbosity) on
    # ``ctx.obj`` when it composes this group under the top-level CLI.
    ctx.ensure_object(dict)


@board_grp.command("add")
@click.argument("entry_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
def add_cmd(entry_path: str, workspace: str) -> None:
    """Append a single board entry from a JSON file to the current epoch."""
    src = Path(entry_path)
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{src}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"{src}: expected a JSON object, got {type(payload).__name__}"
        )
    try:
        entry = validate_board_entry(payload)
    except (KeyError, ValueError) as exc:
        raise click.ClickException(f"{src}: invalid entry: {exc}") from exc

    target = _resolve_board_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        append_entry(target, entry)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"appended {entry.id} ({entry.kind}) to {target}")


@board_grp.command("list")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
def list_cmd(workspace: str) -> None:
    """List the entries in the current epoch's board."""
    target = _resolve_board_path(workspace)
    if not target.exists():
        click.echo(f"(no board at {target})")
        return
    try:
        entries = load_board(target)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not entries:
        click.echo(f"(empty board at {target})")
        return
    click.echo(f"{target} — {len(entries)} entries")
    for entry in entries:
        tags = ",".join(entry.tags) if entry.tags else "-"
        has_exp = "yes" if entry.expectation is not None else "no"
        click.echo(
            f"  {entry.id}\t{entry.kind}\t"
            f"budget={entry.wall_clock_budget_seconds}s\t"
            f"weight={entry.weight}\ttags={tags}\texpectation={has_exp}"
        )


@board_grp.command("remove")
@click.argument("entry_id")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
def remove_cmd(entry_id: str, workspace: str) -> None:
    """Remove an entry by id from the current epoch's board."""
    target = _resolve_board_path(workspace)
    if not target.exists():
        raise click.ClickException(f"no board at {target}")
    try:
        remove_entry(target, entry_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"removed {entry_id} from {target}")


__all__ = ["board_grp", "add_cmd", "list_cmd", "remove_cmd"]
