"""``zicato repair judge-losses`` — backfill per_judge_loss into existing runs.

ADVANCED / DEBUGGING — off the happy path. Used to repair workspaces
whose ``loss.json`` files predate the per-judge-loss promotion fix:
they carry the aggregate ``drift_loss`` correctly but leave
``per_judge_loss`` empty, so the per-judge drift-attribution table in
the analyzer report shows the no-judge-fired notice even when custom
judges fired during the run.

The command walks every run under every epoch and repairs EVERY
persisted loss slot each run directory holds — the canonical
``loss.json`` and each replicate sibling ``loss.r{n}.json``
(:func:`zicato.tournament.unit_cache.persisted_loss_slots`). A
replicated unit keeps most of its records in the r>0 slots, so a pass
resolving only the canonical file would report success while leaving
those profiles unattributed forever. Each slot is re-derived from its
OWN records and never another replicate's, at the epoch's own
``scoring.json`` weighting, and is left byte-for-byte unchanged when it
already agrees.

Slots the reserved-base ledger refuses as EVIDENCE — the contract
pre-flight's degraded probes, the candidate screen's panel-subset draws
— are repaired too: this is a consistency pass over persisted records,
not a read of what a generation did, and a profile whose
``per_judge_loss`` disagrees with its own drift counts is a wrong record
whichever owner wrote it. Attempt siblings (``loss.a{n}.json``) are
never touched; they record executions that were superseded.

Scope boundary: the repair fixes the ON-DISK records. The analytical
index is a projection of canonical slots only
(:func:`zicato.index.ingest.ingest_run` resolves ``loss.json``), so a
run's ``judge_losses`` rows come from replicate 0; a repaired r>0 slot
is correct on disk and carries no index row by design. Each run whose
canonical profile is readable is re-ingested so those rows land without
needing a full ``zicato repair index``.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Literal, get_args

import click

from zicato.core import normalize_wire_drift_kind, normalize_wire_severity
from zicato.core.types import DriftCount, ScoringWeights
from zicato.core.workspace import (
    loss_profile_path,
    scoring_path,
)
from zicato.index.ingest import ingest_run
from zicato.telemetry.reducer import (
    compute_per_judge_loss,
    read_loss_profile,
    write_loss_profile,
)
from zicato.tournament.unit_cache import persisted_loss_slots, unit_events_path
from zicato.workspace import WorkspaceLayout, list_epoch_ids
from zicato.workspace_loader import scoring_weights_from_dict


def _drift_counts_from_events(events_path: Path) -> Counter[tuple[str, str]]:
    """Tally ``(drift_kind, severity)`` pairs from a run's events JSONL.

    A best-effort plain-JSON walk: every line is parsed as a dict and any
    ``DriftDetected`` payload contributes one to its ``(kind, severity)``
    bucket. We deliberately do NOT route through goldfive's strict proto
    replay here — the repair must work in a stripped-down environment, and
    the reducer already owns the proto-strict path for scoring. The kind /
    severity normalisation mirrors :mod:`zicato.telemetry.reducer` so this
    repair agrees with the loss profile.

    Goldfive's persistence sink serialises events with ``MessageToJson``,
    which renders payload keys in camelCase (``driftDetected``); zicato's
    own dict-fallback writer uses snake_case (``drift_detected``). We accept
    either so the repair walks the JSONL regardless of which writer
    produced it.

    Returns an empty counter when the file is absent or unreadable. This
    helper backs the per-judge repair's fallback for loss profiles whose
    own ``drift_counts`` predate the metric surface; the analytical index
    itself no longer re-tallies events (it is a pure projection of
    ``loss.json`` — see :mod:`zicato.index.ingest`).
    """
    tally: Counter[tuple[str, str]] = Counter()
    if not events_path.exists():
        return tally
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return tally
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        payload = evt.get("drift_detected")
        if not isinstance(payload, dict):
            payload = evt.get("driftDetected")
        if not isinstance(payload, dict):
            continue
        kind = normalize_wire_drift_kind(payload.get("kind", ""))
        sev = normalize_wire_severity(payload.get("severity", ""))
        if kind is None or sev is None:
            continue
        tally[(kind, sev)] += 1
    return tally


def _load_scoring_for_epoch(workspace_root: Path, epoch_id: str) -> ScoringWeights:
    """Load one epoch's ScoringWeights, falling back to defaults.

    The repair runs over historical workspaces where the scoring.json
    may not list every weight key — anything missing falls back to
    ScoringWeights defaults via :func:`scoring_weights_from_dict`. A
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
    return scoring_weights_from_dict(raw)


