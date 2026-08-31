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

from zicato.core.types import (
    Generation,
)
from zicato.core.workspace import (
    generation_dir,
)
from zicato.evolve import generation_phase
from zicato.evolve.epoching import (
    _roll_seed_marker,
)
from zicato.evolve.ingest import (
    _cache_gen_score,
    _index_db_path,
)
from zicato.evolve.lifecycle_services import (
    _now_iso,
)
from zicato.util import best_effort

if TYPE_CHECKING:
    # Annotation-only — the proposer module is imported lazily inside
    # ``evolve_once`` (see the module docstring on lazy imports), so its
    # exception type is referenced here purely for type annotations.
    pass

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


def _recorded_generation_ids(workspace_root: Path, epoch_id: str) -> list[str]:
    """Generation ids from the epoch's RECORD directories — no source trees involved.

    ``epochs/{id}/generations/{gen}/`` is written by the journal under both
    storage backends and survives source pruning
    (:mod:`zicato.epoch.gc`), so it is the durable answer to "has this
    epoch minted a generation".
    """
    from zicato.workspace import WorkspaceLayout  # noqa: PLC0415

    gens_root = WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)
    try:
        return sorted(child.name for child in gens_root.iterdir() if child.is_dir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (``.tmp`` + :func:`os.replace`).

    Delegates to the single atomic-write definition in
    :mod:`zicato.storage._atomic` so there is one ``.tmp`` + ``fsync`` +
    rename implementation in the codebase.
    """
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
) -> None:
    """Seed a ``v0`` snapshot for the epoch if no generations exist yet.

    Two seed sources, in priority order:

    1. **Cross-epoch lineage seed.** When the epoch was created by a
       contract-roll, :func:`ensure_epoch_for_contract` leaves a
       ``v0_seed_from`` marker pointing at the previous epoch's
       promoted-head snapshot. The new epoch's ``v0`` is seeded from
       that snapshot so the lineage continues from the best result of
       the old epoch rather than restarting from the registered
       source.
    2. **Registered mutable trees.** The default for a fresh, non-rolled
       epoch (or a rolled epoch whose predecessor had no promoted
       generation beyond v0). Each registered ``mutable_trees`` root is
       copied under ``epochs/{epoch}/generations/v0/snapshot/{name}/``.

    Subsequent invocations are a no-op when ``v0`` already exists.

    The seed snapshot is also recorded in lineage (as the unparented
    promoted head) and marked as the current generation; the same
    bookkeeping the post-promotion path performs after every successful
    round. This keeps lineage truthful when the epoch is later
    summarised by the analysis pass.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    store = default_generation_store(workspace_root)
    # Existence is a RECORD question, not a source question. The store lists
    # source-bearing generations only, so an epoch whose v0 snapshot has been
    # pruned lists nothing — and re-seeding on that answer would write a
    # fresh v0 source under the surviving v0 records, silently pairing this
    # epoch's decisions with a tree that never produced them. The generation
    # record directories survive pruning by design, so they are what says
    # whether this epoch has already been seeded.
    if store.list_generations(epoch_id) or _recorded_generation_ids(workspace_root, epoch_id):
        return  # already have at least one generation; nothing to do

    # Priority 1 — cross-epoch lineage seed left by a contract-roll.
    # The seed marker points at the *snapshot directory* of the
    # predecessor epoch's promoted head; its CHILDREN become the new
    # v0's top-level trees (the roll continues the lineage rather than
    # nesting it one level deeper). seed_generation copies each source
    # under its basename, so handing it the children reproduces the
    # pre-seam flatten-into-v0 behaviour.
    seed_marker = _roll_seed_marker(workspace_root, epoch_id)
    seeded_from_roll = False
    roll_source: tuple[str, str] | None = None  # (source_epoch, source_generation)
    if seed_marker.exists():
        seed_text = seed_marker.read_text(encoding="utf-8").strip()
        seed_source = Path(seed_text) if seed_text else None
        if seed_source is not None and seed_source.exists():
            store.seed_generation(epoch_id, "v0", sorted(seed_source.iterdir()))
            seeded_from_roll = True
            roll_source = _source_epoch_generation(seed_source)
            log.info(
                "epoch %s: seeded v0 from rolled predecessor snapshot %s",
                epoch_id,
                seed_source,
            )

    # Priority 2 — registered mutable trees.
    if not seeded_from_roll:
        raw_trees = (
            workspace_config.get("mutable_trees") or workspace_config.get("source_roots") or []
        )
        if not raw_trees:
            raise RuntimeError(
                "evolve_once: workspace_config has no 'mutable_trees' / "
                "'source_roots' — cannot seed a v0 baseline snapshot. "
                "Run `zicato epoch register --mutable-tree ...` first."
            )
        # seed_generation copies each registered tree under its basename
        # and raises FileNotFoundError for a missing source — the same
        # contract the inline loop enforced.
        store.seed_generation(epoch_id, "v0", [Path(raw) for raw in raw_trees])

    snapshot_root = store.materialize_snapshot(epoch_id, "v0")

    # Lineage + current-generation marker so the orchestrator's
    # downstream readers see a clean baseline state.
    from zicato.epoch import append_to_lineage  # noqa: PLC0415

    baseline_gen = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snapshot_root,
        created_at=_now_iso(),
        promoted=True,
    )
    append_to_lineage(workspace_root, epoch_id, baseline_gen, parent_id=None)
    generation_phase.set_current_generation(workspace_root, epoch_id, "v0")

    # Synthetic ``experiment.json`` for v0 so every downstream consumer
    # (the analyzer report data loader, the index dual-write, the
    # dashboard lineage walker) sees a uniform on-disk shape. The seed is
    # not a proposer experiment; the marker carries a "baseline seed"
    # hypothesis and a null outcome (no tournament round produced it).
    # Idempotent — safe to call again on a workspace whose v0 already
    # has the marker.
    from zicato.epoch.journal import write_seed_experiment  # noqa: PLC0415

    write_seed_experiment(
        workspace_root,
        epoch_id,
        "v0",
        proposed_at=baseline_gen.created_at,
    )

    # Champion self-containment: when this epoch carried the champion
    # forward from a rolled predecessor, MATERIALISE the carried-over
    # per-board losses + aggregate into the new epoch's ``v0`` gen dir,
    # each tagged ``cached: true`` with ``source_epoch`` / ``source_run``
    # provenance. Without this the champion would be a hollow shell — only
    # ``experiment.json`` + ``snapshot/`` — while the challengers carry
    # their ``loss.json`` files, so the epoch would not be self-contained
    # and a fast first round would degrade to a full champion re-run. With
    # the losses materialised, the champion is consistent with the
    # challengers (both materialised per-board, distinguished only by the
    # ``cached`` provenance) and the cache-first runner reuses it from the
    # very first round.
    if roll_source is not None:
        _materialize_carried_champion(
            workspace_root,
            epoch_id=epoch_id,
            generation_id="v0",
            source_epoch=roll_source[0],
            source_generation=roll_source[1],
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

    src_gen_dir = generation_dir(workspace_root, source_epoch, source_generation)
    src_runs_root = src_gen_dir / "runs"
    materialised_entries: list[str] = []
    if src_runs_root.exists():
        for entry_dir in sorted(p for p in src_runs_root.iterdir() if p.is_dir()):
            entry_id = entry_dir.name
            dst_run_dir = run_dir(workspace_root, epoch_id, generation_id, entry_id)
            any_for_entry = False
            # Canonical loss.json (replicate 0) + any loss.r<r>.json siblings.
            # Attempt siblings are excluded: they describe a superseded
            # execution in the SOURCE epoch, and carrying one forward would
            # present it as this generation's measurement.
            for src_loss in sorted(entry_dir.glob("loss*.json")):
                if is_unit_attempt_slot(src_loss):
                    continue
                try:
                    replicate = 0 if src_loss.name == "loss.json" else int(src_loss.stem[6:])
                    profile = read_loss_profile(src_loss)
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    log.debug("materialise champion: unreadable %s: %s", src_loss, exc)
                    continue
                carried = replace(
                    profile,
                    generation_id=generation_id,
                    epoch_id=epoch_id,
                    run_id=run_id_for_unit(generation_id, entry_id, replicate),
                    cached=True,
                    source_epoch=source_epoch,
                    source_run=profile.run_id,
                )
                try:
                    write_loss_profile(carried, dst_run_dir / src_loss.name)
                    any_for_entry = True
                except OSError as exc:
                    log.debug("materialise champion: write %s skipped: %s", src_loss.name, exc)
            if any_for_entry:
                materialised_entries.append(entry_id)

    # Carry the aggregate (gen_score.json) with the same provenance so a
    # fast first round reuses the champion rather than re-running it.
    src_score = src_gen_dir / "gen_score.json"
    if src_score.exists():
        try:
            raw = json.loads(src_score.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("materialise champion: gen_score read skipped: %s", exc)
            raw = None
        if isinstance(raw, dict):
            raw["generation_id"] = generation_id
            raw["cached"] = True
            raw["source_epoch"] = source_epoch
            raw["source_run"] = source_generation
            _cache_gen_score(workspace_root, epoch_id, generation_id, raw)

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
    gdir = generation_dir(workspace_root, epoch_id, generation_id)
    path = gdir / "gen_score.json"
    if not path.exists():
        raise FileNotFoundError(
            f"fast-mode evolve needs a cached parent aggregate at {path}; "
            "run a full round for the parent generation first"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object at top level")
    raw.setdefault("generation_id", generation_id)
    return raw
