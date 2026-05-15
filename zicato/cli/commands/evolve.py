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
from pathlib import Path
from typing import Any

import click


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
            f"{kind} dotted path {dotted!r} must be 'pkg.module.attr' or "
            "'pkg.module:attr'"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise click.BadParameter(
            f"{kind}: could not import module {module_path!r}: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise click.BadParameter(
            f"{kind}: module {module_path!r} has no attribute {attr!r}"
        )
    fn = getattr(module, attr)
    if not callable(fn):
        raise click.BadParameter(
            f"{kind}: {dotted!r} resolved to {type(fn).__name__}, "
            "expected a callable"
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
def evolve_cmd(
    workspace: str,
    epoch: str | None,
    rounds: int,
    mode: str,
    harness_dotted: str,
    auxiliary_dotted: str,
    max_consecutive_rejections: int,
) -> None:
    """Run the evolve loop for N rounds against the current epoch."""
    workspace_root = Path(workspace).resolve()

    harness_call_llm = _import_callable(harness_dotted, kind="harness_call_llm")
    auxiliary_call_llm = _import_callable(
        auxiliary_dotted, kind="auxiliary_call_llm"
    )

    # Lazy import — the orchestrator is heavy. We keep it out of
    # `zicato --help` time.
    from zicato.orchestrator import evolve_n_rounds  # noqa: PLC0415

    try:
        outcomes = asyncio.run(
            evolve_n_rounds(
                rounds=rounds,
                workspace_root=workspace_root,
                epoch_id=epoch,
                harness_call_llm=harness_call_llm,
                auxiliary_call_llm=auxiliary_call_llm,
                fast_mode=(mode == "fast"),
                max_consecutive_rejections=max_consecutive_rejections,
            )
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

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
