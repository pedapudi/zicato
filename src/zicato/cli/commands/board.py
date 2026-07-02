"""``zicato board`` — CLI subcommand group for board file operations.

ADVANCED / DEBUGGING — off the happy path. The board is part of the
evaluation contract; the operator's *live* board edits are picked up
by ``zicato evolve`` automatically (a board change rolls the epoch).
Use ``zicato board`` to inspect or hand-edit a frozen per-epoch board.

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


@click.group(
    name="board",
    short_help="Advanced: inspect / hand-edit a per-epoch board.jsonl.",
)
@click.pass_context
def board_grp(ctx: click.Context) -> None:
    """Advanced: manage the per-epoch board.jsonl file.

    Off the happy path. The board is part of the evaluation contract,
    and `zicato evolve` rolls the epoch when the live board changes —
    use this group only to inspect or hand-edit a frozen board.
    """
    # No shared state — every subcommand resolves the board path on
    # its own. The pass_context wiring exists so the integration CLI
    # can stash global flags (workspace overrides, verbosity) on
    # ``ctx.obj`` when it composes this group under the top-level CLI.
    ctx.ensure_object(dict)


@board_grp.command(
    "add",
    short_help="Append one validated board entry from a JSON file.",
)
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
        raise click.ClickException(f"{src}: expected a JSON object, got {type(payload).__name__}")
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


@board_grp.command(
    "list",
    short_help="List the entries in the current epoch's board.",
)
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


@board_grp.command(
    "remove",
    short_help="Remove a board entry by id from the current epoch.",
)
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


@board_grp.command(
    "audit",
    short_help="Measure the board's A/A noise floor for the current epoch.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
@click.option(
    "--epoch",
    "epoch_id",
    default=None,
    help="Epoch to audit (default: the current epoch).",
)
@click.option(
    "--runs",
    default=5,
    show_default=True,
    type=click.IntRange(min=2),
    help="How many independent A/A draws of the champion to take.",
)
@click.option(
    "--harness-call-llm",
    "harness_dotted",
    required=True,
    help="Dotted import path of the harness call_llm (e.g. mymodule:harness).",
)
@click.option(
    "--auxiliary-call-llm",
    "auxiliary_dotted",
    required=True,
    help="Dotted import path of the auxiliary call_llm (e.g. mymodule:aux).",
)
def audit_cmd(
    workspace: str,
    epoch_id: str | None,
    runs: int,
    harness_dotted: str,
    auxiliary_dotted: str,
) -> None:
    """Measure the evaluation's A/A noise floor and record it on the epoch.

    Runs the current champion against ITSELF --runs times (fresh draws
    through the same board-unit workers every duel uses) and reports the
    delta_scalar spread — the smallest difference the board can actually
    resolve. The measured floor is persisted onto the epoch record
    (config.json's noise_floor field) so `zicato evolve` can warn when
    promote_margin is below it while the evidence gate is off.
    """
    import asyncio  # noqa: PLC0415

    from zicato import adapter_factory, runtime_factory, workspace_loader  # noqa: PLC0415
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415
    from zicato.cli.commands.evolve import _import_callable  # noqa: PLC0415
    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        current_epoch_id,
        load_epoch,
        set_epoch_noise_floor,
    )
    from zicato.tournament.calibration import measure_noise_floor  # noqa: PLC0415

    workspace_root = Path(workspace).resolve()
    resolved_epoch = epoch_id or current_epoch_id(workspace_root)
    if not resolved_epoch:
        raise click.ClickException(
            f"no current epoch under {workspace_root}; run `zicato evolve` "
            "(or `zicato epoch new`) first, or pass --epoch"
        )

    harness_call_llm = _import_callable(harness_dotted, kind="harness_call_llm")
    auxiliary_call_llm = _import_callable(auxiliary_dotted, kind="auxiliary_call_llm")

    try:
        epoch_cfg = load_epoch(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    board_file = board_path(workspace_root, resolved_epoch)
    if not board_file.exists():
        raise click.ClickException(f"no board at {board_file}")
    board, disable_drift, judge_only = load_board_with_meta(board_file)

    workspace_config = workspace_loader.load_workspace_config(workspace_root)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
    )

    # Resolve the current champion generation through the same seams the
    # orchestrator uses (marker file / highest vN + the GenerationStore).
    from zicato.orchestrator import (  # noqa: PLC0415
        _resolve_current_generation,
        _snapshot_root,
    )

    try:
        champion_id = _resolve_current_generation(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"{exc} — run at least one `zicato evolve` round (or seed a "
            "baseline) before auditing the board"
        ) from exc
    from zicato.core.types import Generation  # noqa: PLC0415

    champion = Generation(
        id=champion_id,
        epoch_id=resolved_epoch,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace_root, resolved_epoch, champion_id),
        created_at="",
        promoted=True,
    )

    floor = asyncio.run(
        measure_noise_floor(
            adapter=adapter,
            generation=champion,
            board=board,
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch,
            runs=runs,
            disable_drift=disable_drift,
            judge_only=judge_only,
        )
    )
    set_epoch_noise_floor(workspace_root, resolved_epoch, floor.to_json())

    margin = epoch_cfg.scoring.promote_margin
    click.echo(f"A/A noise floor for {resolved_epoch} ({champion_id}, {runs} draws):")
    click.echo(f"  scalars:        {', '.join(f'{s:.6g}' for s in floor.scalars)}")
    click.echo(f"  max |delta|:    {floor.max_abs_delta:.6g}")
    click.echo(f"  delta std:      {floor.delta_std:.6g}")
    click.echo(f"  promote_margin: {margin:.6g}")
    if margin < floor.max_abs_delta:
        click.echo(
            "  WARNING: promote_margin is BELOW the measured noise floor — "
            "duels decided by the margin alone cannot distinguish a real "
            "improvement from a re-roll. Raise promote_margin or keep the "
            "evidence gate (promote_confidence_threshold) on.",
            err=True,
        )
    else:
        click.echo("  promote_margin clears the measured floor.")


@board_grp.command(
    "preflight",
    short_help="Contract pre-flight: prove the board can out-signal its noise.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
@click.option(
    "--epoch",
    "epoch_id",
    default=None,
    help="Epoch to pre-flight (default: the current epoch).",
)
@click.option(
    "--runs",
    default=5,
    show_default=True,
    type=click.IntRange(min=2),
    help="How many independent A/A draws of the champion to take.",
)
@click.option(
    "--harness-call-llm",
    "harness_dotted",
    required=True,
    help="Dotted import path of the harness call_llm (e.g. mymodule:harness).",
)
@click.option(
    "--auxiliary-call-llm",
    "auxiliary_dotted",
    required=True,
    help="Dotted import path of the auxiliary call_llm (e.g. mymodule:aux).",
)
def preflight_cmd(
    workspace: str,
    epoch_id: str | None,
    runs: int,
    harness_dotted: str,
    auxiliary_dotted: str,
) -> None:
    """Measure the contract's noise floor AND achievable signal; verdict.

    Board-reflection v1. Two measurements: (a) the A/A noise floor —
    the champion duels ITSELF --runs times (same draws `zicato board
    audit` takes); (b) the scripted-perturbation duel — the champion vs
    a deliberately-degraded ephemeral copy of itself (the FIRST
    enumerated mutation point blanked/scrambled in a scratch tree; the
    real lineage is never touched). Verdict: REFUSE-recommended when the
    achievable signal is at/below the floor; WARN when every probe
    scored identically (a saturated contract — the 1.000000 signature);
    OK otherwise. Recommend-only — never gates. The verdict persists
    onto the epoch record and flows into the per-round health report.
    """
    import asyncio  # noqa: PLC0415

    from zicato import adapter_factory, runtime_factory, workspace_loader  # noqa: PLC0415
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415
    from zicato.cli.commands.evolve import _import_callable  # noqa: PLC0415
    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        current_epoch_id,
        load_epoch,
        set_epoch_noise_floor,
        set_epoch_preflight,
    )
    from zicato.epoch.preflight import (  # noqa: PLC0415
        VERDICT_REFUSE,
        VERDICT_WARN,
        run_contract_preflight,
    )

    workspace_root = Path(workspace).resolve()
    resolved_epoch = epoch_id or current_epoch_id(workspace_root)
    if not resolved_epoch:
        raise click.ClickException(
            f"no current epoch under {workspace_root}; run `zicato evolve` "
            "(or `zicato epoch new`) first, or pass --epoch"
        )

    harness_call_llm = _import_callable(harness_dotted, kind="harness_call_llm")
    auxiliary_call_llm = _import_callable(auxiliary_dotted, kind="auxiliary_call_llm")

    try:
        epoch_cfg = load_epoch(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    board_file = board_path(workspace_root, resolved_epoch)
    if not board_file.exists():
        raise click.ClickException(f"no board at {board_file}")
    board, disable_drift, judge_only = load_board_with_meta(board_file)

    workspace_config = workspace_loader.load_workspace_config(workspace_root)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace_root,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
    )

    from zicato.orchestrator import (  # noqa: PLC0415
        _resolve_current_generation,
        _snapshot_root,
    )

    try:
        champion_id = _resolve_current_generation(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"{exc} — run at least one `zicato evolve` round (or seed a "
            "baseline) before pre-flighting the contract"
        ) from exc
    from zicato.core.types import Generation  # noqa: PLC0415

    champion = Generation(
        id=champion_id,
        epoch_id=resolved_epoch,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace_root, resolved_epoch, champion_id),
        created_at="",
        promoted=True,
    )

    try:
        report, floor = asyncio.run(
            run_contract_preflight(
                adapter=adapter,
                generation=champion,
                board=board,
                weights=epoch_cfg.scoring,
                config=config,
                workspace_root=workspace_root,
                epoch_id=resolved_epoch,
                runs=runs,
                disable_drift=disable_drift,
                judge_only=judge_only,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    set_epoch_preflight(workspace_root, resolved_epoch, report.to_json())
    if load_epoch(workspace_root, resolved_epoch).noise_floor is None:
        set_epoch_noise_floor(workspace_root, resolved_epoch, floor.to_json())

    margin = epoch_cfg.scoring.promote_margin
    click.echo(f"Contract pre-flight for {resolved_epoch} ({champion_id}, {runs} A/A draws):")
    click.echo(f"  A/A scalars:       {', '.join(f'{s:.6g}' for s in report.champion_scalars)}")
    click.echo(f"  noise floor:       {report.noise_floor_max_abs_delta:.6g} (max |delta|)")
    click.echo(
        f"  degraded point:    {report.degraded_mutation_id} "
        f"({report.degraded_mutation_kind} @ {report.degraded_file})"
    )
    click.echo(f"  degraded scalar:   {report.degraded_scalar:.6g}")
    click.echo(f"  achievable signal: {report.signal:.6g}")
    click.echo(f"  promote_margin:    {margin:.6g}")
    if report.verdict == VERDICT_REFUSE:
        click.echo("  verdict:           REFUSE-recommended (signal <= noise floor)")
        click.echo(
            "  WARNING: the contract's achievable signal does not clear its own "
            "A/A noise floor — a deliberate degradation moves the scalar no "
            "more than a re-roll of the same tree does, so duels are decided "
            "by noise. Reduce evaluation noise (more replicates, steadier "
            "judges) or strengthen the board before running rounds. "
            "Recommend-only: nothing is blocked.",
            err=True,
        )
    elif report.verdict == VERDICT_WARN:
        click.echo("  verdict:           WARN (saturated contract)")
        click.echo(
            "  WARNING: scalar spread was exactly zero across every probe — "
            "even a deliberately-degraded tree scored identically to the "
            "champion (the 1.000000 saturation signature). Add expectations / "
            "strengthen judges so the board can discriminate candidates. "
            "Recommend-only: nothing is blocked.",
            err=True,
        )
    else:
        click.echo("  verdict:           OK (signal clears the measured floor)")


__all__ = ["board_grp", "add_cmd", "audit_cmd", "list_cmd", "preflight_cmd", "remove_cmd"]
