"""``zicato evolve`` — run the evolve loop for N rounds against the current epoch.

This command is the operator-facing entry point to the round-by-round
self-improvement loop. Each round proposes one experiment, applies it,
runs the tournament, and either promotes or rejects the child
generation. See :mod:`zicato.orchestrator` for the implementation
details.

Usage::

    zicato evolve --rounds 4 \\
        --harness-call-llm my_pkg.llms:harness_call_llm \\
        --auxiliary-call-llm my_pkg.llms:aux_call_llm

The ``--mode`` flag picks between full A/B tournaments and inline
fast-mode keep/discard. The default is ``full``; fast mode reads the
parent's cached aggregate from ``gen_score.json`` and requires that a
prior full-mode round wrote that file.

The two ``--*-call-llm`` options accept dotted import paths in either
``pkg.mod:attr`` or ``pkg.mod.attr`` form — the same convention the
runtime factory uses everywhere else in the tree.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
from pathlib import Path
from typing import Any

import click


def _resolve_supervisor_binary() -> Path | None:
    """Return the path to ``zicato-supervisor`` or ``None`` if unavailable.

    Resolution order:

    1. Environment override ``ZICATO_SUPERVISOR_BINARY`` (useful for tests
       that point at a sentinel script).
    2. The in-tree release build relative to this source file. This is
       the path produced by ``cargo build --release`` and is the default
       distribution mode for development checkouts.
    3. The system ``PATH`` (``zicato-supervisor`` installed globally).

    Returns ``None`` when nothing resolves — the caller prints a warning
    and proceeds without a dashboard.
    """
    import os  # noqa: PLC0415

    env_override = os.environ.get("ZICATO_SUPERVISOR_BINARY")
    if env_override:
        candidate = Path(env_override)
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate

    # In-tree path: this file is at zicato/cli/commands/evolve.py; the
    # binary lives at <repo_root>/supervisor/target/release/zicato-supervisor.
    here = Path(__file__).resolve()
    in_tree = (
        here.parent.parent.parent.parent / "supervisor" / "target" / "release" / "zicato-supervisor"
    )
    if in_tree.exists() and os.access(in_tree, os.X_OK):
        return in_tree

    on_path = shutil.which("zicato-supervisor")
    if on_path:
        return Path(on_path)

    return None


async def _maybe_spawn_supervisor(
    workspace_root: Path,
    port: int,
    bind: str,
    disabled: bool,
) -> asyncio.subprocess.Process | None:
    """Spawn the supervisor binary as a subprocess (or return ``None``).

    The binary's stdout/stderr are inherited from the parent so log
    output appears alongside ``zicato evolve``'s own messages. On
    failure-to-spawn the function still returns ``None`` and prints a
    warning — ``evolve`` continues without a dashboard rather than
    refusing to run.
    """
    if disabled:
        return None
    binary = _resolve_supervisor_binary()
    if binary is None:
        click.echo(
            "warning: zicato-supervisor binary not found; dashboard disabled",
            err=True,
        )
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--workspace",
            str(workspace_root),
            "--port",
            str(port),
            "--bind",
            bind,
        )
    except (OSError, FileNotFoundError) as exc:
        click.echo(
            f"warning: failed to spawn zicato-supervisor ({exc}); dashboard disabled",
            err=True,
        )
        return None
    click.echo(f"Dashboard: http://{bind}:{port}")
    return proc


async def _terminate_supervisor(proc: asyncio.subprocess.Process | None) -> None:
    """Shut down a previously-spawned supervisor; idempotent."""
    if proc is None:
        return
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


def _import_callable(dotted: str, *, kind: str) -> Any:
    """Resolve ``pkg.mod:attr`` or ``pkg.mod.attr`` to a callable.

    Mirrors :func:`zicato.runtime_factory._import_callable`. Duplicated
    here so this CLI module imports stay small (the runtime factory is
    imported by the orchestrator anyway).
    """
    if ":" in dotted:
        module_path, _, attr = dotted.partition(":")
    else:
        module_path, _, attr = dotted.rpartition(".")
    if not module_path or not attr:
        raise click.BadParameter(
            f"{kind} dotted path {dotted!r} must be 'pkg.module.attr' or " "'pkg.module:attr'"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise click.BadParameter(f"{kind}: could not import module {module_path!r}: {exc}") from exc
    if not hasattr(module, attr):
        raise click.BadParameter(f"{kind}: module {module_path!r} has no attribute {attr!r}")
    fn = getattr(module, attr)
    if not callable(fn):
        raise click.BadParameter(
            f"{kind}: {dotted!r} resolved to {type(fn).__name__}, " "expected a callable"
        )
    return fn


@click.command(name="evolve")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root.",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's current epoch.",
)
@click.option(
    "--rounds",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of evolve rounds to attempt.",
)
@click.option(
    "--mode",
    type=click.Choice(["full", "fast"]),
    default="full",
    show_default=True,
    help="full = run both parent + child; fast = child vs cached parent aggregate.",
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
@click.option(
    "--max-consecutive-rejections",
    default=3,
    show_default=True,
    type=click.IntRange(min=1),
    help="Stop early when this many rounds in a row are rejected.",
)
@click.option(
    "--max-wall-clock-seconds",
    default=None,
    type=click.IntRange(min=1),
    envvar="ZICATO_MAX_WALL_CLOCK_SECONDS",
    help=(
        "Total wall-clock budget for this whole evolve invocation, in "
        "seconds. The loop stops cleanly between rounds once the budget "
        "is spent, and a single round that would overrun it is cancelled "
        "and recorded as aborted. Unset (the default) leaves the loop "
        "unbounded. Applies on top of each board entry's own "
        "wall_clock_budget_seconds. Env var: ZICATO_MAX_WALL_CLOCK_SECONDS."
    ),
)
@click.option(
    "--no-auto-epoch",
    is_flag=True,
    default=False,
    help=(
        "Disable contract-hash auto-epoching. With this flag, evolve "
        "errors out (instead of rolling the epoch) when the evaluation "
        "contract has drifted from the current epoch."
    ),
)
@click.option(
    "--epoch-name",
    default=None,
    help=(
        "Name for an auto-created epoch (default: the e{N} scheme). "
        "Ignored when --epoch is passed or no new epoch is created."
    ),
)
@click.option(
    "--no-dashboard",
    is_flag=True,
    default=False,
    help="Do not spawn the supervisor binary / dashboard server.",
)
@click.option(
    "--dashboard-port",
    default=7892,
    show_default=True,
    type=click.IntRange(min=1, max=65535),
    help="Preferred port for the dashboard HTTP server.",
)
@click.option(
    "--dashboard-bind",
    default="127.0.0.1",
    show_default=True,
    help="Bind address for the dashboard HTTP server.",
)
def evolve_cmd(
    workspace: str,
    epoch: str | None,
    rounds: int,
    mode: str,
    harness_dotted: str,
    auxiliary_dotted: str,
    max_consecutive_rejections: int,
    max_wall_clock_seconds: int | None,
    no_auto_epoch: bool,
    epoch_name: str | None,
    no_dashboard: bool,
    dashboard_port: int,
    dashboard_bind: str,
) -> None:
    """Run the evolve loop for N rounds against the current epoch.

    By default, contract-hash auto-epoching is ON: when the evaluation
    contract (board / rubric / scoring / inner-harness identity) has
    drifted, evolve closes the current epoch and opens a fresh one
    before running. Pass ``--no-auto-epoch`` for the strict behaviour
    (error on drift instead of rolling). ``--epoch`` skips auto-epoching
    entirely.
    """
    workspace_root = Path(workspace).resolve()

    harness_call_llm = _import_callable(harness_dotted, kind="harness_call_llm")
    auxiliary_call_llm = _import_callable(auxiliary_dotted, kind="auxiliary_call_llm")

    # Lazy import — the orchestrator is heavy. We keep it out of
    # `zicato --help` time.
    from zicato.orchestrator import evolve_n_rounds  # noqa: PLC0415

    # ``evolve_n_rounds`` appends a single symbolic terminal-reason
    # string here so the summary below can name exactly why the loop
    # ended.
    stop_reason_out: list[str] = []

    async def _run() -> list[Any]:
        sup = await _maybe_spawn_supervisor(
            workspace_root,
            dashboard_port,
            dashboard_bind,
            disabled=no_dashboard,
        )
        try:
            return await evolve_n_rounds(
                rounds=rounds,
                workspace_root=workspace_root,
                epoch_id=epoch,
                harness_call_llm=harness_call_llm,
                auxiliary_call_llm=auxiliary_call_llm,
                fast_mode=(mode == "fast"),
                max_consecutive_rejections=max_consecutive_rejections,
                max_wall_clock_seconds=max_wall_clock_seconds,
                auto_epoch=not no_auto_epoch,
                epoch_name=epoch_name,
                stop_reason_out=stop_reason_out,
            )
        finally:
            await _terminate_supervisor(sup)

    try:
        outcomes = asyncio.run(_run())
    except (FileNotFoundError, RuntimeError) as exc:
        # FileNotFoundError: missing config / epoch marker.
        # RuntimeError: contract drift under --no-auto-epoch, or a
        # missing baseline. Both are operator-actionable; surface them
        # as a clean CLI error rather than a traceback.
        raise click.ClickException(str(exc)) from exc

    # Final summary line — say explicitly why the loop ended. The
    # total wall-clock budget stop is called out distinctly from "all
    # rounds done" and from the consecutive-reject early-stop.
    stop_reason = stop_reason_out[0] if stop_reason_out else "completed"
    ran = len(outcomes)
    if stop_reason == "wall_clock_budget_between_rounds":
        click.echo(
            f"evolve: stopped on the total wall-clock budget of "
            f"{max_wall_clock_seconds}s — ran {ran} of {rounds} requested "
            f"rounds before the budget was spent.",
            err=True,
        )
    elif stop_reason == "wall_clock_budget_mid_round":
        click.echo(
            f"evolve: stopped on the total wall-clock budget of "
            f"{max_wall_clock_seconds}s — round {ran} was cancelled "
            f"mid-flight (recorded as aborted) because finishing it would "
            f"have overrun the budget; ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    elif stop_reason == "consecutive_rejections":
        click.echo(
            f"evolve: stopped early after {max_consecutive_rejections} "
            f"consecutive rejections — ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    elif stop_reason == "degenerate_health":
        click.echo(
            f"evolve: stopped early on a degenerate loop-health finding — "
            f"ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    else:
        click.echo(f"evolve: completed all {ran} requested rounds.", err=True)

    payload = [
        {
            "parent_generation_id": o.parent_generation_id,
            "proposed_generation_id": o.proposed_generation_id,
            "tournament_decision": o.tournament_decision,
            "rejection_reason": o.rejection_reason,
            "parent_scalar": o.parent_scalar,
            "child_scalar": o.child_scalar,
            "delta_scalar": o.delta_scalar,
        }
        for o in outcomes
    ]
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["evolve_cmd"]
