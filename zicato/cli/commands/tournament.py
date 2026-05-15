"""``zicato tournament`` — run a tournament between two generations.

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
default-epoch resolution follows the workspace's ``lineage.json``
when ``--epoch`` is omitted.

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
import json
from pathlib import Path

import click


@click.command(name="tournament")
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
def tournament_cmd(
    parent: str,
    child: str,
    workspace: str,
    epoch: str | None,
    mode: str,
) -> None:
    """Run a tournament between PARENT and CHILD generations."""
    workspace_root = Path(workspace).resolve()

    # The wiring below is intentionally lazy: the workspace loader, the
    # adapter factory, and the runtime config builder all live in
    # sibling modules being filled in by parallel work-streams. We
    # import them inside the command so importing this module (e.g.
    # for ``zicato --help``) does not pull the world in.
    loader, adapter_factory, runtime_factory = _resolve_workspace_components(
        workspace_root
    )

    resolved_epoch_id, parent_gen, child_gen, board, weights = loader.load_pair(
        workspace_root=workspace_root,
        epoch_id=epoch,
        parent_id=parent,
        child_id=child,
    )

    adapter = adapter_factory.build(workspace_root=workspace_root)
    config = runtime_factory.build(workspace_root=workspace_root)

    from zicato.tournament import run_fast_mode, run_tournament  # noqa: PLC0415

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
            )
        )
    else:
        parent_historical = loader.load_historical_aggregate(
            workspace_root=workspace_root,
            epoch_id=resolved_epoch_id,
            generation_id=parent_gen.id,
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
            )
        )

    payload = dataclasses.asdict(result)
    # ``per_entry_losses`` contains tuples of frozen dataclasses;
    # ``asdict`` already unwrapped them into nested dicts. JSON-ify
    # with ``default=str`` to cover Path fields without bespoke
    # converters.
    click.echo(json.dumps(payload, default=str, indent=2, sort_keys=True))


def _resolve_workspace_components(workspace_root: Path):  # type: ignore[no-untyped-def]
    """Locate the workspace's loader / adapter / runtime factories.

    Wired lazily because the modules involved are owned by parallel
    work-streams. Raises a clean :class:`click.ClickException` rather
    than the underlying :class:`ImportError` so an operator running
    ``zicato tournament`` against a not-yet-fully-assembled tree gets
    a directionally-useful error instead of a stack trace.
    """
    try:
        from zicato import (  # noqa: PLC0415
            adapter_factory,
            runtime_factory,
            workspace_loader as loader,
        )
    except ImportError as exc:  # pragma: no cover — exercised once those modules land
        raise click.ClickException(
            "zicato workspace wiring is not available yet: " + str(exc)
        ) from exc
    return loader, adapter_factory, runtime_factory


__all__ = ["tournament_cmd"]