def _iter_runs(workspace_root: Path) -> list[tuple[str, str, str]]:
    """Enumerate every ``(epoch_id, generation_id, entry_id)`` on disk.

    Epochs are enumerated + ordered by the single canonical authority
    (:func:`zicato.workspace.list_epoch_ids`, timestamp-first); the
    per-epoch generation / run sub-walks and the leaf path math route
    through :class:`WorkspaceLayout`. A workspace with no ``epochs/``
    root yields the empty list — the command then exits cleanly rather
    than raising.
    """
    layout = WorkspaceLayout.from_root(workspace_root)
    out: list[tuple[str, str, str]] = []
    for epoch_id in list_epoch_ids(layout):
        gens_root = layout.generations_dir(epoch_id)
        if not gens_root.is_dir():
            continue
        for gen_dir in sorted(p for p in gens_root.iterdir() if p.is_dir()):
            runs_root = layout.runs_dir(epoch_id, gen_dir.name)
            if not runs_root.is_dir():
                continue
            for run_dir_path in sorted(p for p in runs_root.iterdir() if p.is_dir()):
                out.append((epoch_id, gen_dir.name, run_dir_path.name))
    return out


#: What the repair did to ONE persisted loss slot. Every slot lands in
#: exactly one of these, and the command's summary reports all five — a
#: pass that cannot re-derive a slot must say so rather than fold it into
#: a silent skip, because "nothing happened here" and "this slot is beyond
#: repair" call for different operator action.
_SlotOutcome = Literal[
    "repaired",
    "already populated",
    "no per-judge drift on record",
    "no drift counts and no events transcript",
    "unreadable loss profile",
]

#: Summary order, taken from :data:`_SlotOutcome`'s own declaration order so
#: the reported rows cannot drift from the outcomes a slot can land in: what
#: was fixed, then what needed nothing, then what could not be fixed.
_OUTCOME_ORDER: tuple[_SlotOutcome, ...] = get_args(_SlotOutcome)


