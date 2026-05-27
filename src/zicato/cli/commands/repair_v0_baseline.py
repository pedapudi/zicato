"""``zicato repair-v0-baseline`` — backfill the synthetic v0 experiment.json.

ADVANCED / DEBUGGING — off the happy path. Fresh workspaces created by a
recent ``zicato evolve`` already carry a synthetic ``experiment.json``
under ``epochs/{id}/generations/v0/`` (the orchestrator writes one when
the baseline snapshot is materialised). This command exists for
*pre-existing* workspaces whose v0 directory predates that write: it
walks every epoch in the workspace and writes the marker into any v0
generation that is missing one.

The marker carries:

* ``id``: ``"exp_{epoch}_v0"``
* ``parent_generation_id``: ``""`` (cross-epoch lineage lives in
  ``lineage.json``; within the epoch the seed has no parent)
* ``hypothesis.core_idea``: ``"baseline seed"``
* ``outcome``: ``null`` (the seed never ran a tournament round)

Idempotent: re-running the command against a workspace whose v0 already
has a marker is a no-op.

Why this matters: the analyzer report's per-board outcomes heatmap and
aggregate scores table both walk ``_promoted_lineage(data)``, which
needs a baseline anchor. The loader only materialises a generation view
when ``experiment.json`` is readable, so a v0 without the marker
short-circuits the whole promoted lineage and the section renders
empty. The synthetic marker keeps every downstream consumer on a
uniform on-disk shape without inventing tournament numbers.
"""

from __future__ import annotations

from pathlib import Path

import click

from zicato.core.workspace import epoch_dir, generation_dir
from zicato.epoch.journal import write_seed_experiment


def _epochs_root(workspace_root: Path) -> Path:
    """Resolve the ``epochs/`` directory under ``workspace_root``.

    Reuses :func:`zicato.core.workspace.epoch_dir`'s normalisation (it
    descends into ``.zicato/`` when the caller passes the outer project
    dir) by querying it for a sentinel epoch id and stripping the
    trailing component. Avoids reaching for the private normaliser.
    """
    return epoch_dir(workspace_root, "_sentinel_").parent


def _iter_v0_targets(workspace_root: Path, epoch_filter: str | None) -> list[str]:
    """Enumerate epoch ids whose v0 generation directory exists on disk.

    Walks ``epochs/`` directly rather than going through
    :func:`zicato.epoch.lifecycle.list_epochs` so a workspace whose
    ``config.json`` is malformed or pre-schema still surfaces. The
    repair command's whole point is to fix workspaces in the wild;
    insisting on a strictly-valid config to even *find* them is
    backwards.

    When ``epoch_filter`` is given we restrict to that single epoch
    (and still verify that its v0 directory exists — a typo'd filter
    argument yields an empty target list rather than a stack trace).
    """
    epochs_root = _epochs_root(workspace_root)
    if not epochs_root.exists():
        return []
    candidates: list[str] = []
    for child in sorted(epochs_root.iterdir()):
        if not child.is_dir():
            continue
        candidates.append(child.name)
    if epoch_filter is not None:
        candidates = [eid for eid in candidates if eid == epoch_filter]
    out: list[str] = []
    for eid in candidates:
        gdir = generation_dir(workspace_root, eid, "v0")
        if gdir.exists():
            out.append(eid)
    return out


@click.command(
    name="repair-v0-baseline",
    short_help="Advanced: backfill the synthetic v0 experiment.json into every epoch.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--epoch",
    "epoch_filter",
    default=None,
    help="Restrict the backfill to a single epoch id (default: every epoch).",
)
def repair_v0_baseline_cmd(workspace: str, epoch_filter: str | None) -> None:
    """Advanced: backfill the synthetic v0 ``experiment.json`` marker.

    Walks every epoch under the workspace (or the one named by
    ``--epoch``) and, for each that has a ``generations/v0/`` directory
    without an ``experiment.json``, writes a synthetic seed marker. The
    marker is idempotent: workspaces created by a fresh ``zicato
    evolve`` already carry it and the command leaves them alone.
    """
    ws = Path(workspace).resolve()
    targets = _iter_v0_targets(ws, epoch_filter)
    if not targets:
        if epoch_filter is not None:
            click.echo(f"No epoch {epoch_filter!r} with a v0 directory found under {ws}.")
        else:
            click.echo(f"No epochs with a v0 directory found under {ws}.")
        return

    written: list[str] = []
    skipped: list[str] = []
    for eid in targets:
        if write_seed_experiment(ws, eid, "v0"):
            written.append(eid)
        else:
            skipped.append(eid)

    click.echo(f"Scanned {len(targets)} epoch(s) under {ws}.")
    if written:
        click.echo(f"  Wrote v0 marker into {len(written)} epoch(s):")
        for eid in written:
            click.echo(f"    {eid}")
    if skipped:
        click.echo(f"  Left {len(skipped)} epoch(s) untouched (marker already present).")


__all__ = ["repair_v0_baseline_cmd"]
