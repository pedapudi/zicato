"""``zicato repair-judge-losses`` — backfill per_judge_loss into existing runs.

ADVANCED / DEBUGGING — off the happy path. Used to repair workspaces
whose ``loss.json`` files predate the per-judge-loss promotion fix:
they carry the aggregate ``drift_loss`` correctly but leave
``per_judge_loss`` empty, so the per-judge drift-attribution table in
the analyzer report shows the no-judge-fired notice even when custom
judges fired during the run.

The command walks every run under every epoch of the workspace,
re-derives ``per_judge_loss`` by replaying the run's ``events.jsonl``
through the reducer's per-judge attribution path, rewrites the
``loss.json`` with the populated field, and re-ingests the updated
profile into the analytical index so ``judge_losses`` rows land
without needing a full ``zicato reindex``.

The repair is idempotent — running it twice on a workspace whose
loss.json already carries ``per_judge_loss`` is a no-op (the
re-derivation reproduces the same values). Reads ``scoring.json``
for each epoch so the per-judge weighting matches what would have
landed had the original reducer run produced the field.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from zicato.core.types import DriftCount, ScoringWeights
from zicato.core.workspace import (
    events_jsonl_path,
    loss_profile_path,
    scoring_path,
)
from zicato.index.ingest import ingest_run
from zicato.telemetry.reducer import (
    compute_per_judge_loss,
    read_loss_profile,
    write_loss_profile,
)
from zicato.workspace_loader import _scoring_weights_from_dict


def _load_scoring_for_epoch(workspace_root: Path, epoch_id: str) -> ScoringWeights:
    """Load one epoch's ScoringWeights, falling back to defaults.

    The repair runs over historical workspaces where the scoring.json
    may not list every weight key — anything missing falls back to
    ScoringWeights defaults via :func:`_scoring_weights_from_dict`. A
    workspace with no scoring.json at all (a partial init) still
    re-derives at default weights so the per-judge attribution is at
    least computable; the operator can re-set per_judge_weights and
    re-run the repair to refine.
    """
    path = scoring_path(workspace_root, epoch_id)
    if not path.exists():
        return ScoringWeights()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ScoringWeights()
    if not isinstance(raw, dict):
        return ScoringWeights()
    return _scoring_weights_from_dict(raw)


def _iter_runs(workspace_root: Path) -> list[tuple[str, str, str]]:
    """Enumerate every ``(epoch_id, generation_id, entry_id)`` on disk.

    The walk mirrors the analytical index's own walk so the repair
    sees the same set of runs the analyzer / dashboard render against.
    A workspace with no ``epochs/`` root yields the empty list — the
    command then exits cleanly rather than raising.
    """
    epochs_root = workspace_root / "epochs"
    if not epochs_root.is_dir():
        return []
    out: list[tuple[str, str, str]] = []
    for epoch_dir in sorted(p for p in epochs_root.iterdir() if p.is_dir()):
        gens_root = epoch_dir / "generations"
        if not gens_root.is_dir():
            continue
        for gen_dir in sorted(p for p in gens_root.iterdir() if p.is_dir()):
            runs_root = gen_dir / "runs"
            if not runs_root.is_dir():
                continue
            for run_dir_path in sorted(p for p in runs_root.iterdir() if p.is_dir()):
                out.append((epoch_dir.name, gen_dir.name, run_dir_path.name))
    return out


def _rederive_per_judge_loss(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    weights: ScoringWeights,
) -> bool:
    """Re-derive ``per_judge_loss`` for one run and write it back.

    Returns ``True`` when the run's ``loss.json`` was rewritten (any
    re-derivation, even one that produced the same tuple, counts as a
    write so the file's serialised form is canonical). Returns
    ``False`` when there is nothing to repair — no ``loss.json``
    landed for the run, or the file could not be parsed.

    The re-derivation uses the run's already-bucketed
    :attr:`LossProfile.drift_counts` rather than re-walking
    ``events.jsonl`` because the reducer's per-judge attribution
    pipeline is keyed on the ``custom:<judge_name>`` drift kind that
    already lives on those counts — same path
    :func:`zicato.telemetry.reducer.compute_per_judge_loss` operates on.
    When the persisted profile has no drift_counts (the loss profile
    predated the metric_counts surface entirely), we additionally
    rebuild drift_counts from the events JSONL so the per-judge view
    is not silently empty.
    """
    lpath = loss_profile_path(workspace_root, epoch_id, generation_id, entry_id)
    if not lpath.exists():
        return False
    try:
        profile = read_loss_profile(lpath)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False

    drift_counts: tuple[DriftCount, ...] = profile.drift_counts
    if not drift_counts:
        # Fall back to re-tallying events.jsonl so a run whose loss
        # profile was written without drift_counts (an older reducer
        # crash, or a hand-rolled fixture) still surfaces per-judge
        # attribution. We reuse the analytical index's tolerant event
        # walker rather than the strict proto-replay path.
        from zicato.index.ingest import _drift_counts_from_events  # noqa: PLC0415

        epath = events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id)
        tally = _drift_counts_from_events(epath)
        if tally:
            drift_counts = tuple(
                DriftCount(kind=kind, severity=sev, count=count)  # type: ignore[arg-type]
                for (kind, sev), count in sorted(tally.items())
            )

    per_judge_loss = compute_per_judge_loss(drift_counts, weights)
    # Re-write even when the new tuple equals the old one — the file's
    # serialised form is now canonical and the index re-ingest below
    # picks up the on-disk shape.
    from dataclasses import replace  # noqa: PLC0415

    new_profile = replace(profile, per_judge_loss=per_judge_loss)
    write_loss_profile(new_profile, lpath)
    return True


@click.command(
    name="repair-judge-losses",
    short_help="Advanced: backfill per_judge_loss into existing loss.json files.",
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--reingest/--no-reingest",
    default=True,
    show_default=True,
    help=(
        "Re-ingest each rewritten run into index.db so the judge_losses "
        "table is populated without a full `zicato reindex`."
    ),
)
def repair_judge_losses_cmd(workspace: str, reingest: bool) -> None:
    """Advanced: backfill per_judge_loss into existing runs.

    Off the happy path. Walks every run on disk, re-derives the
    per-judge weighted-loss attribution from the run's drift_counts
    (and, for runs whose profile predated drift_counts entirely, from
    the events JSONL), and rewrites loss.json with the populated
    `per_judge_loss` field. Idempotent: re-running produces the same
    files. By default each rewritten run is re-ingested into the
    analytical index so `judge_losses` rows land immediately.
    """
    ws = Path(workspace).resolve()
    runs = _iter_runs(ws)
    rewrote = 0
    skipped = 0
    weights_cache: dict[str, ScoringWeights] = {}
    for epoch_id, generation_id, entry_id in runs:
        if epoch_id not in weights_cache:
            weights_cache[epoch_id] = _load_scoring_for_epoch(ws, epoch_id)
        weights = weights_cache[epoch_id]
        wrote = _rederive_per_judge_loss(ws, epoch_id, generation_id, entry_id, weights)
        if wrote:
            rewrote += 1
            if reingest:
                try:
                    ingest_run(ws, None, epoch_id, generation_id, entry_id)
                except Exception as exc:  # noqa: BLE001 — best-effort repair
                    click.echo(
                        f"  warning: re-ingest of "
                        f"{epoch_id}/{generation_id}/{entry_id} failed: {exc}",
                        err=True,
                    )
        else:
            skipped += 1
    click.echo(
        f"Repaired per-judge loss in {rewrote} run "
        f"{'profile' if rewrote == 1 else 'profiles'} under {ws} "
        f"({skipped} skipped — no loss.json or unreadable)."
    )


__all__ = ["repair_judge_losses_cmd"]