def _repair_slot(loss_path: Path, weights: ScoringWeights) -> _SlotOutcome:
    """Re-derive ``per_judge_loss`` for ONE persisted loss slot and write it back.

    Writes the file only when the re-derived attribution differs from the
    persisted one, so a slot that is already correct is left byte-for-byte
    unchanged and the pass is idempotent by construction.

    The attribution is derived from the slot's already-bucketed
    :attr:`LossProfile.drift_counts`, which carry the ``custom:<judge_name>``
    drift kind :func:`zicato.telemetry.reducer.compute_per_judge_loss` is
    keyed on. A profile written with no drift_counts at all (one predating
    the metric surface, or an older reducer crash) falls back to re-tallying
    THIS slot's own transcript
    (:func:`zicato.tournament.unit_cache.unit_events_path`): replicate 0
    reads ``events.jsonl`` and replicate ``n`` reads ``events.r{n}.jsonl``,
    so a replicate is never attributed from another replicate's drift. Only
    ``per_judge_loss`` is written back — a drift_counts tuple rebuilt from
    the transcript feeds the attribution and is not itself persisted, so the
    repair never invents counts the reducer did not record.

    A slot with neither source is reported as such, not skipped: a workspace
    written before the transcript was replicate-keyed has no
    ``events.r{n}.jsonl`` for its r>0 slots, and those stay unrepairable
    however often the pass runs.
    """
    try:
        profile = read_loss_profile(loss_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return "unreadable loss profile"

    drift_counts: tuple[DriftCount, ...] = profile.drift_counts
    if not drift_counts:
        events_path = unit_events_path(loss_path)
        if not events_path.exists():
            return "no drift counts and no events transcript"
        tally = _drift_counts_from_events(events_path)
        if tally:
            drift_counts = tuple(
                DriftCount(kind=kind, severity=sev, count=count)  # type: ignore[arg-type]
                for (kind, sev), count in sorted(tally.items())
            )

    per_judge_loss = compute_per_judge_loss(drift_counts, weights)
    if per_judge_loss == profile.per_judge_loss:
        return "already populated" if per_judge_loss else "no per-judge drift on record"
    write_loss_profile(replace(profile, per_judge_loss=per_judge_loss), loss_path)
    return "repaired"


@click.command(
    name="repair-judge-losses",
    short_help="Advanced: backfill per_judge_loss into every persisted loss slot.",
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
        "Re-ingest each run whose canonical loss.json is readable into "
        "index.db so the judge_losses table is populated without a full "
        "`zicato repair index`."
    ),
)
def repair_judge_losses_cmd(workspace: str, reingest: bool) -> None:
    """Advanced: backfill per_judge_loss into existing runs.

    Off the happy path. Walks every run directory on disk and repairs
    every persisted loss slot it holds — the canonical loss.json and
    each replicate sibling loss.r{n}.json, degraded measurement bands
    included, attempt siblings never. Each slot's per-judge
    weighted-loss attribution is re-derived from its own drift_counts,
    falling back to its own replicate-keyed events transcript, and
    written back. Idempotent: a slot that already agrees is left
    unchanged.

    The summary counts every slot by what happened to it, canonical and
    replicate separately, including the slots that could not be
    re-derived because neither source survives.

    Scope: this repairs the on-disk records. The analytical index
    projects canonical slots only, so `judge_losses` rows come from
    replicate 0; a repaired replicate slot is correct on disk and
    carries no index row by design. Each run with a readable canonical
    profile is re-ingested by default.
    """
    ws = Path(workspace).resolve()
    weights_cache: dict[str, ScoringWeights] = {}
    # outcome -> (canonical slots, replicate slots)
    tally: dict[_SlotOutcome, list[int]] = {name: [0, 0] for name in _OUTCOME_ORDER}
    run_dirs = 0
    for epoch_id, generation_id, entry_id in _iter_runs(ws):
        if epoch_id not in weights_cache:
            weights_cache[epoch_id] = _load_scoring_for_epoch(ws, epoch_id)
        weights = weights_cache[epoch_id]
        run_dir = loss_profile_path(ws, epoch_id, generation_id, entry_id).parent
        slots = persisted_loss_slots(run_dir)
        if not slots:
            continue
        run_dirs += 1
        canonical_readable = False
        for replicate_index, loss_path in slots:
            outcome = _repair_slot(loss_path, weights)
            tally[outcome][0 if replicate_index == 0 else 1] += 1
            if replicate_index == 0 and outcome != "unreadable loss profile":
                canonical_readable = True
        # The index is a projection of the canonical slot, so the re-ingest
        # is per RUN and unconditional on whether this pass changed the
        # file: a workspace whose loss.json was already populated but whose
        # judge_losses rows never landed is exactly what --reingest is for.
        if reingest and canonical_readable:
            try:
                ingest_run(ws, None, epoch_id, generation_id, entry_id)
            except Exception as exc:  # noqa: BLE001 — best-effort repair
                click.echo(
                    f"  warning: re-ingest of "
                    f"{epoch_id}/{generation_id}/{entry_id} failed: {exc}",
                    err=True,
                )
    total = sum(sum(pair) for pair in tally.values())
    click.echo(
        f"Per-judge loss repair under {ws}: "
        f"{total} loss {'slot' if total == 1 else 'slots'} in "
        f"{run_dirs} run {'directory' if run_dirs == 1 else 'directories'}."
    )
    width = max(len(name) for name in _OUTCOME_ORDER)
    for name in _OUTCOME_ORDER:
        canonical, replicate = tally[name]
        click.echo(
            f"  {name:<{width}}  {canonical + replicate:>5}  "
            f"(canonical {canonical}, replicate {replicate})"
        )


__all__ = ["repair_judge_losses_cmd"]
