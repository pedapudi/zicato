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

The evaluation callable is resolved from the named engine selected by
`models.roles.evaluation`. Import or configuration failures produce a command
error before analysis begins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import click

from zicato.config import resolve_configuration
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
    """Resolve the named evaluation engine through the runtime owner."""
    from zicato.runtime_factory import resolve_role_call_llm

    try:
        return resolve_role_call_llm(
            config.raw, role="evaluation", workspace_root=config.path.parent
        )
    except (ImportError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


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
            aux_config=resolve_configuration(config.raw).values.aux,
            round_n=round_n,
        )
    )
    click.echo(f"Wrote decision-telemetry insight for epoch {epoch_id!r} to {out_path}")


__all__ = ["analyze_telemetry_cmd"]
