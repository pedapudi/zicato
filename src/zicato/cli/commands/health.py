"""``zicato health`` — report on the evolve loop's optimization signal.

Standalone command file picked up by :mod:`zicato.cli.discovery`.

The command loads the current epoch's per-generation loss profiles,
its experiment records, and its frozen board, runs every detector in
:mod:`zicato.health.diagnostics`, and prints the resulting
:class:`~zicato.health.diagnostics.LoopHealth` report. Findings are
colour-coded by severity. The command exits non-zero when any
``critical`` finding is present so a CI / supervisor wrapper notices a
toothless evaluation without an operator having to read the output.

All workspace I/O lives here — the diagnostics module itself is pure.
Generation directories are enumerated under ``epochs/{id}/generations``
and sorted numerically (``v2`` before ``v10``) so window-based
detectors see lineage order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from zicato.core.types import BoardEntry, LossProfile
from zicato.core.workspace import (
    experiment_json_path,
    generations_dir,
    loss_profile_path,
)
from zicato.health.diagnostics import LoopHealth, assess_loop_health

#: ANSI-ish colour names click understands, keyed by finding severity.
_SEVERITY_COLOR: dict[str, str] = {
    "info": "cyan",
    "warning": "yellow",
    "critical": "red",
}


def _resolve_epoch_id(workspace_dir: Path, override: str | None) -> str:
    """Resolve the active epoch id from ``--epoch`` or the marker file."""
    if override:
        return override
    marker = workspace_dir / "current_epoch"
    if marker.exists():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise click.ClickException(f"No active epoch. Either pass --epoch or write the id to {marker}.")


def _generation_ids(workspace_dir: Path, epoch_id: str) -> list[str]:
    """Return generation ids under the epoch in numeric lineage order.

    ``v2`` sorts before ``v10`` — a plain lexical sort would invert
    them and feed window-based detectors a scrambled history. Ids that
    do not match the ``v<digits>`` convention are appended after the
    numeric ones in lexical order so adapters that name generations
    differently still surface.
    """
    gens_root = generations_dir(workspace_dir, epoch_id)
    if not gens_root.exists():
        return []
    numeric: list[tuple[int, str]] = []
    other: list[str] = []
    for child in gens_root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("v") and name[1:].isdigit():
            numeric.append((int(name[1:]), name))
        else:
            other.append(name)
    numeric.sort()
    return [name for _, name in numeric] + sorted(other)


def _load_losses_by_generation(
    workspace_dir: Path,
    epoch_id: str,
    generation_ids: list[str],
    board_entries: list[BoardEntry],
) -> dict[str, list[LossProfile]]:
    """Read every ``loss.json`` for every (generation, board entry) pair.

    Missing per-run loss files are skipped silently — a freshly-created
    generation may have no telemetry yet. A generation that produced no
    readable loss at all still gets an entry (an empty list) so the
    health report can see it ran.
    """
    # Lazy import: keeps `zicato --help` fast.
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    losses_by_generation: dict[str, list[LossProfile]] = {}
    for generation_id in generation_ids:
        losses: list[LossProfile] = []
        for entry in board_entries:
            lpath = loss_profile_path(workspace_dir, epoch_id, generation_id, entry.id)
            if not lpath.exists():
                continue
            try:
                losses.append(read_loss_profile(lpath))
            except (OSError, ValueError, KeyError):
                continue
        losses_by_generation[generation_id] = losses
    return losses_by_generation


def _load_experiments(
    workspace_dir: Path, epoch_id: str, generation_ids: list[str]
) -> list[dict[str, Any]]:
    """Read each generation's ``experiment.json`` as a plain dict.

    Dicts are returned in lineage order. The diagnostics detectors
    accept the raw dict shape directly, so no typed reconstruction is
    needed here. Unparseable files are skipped silently.
    """
    out: list[dict[str, Any]] = []
    for generation_id in generation_ids:
        path = experiment_json_path(workspace_dir, epoch_id, generation_id)
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            d.setdefault("generation_id", generation_id)
            out.append(d)
    return out


def _load_board(workspace_dir: Path, epoch_id: str) -> list[BoardEntry]:
    """Load the epoch's frozen board, or raise a clean click error."""
    from zicato.board.jsonl import load_board  # noqa: PLC0415
    from zicato.core.workspace import board_path  # noqa: PLC0415

    path = board_path(workspace_dir, epoch_id)
    if not path.exists():
        raise click.ClickException(
            f"No board.jsonl at {path}; the epoch {epoch_id!r} is incomplete."
        )
    try:
        return load_board(path)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Could not read board at {path}: {exc}") from exc


