"""``zicato repair report`` — re-render one epoch's ``analysis.md``.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` regenerates
the comprehensive analysis report (``epochs/{id}/analysis.md`` and its
HTML companion) after every round, so the happy-path operator never has
to invoke this by hand.

Run this when an existing epoch's report needs to be rebuilt against
the current on-disk data. The most common case is a path-resolution bug
in an older binary that wrote an empty / placeholder-only report even
though the data was on disk — see ``epoch_dir``'s outer-vs-inner
workspace-root normalisation in :mod:`zicato.core.workspace`. The
backfill walks the right tree and re-templates the deterministic
sections; the LLM-narrative pass is re-run only when an auxiliary
callable is configured.

The command is thin — it resolves the workspace, picks an epoch, looks
up the auxiliary callable, and calls
:func:`zicato.analyzer.report.generate_epoch_report`.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import click

from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config


def _load_workspace_config(workspace_dir: Path) -> WorkspaceConfig:
    """Read the workspace's ``config.json`` (or raise a clean click error).

    Tolerates the operator passing either the outer project dir or the
    inner ``.zicato/``: each candidate root is loaded and the first one
    that has a config wins. The descent is this command's policy; where a
    config sits under one root is the loader's.
    """

    try:
        outer = read_workspace_config(workspace_dir)
        if outer.exists:
            return outer
        inner = read_workspace_config(workspace_dir / ".zicato")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if inner.exists:
        return inner
    raise click.ClickException(f"No workspace config at {outer.path}. Run `zicato init` first.")


def _resolve_epoch(workspace_dir: Path, override: str | None) -> str:
    """Resolve the active epoch id from the override or the workspace marker."""

    if override:
        return override
    for candidate in (workspace_dir, workspace_dir / ".zicato"):
        current_path = candidate / "current_epoch"
        if current_path.exists():
            text = current_path.read_text(encoding="utf-8").strip()
            if text:
                return text
    raise click.ClickException(
        f"No active epoch. Either pass --epoch or write the id to "
        f"{workspace_dir / 'current_epoch'}."
    )


def _maybe_resolve_aux_llm(
    config: WorkspaceConfig,
) -> Callable[[str, str, str], Awaitable[str]] | None:
    """Best-effort lookup of the auxiliary LLM callable.

    The deterministic sections of the report do not need the auxiliary
    LLM — they are templated from on-disk data. The LLM only writes the
    prose blocks. When no callable is configured, return ``None`` and
    let :func:`generate_epoch_report` substitute placeholder prose; the
    deterministic figures + tables are still re-written from the correct
    workspace path, which is the point of the backfill.
    """

    dotted = config.raw.get("auxiliary_call_llm") or config.runtime.get("auxiliary_call_llm")
    if not dotted:
        return None
    mod_name, _, attr = str(dotted).rpartition(".")
    if not mod_name:
        return None
    try:
        module = importlib.import_module(mod_name)
    except ImportError:
        return None
    if not hasattr(module, attr):
        return None
    resolved: Callable[[str, str, str], Awaitable[str]] = getattr(module, attr)
    return resolved


async def _placeholder_aux(_system: str, _user: str, _model: str) -> str:
    """A no-op auxiliary callable used when no real one is configured.

    Returns an empty string so :func:`generate_epoch_report` falls back
    to its placeholder prose blocks. The deterministic data sections are
    re-rendered regardless — those are what backfill cares about.
    """

    return ""


@click.command(
    name="regenerate-report",
    short_help=(
        "Advanced: re-render `epochs/{id}/analysis.md` against the current " "on-disk data."
    ),
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root (either the project dir or the .zicato/ dir).",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's current_epoch marker.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help=(
        "Skip the auxiliary-LLM prose pass and substitute placeholders. "
        "The deterministic data sections (figures, tables, scores) are still "
        "re-rendered."
    ),
)
def regenerate_report_cmd(workspace: str, epoch: str | None, no_llm: bool) -> None:
    """Advanced: re-render an epoch's analysis.md from the current files.

    Off the happy path — `zicato evolve` regenerates the report after
    every round. Use this to repair an existing epoch whose report was
    written by a buggy older orchestrator (e.g. the workspace-root
    mis-rooting bug where the data sections rendered empty even though
    the per-generation files were on disk).

    The command is idempotent and read-only against everything except
    `analysis.md` / `analysis.html`. Pass `--no-llm` to skip the prose
    regeneration entirely; the deterministic figures + tables (the parts
    repaired by this backfill) are re-rendered regardless.
    """

    # Lazy import — the analyzer pulls in the matplotlib-style figure
    # renderers and we want `zicato --help` to stay snappy.
    from zicato.analyzer.report import generate_epoch_report  # noqa: PLC0415

    workspace_dir = Path(workspace).resolve()
    config = _load_workspace_config(workspace_dir)
    epoch_id = _resolve_epoch(workspace_dir, epoch)

    aux_call_llm = None if no_llm else _maybe_resolve_aux_llm(config)
    if aux_call_llm is None:
        aux_call_llm = _placeholder_aux
        if not no_llm:
            click.echo(
                "warning: no auxiliary LLM configured; substituting placeholder "
                "prose. Pass --no-llm to suppress this warning.",
                err=True,
            )

    model = config.auxiliary_model

    out_path = asyncio.run(
        generate_epoch_report(
            workspace_dir,
            epoch_id,
            aux_call_llm,
            model=model,
        )
    )
    click.echo(f"Regenerated analysis report for epoch {epoch_id!r} at {out_path}")


__all__ = ["regenerate_report_cmd"]
