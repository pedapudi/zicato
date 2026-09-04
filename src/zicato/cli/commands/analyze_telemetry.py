"""``zicato inspect telemetry`` — manually run the decision-telemetry analyzer.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` runs the
analyzer as part of the loop. Run ``zicato inspect telemetry`` by hand
only to (re)generate a decision-telemetry insight for an epoch.

Standalone command file. The auto-discovery layer in
:mod:`zicato.cli.discovery` picks up the ``analyze_telemetry_cmd``
exported below.

The command wires together:

* :func:`zicato.workspace_loader.load_workspace_config` for the workspace
  config (evaluation callable dotted path, evaluation model id).
* :func:`zicato.runtime_factory.make_runtime_config` for the
  :class:`zicato.core.types.RuntimeConfig` and its ``evaluation_call_llm``
  callable.
* :func:`zicato.analyzer.insights.analyze_epoch_telemetry` for the
  analysis itself.

The evaluation callable resolution mirrors the ``zicato proposer propose``
command's discipline: we read the workspace config's
``runtime.evaluation_call_llm`` (or the top-level ``evaluation_call_llm``
key, also accepted) and import the dotted path. A failure to resolve the callable
surfaces as a ``ClickException`` rather than a stack trace.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import click

from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config


def _load_workspace_config(workspace_dir: Path) -> WorkspaceConfig:
    """Read the workspace's ``config.json`` (or raise a clean click error)."""

    try:
        config = read_workspace_config(workspace_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not config.exists:
        raise click.ClickException(
            f"No workspace config at {config.path}. Run `zicato epoch register` first."
        )
    return config


def _resolve_epoch(workspace_dir: Path, override: str | None) -> str:
    """Resolve the active epoch id from the override or the workspace marker."""

    if override:
        return override
    current_path = workspace_dir / "current_epoch"
    if current_path.exists():
        text = current_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise click.ClickException(
        f"No active epoch. Either pass --epoch or write the id to {current_path}."
    )


def _resolve_aux_llm(config: WorkspaceConfig) -> Callable[[str, str, str], Awaitable[str]]:
    """Look up the evaluation LLM callable from the workspace config.

    Mirrors :func:`zicato.cli.commands.propose._resolve_aux_llm`. The
    config field is ``evaluation_call_llm`` (a dotted import path) or
    the nested ``runtime.evaluation_call_llm``. When absent, we raise a
    click error early so the operator doesn't burn time waiting for a
    call that can't happen.
    """

    dotted = config.raw.get("evaluation_call_llm") or config.runtime.get("evaluation_call_llm")
    if not dotted:
        raise click.ClickException(
            "No evaluation LLM callable is registered. Wire one into the "
            "workspace config under 'evaluation_call_llm' (dotted import path) "
            "before running `zicato inspect telemetry`."
        )
    mod_name, _, attr = str(dotted).rpartition(".")
    if not mod_name:
        raise click.ClickException(
            f"evaluation_call_llm config value is not a dotted path: {dotted!r}"
        )
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise click.ClickException(
            f"Could not import {mod_name!r} for evaluation_call_llm: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise click.ClickException(
            f"Module {mod_name!r} has no attribute {attr!r} for evaluation_call_llm"
        )
    resolved: Callable[[str, str, str], Awaitable[str]] = getattr(module, attr)
    return resolved


@click.command(
    name="analyze-telemetry",
    short_help="Advanced: (re)run the decision-telemetry analyzer for an epoch.",
)
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(),
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's 'current_epoch' file contents.",
)
@click.option(
    "--round",
    "round_n",
    type=int,
    default=None,
    help=(
        "Round number for the output filename. Omit to write "
        "insights/latest.md instead of insights/round_{N:04d}.md."
    ),
)
def analyze_telemetry_cmd(workspace: str, epoch: str | None, round_n: int | None) -> None:
    """Advanced: run the decision-telemetry analyzer for the current epoch.

    Off the happy path — `zicato evolve` runs the analyzer per round.
    Use this to (re)generate an insight for an epoch out of band.
    """

    # Lazy import: keeps `zicato --help` fast and the analyzer module
    # easy to install incrementally.
    from zicato.analyzer.insights import analyze_epoch_telemetry  # noqa: PLC0415

    workspace_dir = Path(workspace)
    config = _load_workspace_config(workspace_dir)
    epoch_id = _resolve_epoch(workspace_dir, epoch)
    aux_call_llm = _resolve_aux_llm(config)
    model = config.evaluation_model

    out_path = asyncio.run(
        analyze_epoch_telemetry(
            workspace_dir,
            epoch_id,
            aux_call_llm,
            model=model,
            round_n=round_n,
        )
    )
    click.echo(f"Wrote decision-telemetry insight for epoch {epoch_id!r} to {out_path}")


__all__ = ["analyze_telemetry_cmd"]
