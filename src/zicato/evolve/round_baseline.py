"""Mutation snapshot and baseline lifecycle ownership."""

# ruff: noqa: E402
from __future__ import annotations

import json
import logging
import time  # noqa: F401  — kept as the ``orch.time`` clock seam (see __all__)
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.loss import has_execution_evidence, validate_loss_identity
from zicato.core.measurement import (
    artifact_replicate_index,
    iter_measurement_artifacts,
    measurement_artifact_path,
    recorded_artifact_measurement,
)
from zicato.core.workspace import (
    generation_dir,
)
from zicato.evolve.epoching import (
    _roll_seed_marker,
)
from zicato.evolve.ingest import (
    _index_db_path,
)
from zicato.evolve.lifecycle_services import (
    _now_iso,
)
from zicato.tournament.scoring import read_gen_score, write_gen_score
from zicato.util import best_effort
from zicato.workspace.layout import WorkspaceLayout

if TYPE_CHECKING:
    from zicato.runtime.lock import WorkspaceLock


log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


def _recorded_generation_ids(workspace_root: Path, epoch_id: str) -> list[str]:
    """Generation ids from the epoch's RECORD directories — no source trees involved.

    ``epochs/{id}/generations/{gen}/`` is written by the journal under both
    storage backends and survives source pruning
    (:mod:`zicato.epoch.gc`), so it is the durable answer to "has this
    epoch minted a generation".
    """
    from zicato.workspace import WorkspaceLayout, generation_ids  # noqa: PLC0415

    return generation_ids(WorkspaceLayout.from_root(workspace_root), epoch_id)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a UTF-8 record through the shared synchronized writer."""
    from zicato.storage._atomic import atomic_write_text as _atomic_write_text_impl  # noqa: PLC0415

    _atomic_write_text_impl(path, text)


def _dump_mutations_snapshot(
    workspace_root: Path,
    epoch_id: str,
    mutations: list[Any],
) -> None:
    """Serialize the round's enumerated mutation points to ``mutations.json``.

    Writes a JSON array of objects ``{id, kind, file, line_start,
    line_end, content, content_hash}`` — i.e. :func:`dataclasses.asdict`
    of each :class:`zicato.core.types.MutationPoint` with the ``Path``
    fields stringified — to ``epochs/{epoch_id}/mutations.json``. The
    write is atomic (``.tmp`` + :func:`os.replace`).

    Best-effort: any failure (a serialisation error, an I/O error) is
    swallowed at ``debug`` level so a broken snapshot can never abort the
    evolve round. The proposer has already been fed the in-memory
    ``mutations`` list by the time this runs; the on-disk file is purely
    for the dashboard.
    """
    import dataclasses as _dataclasses  # noqa: PLC0415
    import os as _os  # noqa: PLC0415

    from zicato.core.workspace import mutations_json_path  # noqa: PLC0415

    with best_effort(
        "mutations.json snapshot",
        on_error=lambda exc: log.debug("mutations.json snapshot skipped: %s", exc),
    ):
        payload: list[dict[str, Any]] = []
        for point in mutations:
            raw = _dataclasses.asdict(point)
            payload.append(
                {
                    "id": raw["id"],
                    "kind": raw["kind"],
                    "file": str(raw["file"]),
                    "line_start": raw["line_start"],
                    "line_end": raw["line_end"],
                    "content": raw["content"],
                    "content_hash": raw["content_hash"],
                }
            )
        target = mutations_json_path(workspace_root, epoch_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        _os.replace(tmp, target)


def _ensure_baseline_snapshot(
    workspace_root: Path,
    epoch_id: str,
    workspace_config: Any,
    *,
    writer: WorkspaceLock,
) -> None:
    """Finish baseline initialization from retained source under its writer."""
    from zicato.epoch.baseline import finish_baseline_seed, prepare_baseline_seed
    from zicato.epoch.genstore import default_generation_store
    from zicato.epoch.publication import BaselineSeed
    from zicato.runtime.lock import validate_workspace_lock

    validate_workspace_lock(writer, workspace_root)
    store = default_generation_store(workspace_root)
    seed = BaselineSeed.read(workspace_root, epoch_id)
    if seed is None:
        generations = _recorded_generation_ids(workspace_root, epoch_id)
        if store.list_generations(epoch_id) or generations:
            if store.has_generation(epoch_id, "v0"):
                baseline = generation_dir(workspace_root, epoch_id, "v0")
                if not (baseline / "experiment.json").is_file():
                    raise RuntimeError(
                        f"epoch {epoch_id} has baseline source without completed initialization; "
                        "preserve its source and repair the missing seed records before evolving"
                    )
            return
        source_coordinates: tuple[str, str] | None = None
        sources: list[Path] = []
        marker = _roll_seed_marker(workspace_root, epoch_id)
        if marker.exists():
            source = Path(marker.read_text(encoding="utf-8").strip())
            if not source.is_dir():
                raise FileNotFoundError(f"baseline predecessor snapshot is missing: {source}")
            sources = sorted(source.iterdir())
            source_coordinates = _source_epoch_generation(source)
        if not sources:
            from zicato.core.adapter_config import registered_mutable_trees

            raw = registered_mutable_trees(workspace_config, workspace_root)
            if not raw:
                raise RuntimeError(
                    "evolve_once: workspace_config has no 'mutable_trees' / 'source_roots' — "
                    "cannot seed a v0 baseline snapshot; run `zicato epoch register` first"
                )
            sources = [Path(item) for item in raw]
        seed = prepare_baseline_seed(
            workspace_root,
            epoch_id,
            sources,
            backend=store.backend_name,
            created_at=_now_iso(),
            source_coordinates=source_coordinates,
        )
        seed.write(workspace_root)
    finish_baseline_seed(workspace_root, seed, writer=writer)
    if seed.source_epoch is not None and seed.source_generation is not None:
        _materialize_carried_champion(
            workspace_root,
            epoch_id=epoch_id,
            generation_id="v0",
            source_epoch=seed.source_epoch,
            source_generation=seed.source_generation,
        )


def _source_epoch_generation(seed_source: Path) -> tuple[str, str] | None:
    """Derive ``(source_epoch, source_generation)`` from a roll-seed snapshot path.

    The cross-epoch roll-seed marker points at the predecessor's
    promoted-head snapshot directory, of the form
    ``…/epochs/<epoch>/generations/<gen>/snapshot``. This recovers the
    ``(epoch, generation)`` pair so the champion's prior losses can be
    materialised into the new epoch with honest provenance. Returns
    ``None`` when the path does not match the expected layout (a
    hand-built marker, a future relayout) — materialisation is then
    skipped, which is a clean degrade rather than a crash.
    """
    parts = seed_source.parts
    try:
        # …/epochs/<epoch>/generations/<gen>/snapshot
        snap_i = len(parts) - 1 - parts[::-1].index("snapshot")
    except ValueError:
        return None
    # Expect ["generations", <gen>, "snapshot"] ending and an "epochs"
    # marker two levels above the generation id.
    if snap_i < 4 or parts[snap_i - 2] != "generations" or parts[snap_i - 4] != "epochs":
        return None
    source_generation = parts[snap_i - 1]
    source_epoch = parts[snap_i - 3]
    return source_epoch, source_generation


def _materialize_carried_champion(
    workspace_root: Path,
    *,
    epoch_id: str,
    generation_id: str,
    source_epoch: str,
    source_generation: str,
) -> None:
    """Copy a carried-over champion's per-board losses + aggregate into this epoch.

    Best-effort. Reads every per-board ``loss.json`` (and per-replicate
    ``loss.r<r>.json``) the champion produced in ``source_epoch`` /
    ``source_generation`` and rewrites each into THIS epoch's
    ``generations/<generation_id>/runs/<entry>/`` with ``cached=True`` and
    ``source_epoch`` / ``source_run`` provenance (``source_run`` is the
    original run id, so the trail back to the live evaluation survives).
    The champion's ``gen_score.json`` aggregate is likewise copied with the
    same provenance fields so a fast first round reuses it. Each
    materialised run is folded into the analytical index so the champion
    reads as scored-but-cached within the epoch (the index's ``cached``
    column keeps it from being double-counted as a fresh evaluation).

    A missing source (the predecessor never scored its head), an
    unreadable file, or an absent reducer degrades to "materialise what we
    can" — never an abort. The champion's run id in the new epoch keeps
    the canonical ``{generation_id}--{entry_id}`` form so the cache-first
    runner finds it as a hit.
    """
    from zicato.core.workspace import run_dir, run_id_for_unit  # noqa: PLC0415
    from zicato.tournament.unit_cache import is_unit_attempt_slot  # noqa: PLC0415

    try:
        from zicato.telemetry.reducer import (  # noqa: PLC0415
            read_loss_profile,
            write_loss_profile,
        )
    except ImportError as exc:
        # The reducer (de)serialisers are unavailable in this environment
        # (e.g. a test that stubs out ``zicato.telemetry``). Materialising
        # carried losses is best-effort — degrade to "carry nothing"
        # rather than aborting the epoch's baseline seed.
        log.debug("materialise champion: reducer unavailable (%s); skipping", exc)
        return

    from zicato.workspace import WorkspaceLayout, run_entry_ids  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace_root)
    materialised_entries: list[str] = []
    measurements_complete = True
    for entry_id in run_entry_ids(layout, source_epoch, source_generation):
        entry_dir = layout.run_dir(source_epoch, source_generation, entry_id)
        dst_run_dir = run_dir(workspace_root, epoch_id, generation_id, entry_id)
        any_for_entry = False
        # Canonical loss.json (replicate 0) + any loss.r<r>.json siblings.
        # Attempt siblings are excluded: they describe a superseded
        # execution in the SOURCE epoch, and carrying one forward would
        # present it as this generation's measurement.
        for src_loss in iter_measurement_artifacts(entry_dir):
            if is_unit_attempt_slot(src_loss):
                continue
            try:
                replicate = artifact_replicate_index(src_loss.name)
                if replicate is None:
                    continue
                profile = read_loss_profile(src_loss)
                measurement = recorded_artifact_measurement(
                    entry_dir, src_loss, profile.measurement, profile.match_id
                )
                validate_loss_identity(
                    profile,
                    epoch_id=source_epoch,
                    generation_id=source_generation,
                    entry_id=entry_id,
                    measurement=measurement,
                )
                if not has_execution_evidence(profile):
                    measurements_complete = False
                    continue
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                measurements_complete = False
                log.debug("materialise champion: ineligible measurement %s: %s", src_loss, exc)
                continue
            carried = replace(
                profile,
                measurement=measurement,
                generation_id=generation_id,
                epoch_id=epoch_id,
                run_id=run_id_for_unit(
                    generation_id, entry_id, replicate, base_seed=measurement.base_seed
                ),
                cached=True,
                source_epoch=source_epoch,
                source_run=profile.run_id,
            )
            try:
                write_loss_profile(
                    carried,
                    measurement_artifact_path(
                        dst_run_dir, "loss", replicate, base_seed=measurement.base_seed
                    ),
                )
                any_for_entry = True
            except OSError as exc:
                log.debug("materialise champion: write %s skipped: %s", src_loss.name, exc)
        if any_for_entry:
            materialised_entries.append(entry_id)

    # Carry the aggregate (gen_score.json) with the same provenance so a
    # fast first round reuses the champion rather than re-running it.
    score = read_gen_score(layout, source_epoch, source_generation)
    if measurements_complete and score is not None:
        raw = score.to_dict()
        raw.update(
            generation_id=generation_id,
            cached=True,
            source_epoch=source_epoch,
            source_run=source_generation,
        )
        write_gen_score(workspace_root, epoch_id, generation_id, raw)

    # Fold the materialised runs into the analytical index so the champion
    # reads as scored-but-cached within the epoch.
    for entry_id in materialised_entries:
        try:
            from zicato.index.ingest import ingest_run  # noqa: PLC0415

            ingest_run(
                workspace_root,
                _index_db_path(workspace_root),
                epoch_id,
                generation_id,
                entry_id,
            )
        except ImportError:
            break
        except Exception as exc:  # noqa: BLE001 — index dual-write is best-effort
            log.debug("materialise champion: index ingest %s skipped: %s", entry_id, exc)
    if materialised_entries:
        log.info(
            "epoch %s: materialised carried champion %s from %s/%s (%d board entries, cached)",
            epoch_id,
            generation_id,
            source_epoch,
            source_generation,
            len(materialised_entries),
        )


def _load_historical_aggregate(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Read the parent's cached ``gen_score.json``.

    Raises :class:`FileNotFoundError` when the cache is missing — fast
    mode is meaningless without a parent aggregate.
    """
    layout = WorkspaceLayout.from_root(workspace_root)
    score = read_gen_score(layout, epoch_id, generation_id)
    if score is None:
        path = layout.gen_score(epoch_id, generation_id)
        raise FileNotFoundError(
            f"fast-mode evolve needs a cached parent aggregate at {path}; "
            "run a full round for the parent generation first"
        )
    raw = score.to_dict()
    raw.setdefault("generation_id", generation_id)
    return raw
