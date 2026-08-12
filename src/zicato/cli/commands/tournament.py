"""``zicato tournament`` — run a tournament between two generations.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` runs a
tournament every round as part of the loop. Run ``zicato tournament``
by hand only to re-score a specific PARENT/CHILD pair in isolation.

Two modes:

* ``full`` (default) — runs every board entry under both PARENT and
  CHILD generations and applies the promote gate against the live A/B
  comparison.
* ``fast`` — runs only the child and applies the gate against the
  parent's historical aggregate as cached in the workspace.

Usage::

    zicato tournament PARENT CHILD [--workspace .zicato] [--epoch ID] \\
        [--mode full|fast]

PARENT and CHILD are generation ids under the (resolved) epoch — the
default-epoch resolution follows the workspace's ``current_epoch``
marker when ``--epoch`` is omitted.

This command is a thin shell over :func:`zicato.tournament.run_tournament`
and :func:`zicato.tournament.run_fast_mode`: it resolves paths,
loads the frozen artifacts (board, scoring weights, generations),
constructs the :class:`RuntimeConfig`, and prints the
:class:`GateOutcome` as JSON. The shell DOES NOT itself touch
goldfive or the inner harness — that wiring is owned by the
:class:`HarnessAdapter` factory the workspace resolves to.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
from pathlib import Path
from typing import Any

import click

from zicato.core.types import Generation
from zicato.core.workspace import generation_dir


@click.command(
    name="tournament",
    short_help="Advanced: run a tournament between two generations in isolation.",
)
@click.argument("parent")
@click.argument("child")
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace root.",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's current epoch.",
)
@click.option(
    "--mode",
    type=click.Choice(["full", "fast"]),
    default="full",
    show_default=True,
    help="full = run both generations; fast = child vs parent's historical aggregate.",
)
@click.option(
    "--skip-regression",
    is_flag=True,
    help="Skip the regression-suite gate even when enabled in scoring.",
)
@click.option(
    "--replicates",
    type=int,
    default=None,
    help=(
        "Debug override for the per-duel replicate count. Defaults to the "
        "contract's resolved structure value (what `zicato evolve` uses) — "
        "pass this only to force a different count for this one invocation."
    ),
)
def tournament_cmd(
    parent: str,
    child: str,
    workspace: str,
    epoch: str | None,
    mode: str,
    skip_regression: bool,
    replicates: int | None,
) -> None:
    """Advanced: run a tournament between PARENT and CHILD generations.

    Off the happy path — `zicato evolve` runs the tournament every
    round. Use this only to re-score a specific generation pair.
    """
    workspace_root = Path(workspace).resolve()

    loader, adapter_factory, runtime_factory = _resolve_workspace_components()

    try:
        workspace_config = loader.load_workspace_config(workspace_root)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved_epoch_id = epoch or _resolve_epoch_id(workspace_root)
    try:
        board, disable_drift, judge_only = loader.load_current_board_with_meta(workspace_root)
        weights = loader.load_current_scoring(workspace_root)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    # ``--skip-regression`` is a per-invocation override; flip the
    # weights' opt-in flag off so the runner takes the fast path.
    if skip_regression and weights.regression_gate_enabled:
        weights = dataclasses.replace(weights, regression_gate_enabled=False)

    parent_gen = _build_generation(workspace_root, resolved_epoch_id, parent)
    child_gen = _build_generation(workspace_root, resolved_epoch_id, child)

    try:
        adapter = adapter_factory.make_adapter_from_config(workspace_config)
        config = runtime_factory.make_runtime_config(
            workspace_config, workspace_root=workspace_root
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Resolve replicates exactly the way ``evolve_once`` does (orchestrator.py)
    # so a debug re-score matches the live loop's numbers for the same
    # contract: build the strategy from the contract's tournament structure
    # and read its resolved ``.replicates()`` — never the bare default of 1
    # ``run_tournament`` / ``run_fast_mode`` fall back to on their own.
    # ``--replicates`` is an explicit per-invocation override on top of that.
    from zicato.selection.registry import make_strategy  # noqa: PLC0415
    from zicato.tournament import run_fast_mode, run_tournament  # noqa: PLC0415

    strategy = make_strategy(
        weights.tournament_structure,
        board_ids=[e.id for e in board],
        board_tags={e.id: e.tags for e in board},
    )
    resolved_replicates = replicates if replicates is not None else strategy.replicates()

    if mode == "full":
        result = asyncio.run(
            run_tournament(
                adapter=adapter,
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=board,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=resolved_epoch_id,
                disable_drift=disable_drift,
                judge_only=judge_only,
                # ``--mode full`` re-samples BOTH sides for noise (it bypasses
                # the cache by design); force-fresh the champion too rather
                # than reusing its cached per-board units.
                champion_force_fresh=True,
                replicates=resolved_replicates,
            )
        )
    else:
        parent_historical = _load_historical_aggregate(
            workspace_root, resolved_epoch_id, parent_gen.id
        )
        result = asyncio.run(
            run_fast_mode(
                adapter=adapter,
                child_gen=child_gen,
                board=board,
                weights=weights,
                config=config,
                workspace_root=workspace_root,
                epoch_id=resolved_epoch_id,
                parent_historical_agg=parent_historical,
                disable_drift=disable_drift,
                judge_only=judge_only,
                replicates=resolved_replicates,
            )
        )

    payload = dataclasses.asdict(result)
    # ``per_entry_losses`` contains tuples of frozen dataclasses;
    # ``asdict`` already unwrapped them into nested dicts. JSON-ify
    # with ``default=str`` to cover Path fields without bespoke
    # converters.
    click.echo(json.dumps(payload, default=str, indent=2, sort_keys=True))


def _resolve_workspace_components() -> tuple[Any, Any, Any]:
    """Locate the workspace's loader / adapter / runtime factories.

    Wired lazily because the modules involved have heavier transitive
    imports than this command file wants to drag in at ``--help`` time.
    Raises a clean :class:`click.ClickException` rather than the
    underlying :class:`ImportError` so an operator running ``zicato
    tournament`` against a not-yet-fully-assembled tree gets a
    directionally-useful error instead of a stack trace.
    """
    try:
        from zicato import adapter_factory, runtime_factory  # noqa: PLC0415
        from zicato import workspace_loader as loader  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — defensive
        raise click.ClickException("zicato workspace wiring is not available: " + str(exc)) from exc
    return loader, adapter_factory, runtime_factory


def _resolve_epoch_id(workspace_root: Path) -> str:
    """Read ``current_epoch`` from the workspace marker file."""
    marker = workspace_root / "current_epoch"
    if not marker.exists():
        raise click.ClickException(
            f"no current_epoch marker under {workspace_root}; "
            "pass --epoch explicitly or run `zicato epoch new` first"
        )
    text = marker.read_text(encoding="utf-8").strip()
    if not text:
        raise click.ClickException(
            f"{marker} is empty; pass --epoch explicitly or run `zicato epoch new` first"
        )
    return text


def _build_generation(workspace_root: Path, epoch_id: str, generation_id: str) -> Generation:
    """Build a :class:`Generation` from on-disk snapshot info.

    The tournament runner needs a :class:`Generation` with a valid
    ``snapshot_root``. We resolve the snapshot directory under the
    generation's directory and trust the adapter to fail loudly if the
    snapshot is missing the entrypoint module.
    """
    gen_dir = generation_dir(workspace_root, epoch_id, generation_id)
    snapshot_root = gen_dir / "snapshot"
    if not snapshot_root.exists():
        raise click.ClickException(
            f"snapshot not found at {snapshot_root}; "
            f"generation {generation_id!r} under epoch {epoch_id!r} is incomplete"
        )
    return Generation(
        id=generation_id,
        epoch_id=epoch_id,
        parent_id=None,  # the runner does not consult lineage here
        snapshot_root=snapshot_root.resolve(),
        created_at=_dt.datetime.now(_dt.UTC).isoformat(),
    )


def _load_historical_aggregate(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Read the parent generation's cached ``gen_score.json`` aggregate.

    Fast mode needs the parent's previously-computed scalar / per-entry
    drift counts to gate against. The convention is that the runner
    writes the aggregate to ``gen_score.json`` under the generation's
    directory after a full-mode tournament.
    """
    gen_dir = generation_dir(workspace_root, epoch_id, generation_id)
    path = gen_dir / "gen_score.json"
    if not path.exists():
        raise click.ClickException(
            f"fast-mode tournament needs a cached parent aggregate at {path}; "
            "run a full-mode tournament for the parent generation first"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise click.ClickException(f"{path}: expected a JSON object at top level")
    raw.setdefault("generation_id", generation_id)
    return raw


__all__ = ["tournament_cmd"]
