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
    from zicato.evolve.generation_phase import (  # noqa: PLC0415
        current_generation,
        snapshot_root,
    )

    try:
        champion_id = current_generation(workspace_root, resolved_epoch)
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
        snapshot_root=snapshot_root(workspace_root, resolved_epoch, champion_id),
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
    "--degrade-mutation-id",
    "degrade_mutation_id",
    default=None,
    help=(
        "Degrade exactly this mutation point instead of the automatic "
        "role-diverse sample (use when you know which point carries the "
        "contract's signal)."
    ),
)
@click.option(
    "--probe-points",
    "probe_points",
    default=None,
    type=click.IntRange(min=1),
    help=(
        "Ceiling on how many mutation points the automatic sample degrades "
        "[default: runtime.preflight_probe_points]. Probing stops early once "
        "the verdict is settled, so this rarely costs the full count."
    ),
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
    degrade_mutation_id: str | None,
    probe_points: int | None,
    harness_dotted: str,
    auxiliary_dotted: str,
) -> None:
    """Measure the contract's noise floor AND degradation signal; verdict.

    Board-reflection v1. Two measurements: (a) the A/A noise floor —
    the champion duels ITSELF --runs times (same draws `zicato board
    audit` takes); (b) the scripted-perturbation duels — the champion vs
    deliberately-degraded ephemeral copies of itself (a deterministic,
    role-diverse sample of mutation points blanked/scrambled in scratch
    trees; the real lineage is never touched), reporting the MAX signal
    so one inert point cannot veto a healthy contract. Verdicts:
    REFUSE-recommended when the measured signal is at/below the floor;
    WARN when every probe scored identically (a saturated contract — the
    1.000000 signature); INERT when the probes moved nothing while the
    A/A draws varied (the signal is unmeasured, not zero — pick a
    representative point); OK otherwise. Also places promote_margin against
    the floor and the measured signal and names the side it fell outside of
    — WARNING-class only, since the upper number is DEGRADATION headroom and
    does not bound what a challenger can improve (issue #119).
    Recommend-only — never gates. The verdict persists onto the epoch
    record and flows into the per-round health report.
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
        VERDICT_INERT,
        VERDICT_REFUSE,
        VERDICT_WARN,
        WINDOW_EMPTY,
        WINDOW_MARGIN_ABOVE_ACHIEVABLE,
        WINDOW_MARGIN_BELOW_FLOOR,
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

    from zicato.evolve.generation_phase import (  # noqa: PLC0415
        current_generation,
        snapshot_root,
    )

    try:
        champion_id = current_generation(workspace_root, resolved_epoch)
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
        snapshot_root=snapshot_root(workspace_root, resolved_epoch, champion_id),
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
                degrade_mutation_id=degrade_mutation_id,
                probe_points=probe_points,
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
    # Every probe, not just the winner: an operator judging whether a REFUSE is
    # about the board or about the sample needs to see an inert point next to a
    # live one (issue #106).
    for probe in report.probed_points:
        if probe.skipped:
            detail = f"skipped ({probe.skipped})"
        else:
            detail = f"signal {probe.signal:.6g} (scalar {probe.degraded_scalar:.6g})"
        click.echo(
            f"  probe:             {probe.mutation_id} [{probe.role or probe.kind}] {detail}"
        )
    click.echo(
        f"  best point:        {report.degraded_mutation_id} "
        f"({report.degraded_mutation_kind} @ {report.degraded_file})"
    )
    click.echo(f"  degraded scalar:   {report.degraded_scalar:.6g}")
    click.echo(f"  degradation signal:{report.signal:.6g}")
    click.echo(f"  promote_margin:    {margin:.6g}")
    if report.recommended_margin is not None:
        click.echo(f"  recommended margin:{report.recommended_margin:.6g} (from delta_std)")
    if report.verdict == VERDICT_REFUSE:
        click.echo("  verdict:           REFUSE-recommended (signal <= noise floor)")
        click.echo(
            "  WARNING: the contract's measured signal does not clear its own "
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
    elif report.verdict == VERDICT_INERT:
        click.echo("  verdict:           INERT probe (the signal is UNMEASURED)")
        click.echo(
            "  WARNING: every probed mutation point left the scalar exactly at "
            "the champion mean while the A/A draws did vary — the probe moved "
            "nothing, so the signal is unmeasured rather than zero. "
            "This is NOT evidence against the contract: a point can be inert "
            "because the contract routes around it (e.g. a tool description no "
            "longer reached once a structured-output schema produces the "
            "deliverable). Re-run with --degrade-mutation-id naming a point the "
            "deliverable demonstrably depends on, or raise --probe-points.",
            err=True,
        )
    else:
        click.echo("  verdict:           OK (signal clears the measured floor)")
    # The promote_margin window (issue #112) — a separate question from
    # signal-vs-noise. Every outcome here is a WARNING: the upper comparison is
    # against DEGRADATION headroom, which bounds a challenger's improvement from
    # neither side (issue #119).
    if report.window_failure == WINDOW_MARGIN_ABOVE_ACHIEVABLE:
        click.echo("  window:            WARN (margin above the degradation signal)")
        click.echo(
            f"  WARNING: promote_margin {margin:.6g} is at or above the measured "
            f"degradation signal {report.signal:.6g}. Read that comparison "
            "carefully: what the probe measured is how far the scalar moved when a "
            "mutation point was DESTROYED — how much this champion has left to "
            "LOSE — while a promotion needs movement the other way. The two are "
            "unrelated in general, and a champion sitting near the failing end has "
            "little left to break and plenty to gain. So this is a reason to check "
            "promote_margin against what a real fix on this board is worth; it is "
            "NOT evidence that nothing can promote. Improvement headroom is "
            "UNMEASURED. The margin does still need to clear the noise floor "
            f"({report.noise_floor_max_abs_delta:.6g}), which is measured honestly. "
            "The probe also degrades ONE point at a time, so it under-reports even "
            "the movement it does measure.",
            err=True,
        )
    elif report.window_failure == WINDOW_MARGIN_BELOW_FLOOR:
        click.echo("  window:            WARN (margin below the noise floor)")
        click.echo(
            f"  WARNING: promote_margin {margin:.6g} is at or below the measured "
            "noise floor — promotions cannot be distinguished from re-rolls of "
            "the same generation. Raise it above the noise (the recommended "
            "margin above scales delta_std, which does not drift upward as "
            "calibration draws accumulate) and/or keep the evidence gate on.",
            err=True,
        )
    elif report.window_failure == WINDOW_EMPTY:
        click.echo("  window:            EMPTY (no promote_margin is defensible)")
        click.echo(
            "  WARNING: the measured signal does not clear the noise floor, so a "
            "deliberate degradation moves the scalar no more than a re-roll does. "
            "Do NOT tune promote_margin; no value of it is defensible on a board "
            "whose measurable movement is inside its own noise. Reduce evaluation "
            "noise or strengthen the board.",
            err=True,
        )
    else:
        click.echo(
            "  window:            OK "
            f"({report.noise_floor_max_abs_delta:.6g} < {margin:.6g} < {report.signal:.6g})"
        )
    # The holdout's SECOND bound (issue #118). Prose, printed whenever the
    # split is active and either holdout bound looks infeasible — the window
    # lines above only ever placed the TRAIN margin, so a board rejected by the
    # holdout confirmation had nothing here to explain it.
    if report.holdout_note:
        click.echo(f"  holdout:           {report.holdout_note}", err=True)


@board_grp.command(
    "judges",
    short_help="List the board's judges; --test-retest measures reliability.",
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
    help="Epoch whose board to inspect (default: the current epoch).",
)
@click.option(
    "--test-retest",
    "run_retest",
    is_flag=True,
    default=False,
    help="Judge a frozen transcript k times per judge and report disagreement.",
)
@click.option(
    "--retest-k",
    default=3,
    show_default=True,
    type=click.IntRange(min=2),
    help="How many times each judge re-judges the same frozen transcript.",
)
@click.option(
    "--threshold",
    default=0.25,
    show_default=True,
    type=click.FloatRange(min=0.0, max=1.0),
    help="Pairwise disagreement rate above which a judge is flagged noisy.",
)
@click.option(
    "--transcript",
    "transcript_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Frozen transcript file to re-judge (e.g. a settled reasoning trace "
        "saved from a prior run's events). Default: a synthetic fixture "
        "transcript."
    ),
)
@click.option(
    "--auxiliary-call-llm",
    "auxiliary_dotted",
    default=None,
    help=(
        "Dotted import path of the judge/aux call_llm (e.g. mymodule:aux). "
        "Required with --test-retest — inline judges are LLM-backed."
    ),
)
def judges_cmd(
    workspace: str,
    epoch_id: str | None,
    run_retest: bool,
    retest_k: int,
    threshold: float,
    transcript_path: str | None,
    auxiliary_dotted: str | None,
) -> None:
    """List the board's declared process judges; optionally retest them.

    Without --test-retest: print every judge the board declares (name,
    mode, severity, criterion/dotted-path). With --test-retest: build
    each judge through the same runtime bridge real runs use and judge
    ONE frozen transcript --retest-k times; report the per-judge
    test-retest disagreement rate. A judge that disagrees with itself on
    identical input injects pure noise into every custom:<judge_name>
    drift count it produces — the fix is a lower per_judge_weights entry
    or a sharper criterion. Recommend-only; nothing is gated.
    """
    import asyncio  # noqa: PLC0415

    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
    from zicato.judge_runtime.reliability import (  # noqa: PLC0415
        FIXTURE_TRANSCRIPT,
        declared_judge_specs,
        test_retest_board,
    )

    workspace_root = Path(workspace).resolve()
    resolved_epoch = epoch_id or current_epoch_id(workspace_root)
    board_file = (
        board_path(workspace_root, resolved_epoch)
        if resolved_epoch
        else _resolve_board_path(workspace)
    )
    if not board_file.exists():
        raise click.ClickException(f"no board at {board_file}")
    try:
        entries = load_board(board_file)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    specs = declared_judge_specs(entries)
    if not specs:
        click.echo(f"(no judges declared on {board_file})")
        return

    click.echo(f"{board_file} — {len(specs)} declared judge(s)")
    for spec in specs:
        body = str(spec.body).replace("\n", " ")
        if len(body) > 60:
            body = body[:57] + "..."
        click.echo(f"  {spec.name}\t{spec.mode}\tseverity={spec.severity}\t{body}")
    if not run_retest:
        return

    if not auxiliary_dotted:
        raise click.ClickException(
            "--test-retest needs --auxiliary-call-llm (inline judges are LLM-backed)"
        )
    from zicato.cli.commands.evolve import _import_callable  # noqa: PLC0415

    aux_call_llm = _import_callable(auxiliary_dotted, kind="auxiliary_call_llm")
    transcript = (
        Path(transcript_path).read_text(encoding="utf-8") if transcript_path else FIXTURE_TRANSCRIPT
    )

    reliabilities = asyncio.run(test_retest_board(entries, transcript, aux_call_llm, k=retest_k))

    click.echo(f"\nTest-retest over one frozen transcript (k={retest_k} per judge):")
    for rel in reliabilities:
        marks = "".join("V" if v else "." for v in rel.verdicts) + "!" * rel.errors
        click.echo(
            f"  {rel.judge_name}\tfired {rel.fired}/{rel.k} [{marks}]\t"
            f"disagreement={rel.disagreement_rate:.0%}"
        )
        # A judge that RAISED measured nothing on those calls; without this the
        # probe reports it as a perfectly self-consistent judge that never
        # fired, and the operator goes looking at the board (issue #121).
        if rel.errors:
            click.echo(
                f"    WARNING: the judge's callable RAISED on {rel.errors}/{rel.k} "
                "calls — check the judge/auxiliary endpoint and model config; "
                "this probe measured only the calls that answered.",
                err=True,
            )

    from zicato.health.diagnostics import detect_noisy_judge  # noqa: PLC0415

    findings = detect_noisy_judge(reliabilities, threshold=threshold)
    if not findings:
        click.echo(f"  every judge is self-consistent at threshold {threshold:.0%}.")
        return
    for finding in findings:
        click.echo(f"  WARNING [{finding.code}]: {finding.summary}", err=True)
        recommendation = finding.detail.get("recommendation")
        if recommendation:
            click.echo(f"    recommendation: {recommendation}", err=True)


__all__ = [
    "board_grp",
    "add_cmd",
    "audit_cmd",
    "judges_cmd",
    "list_cmd",
    "preflight_cmd",
    "remove_cmd",
]
