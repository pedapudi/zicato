"""``zicato inspect telemetry`` — manually run the decision-telemetry analyzer.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` runs the
analyzer as part of the loop. Run ``zicato inspect telemetry`` by hand
only to (re)generate a decision-telemetry insight for an epoch.

Standalone command file. The auto-discovery layer in
:mod:`zicato.cli.discovery` picks up the ``analyze_telemetry_cmd``
exported below.

The command wires together:

* :func:`zicato.workspace_loader.load_workspace_config` for the workspace
  config (auxiliary callable dotted path, auxiliary model id).
* :func:`zicato.runtime_factory.make_runtime_config` for the
  :class:`zicato.core.types.RuntimeConfig` and its ``auxiliary_call_llm``
  callable.
* :func:`zicato.analyzer.insights.analyze_epoch_telemetry` for the
  analysis itself.

The auxiliary callable resolution mirrors the ``zicato proposer propose``
command's discipline: we read the workspace config's
``runtime.auxiliary_call_llm`` (or its legacy ``auxiliary_call_llm``
sibling) and import the dotted path. A failure to resolve the callable
surfaces as a ``ClickException`` rather than a stack trace.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click


def _load_workspace_config(workspace_dir: Path) -> dict[str, Any]:
    """Read the workspace's ``config.json`` (or raise a clean click error)."""

    config_path = workspace_dir / "config.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace config at {config_path}. Run `zicato epoch register` first."
        )
    try:
        loaded: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        return loaded
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not parse {config_path}: {exc}") from exc


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


def _resolve_aux_llm(config: dict[str, Any]) -> Callable[[str, str, str], Awaitable[str]]:
    """Look up the auxiliary LLM callable from the workspace config.

    Mirrors :func:`zicato.cli.commands.propose._resolve_aux_llm`. The
    config field is ``auxiliary_call_llm`` (a dotted import path) or
    the nested ``runtime.auxiliary_call_llm``. When absent, we raise a
    click error early so the operator doesn't burn time waiting for a
    call that can't happen.
    """

    dotted = config.get("auxiliary_call_llm")
    if not dotted and isinstance(config.get("runtime"), dict):
        dotted = config["runtime"].get("auxiliary_call_llm")
    if not dotted:
        raise click.ClickException(
            "No auxiliary LLM callable is registered. Wire one into the "
            "workspace config under 'auxiliary_call_llm' (dotted import path) "
            "before running `zicato inspect telemetry`."
        )
    mod_name, _, attr = str(dotted).rpartition(".")
    if not mod_name:
        raise click.ClickException(
            f"auxiliary_call_llm config value is not a dotted path: {dotted!r}"
        )
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise click.ClickException(
            f"Could not import {mod_name!r} for auxiliary_call_llm: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise click.ClickException(
            f"Module {mod_name!r} has no attribute {attr!r} for auxiliary_call_llm"
        )
    resolved: Callable[[str, str, str], Awaitable[str]] = getattr(module, attr)
    return resolved


def _resolve_model(config: dict[str, Any]) -> str:
    """Pick the model id forwarded to the auxiliary LLM."""

    if config.get("auxiliary_model"):
        return str(config["auxiliary_model"])
    if isinstance(config.get("runtime"), dict):
        return str(config["runtime"].get("auxiliary_model", ""))
    return ""


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
    model = _resolve_model(config)

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