def _max_generations_per_contract(workspace_dir: Path, epoch_id: str) -> int | None:
    """Read the epoch's ``overfitting.max_generations_per_contract`` cadence.

    Best-effort: a missing / unreadable ``scoring.json`` (an incomplete
    epoch) yields ``None`` — the cadence detector simply stays silent rather
    than failing the health command.
    """
    from zicato.core.workspace import scoring_path  # noqa: PLC0415
    from zicato.workspace_loader import overfitting_config_from_dict  # noqa: PLC0415

    path = scoring_path(workspace_dir, epoch_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return overfitting_config_from_dict(raw.get("overfitting")).max_generations_per_contract


def _workspace_health_config(workspace_dir: Path) -> Any:
    """Resolve the detector thresholds from the workspace ``config.json``.

    The ``health`` block is the operator surface for the loop-health
    thresholds (the former ``ZICATO_HEALTH_*`` env vars, deleted). A
    missing / unreadable ``config.json`` yields ``None`` — the defaults
    apply, matching the other best-effort loaders here — but a PRESENT,
    malformed ``health`` block fails loudly (as a clean CLI error): the
    operator explicitly wrote it and deserves the typo report, not a
    silently defaulted detector.
    """
    from zicato.config import health_config_from_workspace  # noqa: PLC0415
    from zicato.workspace_loader import load_workspace_config  # noqa: PLC0415

    try:
        cfg = load_workspace_config(workspace_dir)
    except (FileNotFoundError, ValueError):
        return None
    try:
        return health_config_from_workspace(cfg)
    except (KeyError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def render_report(report: LoopHealth) -> str:
    """Render a :class:`LoopHealth` report as colour-coded terminal text.

    Kept as a free function (not inlined into the command) so tests can
    assert on the rendered text without invoking the click runner.
    """
    lines: list[str] = []
    lines.append(f"Loop health for epoch {report.epoch_id!r}")
    lines.append(f"checked_at: {report.checked_at}")
    if report.healthy:
        lines.append(click.style("HEALTHY — no warning or critical findings.", fg="green"))
    else:
        lines.append(click.style("UNHEALTHY — one or more findings need attention.", fg="red"))
    lines.append("")

    if not report.findings:
        lines.append("No findings — every detector stayed silent.")
        return "\n".join(lines)

    lines.append(f"{len(report.findings)} finding(s):")
    for finding in report.findings:
        color = _SEVERITY_COLOR.get(finding.severity, "white")
        tag = click.style(f"[{finding.severity.upper()}]", fg=color, bold=True)
        lines.append(f"  {tag} {finding.code}: {finding.summary}")
        for key in sorted(finding.detail):
            lines.append(f"      {key}: {finding.detail[key]}")
    return "\n".join(lines)


@click.command(
    name="health",
    short_help="Advanced: report whether the evolve loop has real optimization signal.",
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
def health_cmd(workspace: str, epoch: str | None) -> None:
    """Report on the evolve loop's optimization signal for the current epoch.

    Detects toothless evaluations — flat scoring, dead board entries,
    inert drift, a stalled proposer — and prints them as findings.
    Exits non-zero when any critical finding is present.
    """
    workspace_dir = Path(workspace)
    epoch_id = _resolve_epoch_id(workspace_dir, epoch)

    board_entries = _load_board(workspace_dir, epoch_id)
    generation_ids = _generation_ids(workspace_dir, epoch_id)
    losses_by_generation = _load_losses_by_generation(
        workspace_dir, epoch_id, generation_ids, board_entries
    )
    experiments = _load_experiments(workspace_dir, epoch_id, generation_ids)

    report = assess_loop_health(
        losses_by_generation=losses_by_generation,
        experiments=experiments,
        board_entries=board_entries,
        epoch_id=epoch_id,
        config=_workspace_health_config(workspace_dir),
        max_generations_per_contract=_max_generations_per_contract(workspace_dir, epoch_id),
    )

    click.echo(render_report(report))

    if any(finding.severity == "critical" for finding in report.findings):
        raise SystemExit(1)


__all__ = ["health_cmd", "render_report"]
