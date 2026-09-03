"""Conservative crash-resume protocol for ``zicato evolve``.

When ``evolve`` is killed mid-tournament (operator SIGTERM, host reboot,
OOM) the durable artifacts under ``epochs/{epoch}/`` survive — promoted
generations, the journal, ``experiment.json``, and any per-board
``loss.json`` a completed run wrote (see RUNTIME.md §4.1). The live
``runtime/`` state (heartbeat, active runs, active tournament) does
not; it is rebuilt from scratch on restart.

This module first completes every durable field-settlement receipt. It then
decides whether the most recent un-outcomed generation can resume in place or
must be discarded.

The single design rule is **conservatism** (RUNTIME.md §4.2): *when it
cannot tell exactly what state things are in, it discards the partial
work and re-runs.* The cost of a wasted re-run is one round; the cost of
a wrong inference is journal / lineage corruption.

Why resume is nearly free
-------------------------
A board unit is cached by ``(generation_id, entry_id, replicate)`` —
its ``loss.json`` on disk IS the cache (see
:func:`zicato.tournament.runner._resolve_cached_unit`). A generation
under a fixed contract is immutable, so a completed unit's ``loss.json``
is a permanent cache HIT. Resuming a tournament is therefore largely
"re-enter the loop and let the cache hit the done units": the resumed
round re-runs only the entries that have no ``loss.json`` yet.

The load-bearing safety invariant
----------------------------------
The unit cache key does **not** include the patch set. So reusing a
generation's ``loss.json`` files is only sound if the *same patches*
produced the snapshot those units ran against. That is exactly why
resume-in-place reuses the **persisted** ``experiment.json`` + patches
rather than re-proposing (the proposer is non-deterministic — a fresh
proposal would yield a different snapshot, and the old ``loss.json``
would then be stale-but-cached: silent corruption). Whenever the
persisted experiment is absent, unreadable, or its
snapshot cannot be reconciled with the patches, this module DISCARDS
the generation directory so the next round re-proposes cleanly into a
fresh ``vN`` — never reusing a unit cache it cannot vouch for.

Lineage / journal safety
------------------------
An applied generation enters ``lineage.json`` immediately with
``promoted=null``. Settlement later resolves that same node to ``true`` or
``false`` and appends its journal section. Discarding an interrupted
single-challenger generation therefore removes its pending lineage node as
well as its directory. If a multi-challenger field reached a decision, its
durable settlement receipt resolves every sibling together; without a
receipt, recovery discards every pending sibling in the field.

Source state can precede both canonical registers. ``derive_generation``
commits the candidate's tree before the pending lineage node is written. A
stop inside that interval therefore leaves a source generation that neither a
record directory nor lineage names — under the Git backend, a tag and a
worktree living outside ``epochs/`` entirely. Startup discards that source
through the configured store once settlement receipts have replayed and
before it classifies any generation, which keeps the identifier the next
proposal allocates free of an earlier commit.

Scope
-----
Tournament execution resumes in place only for one challenger with a readable
experiment and completed board units. Settlement recovery covers every
tournament structure because its persisted receipt contains the final decision
and requires no new evaluation.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.runtime.paths import (
    active_runs_dir,
    active_tournament_log_path,
    active_tournament_path,
    heartbeat_path,
)
from zicato.workspace import (
    WorkspaceLayout,
    generation_ids,
    generation_round_number,
    run_entry_ids,
)

if TYPE_CHECKING:
    from zicato.core.types import Experiment
    from zicato.epoch.genstore import GenerationStore

log = logging.getLogger("zicato.runtime.resume")

#: The epoch's seed generation. Its source tree is materialised before either
#: canonical register names it, and a workspace whose baseline record was lost
#: keeps a valid tree that ``zicato repair v0-baseline`` rewrites the record
#: for, so the baseline is never treated as unrecorded source.
_BASELINE_GENERATION_ID = "v0"


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """The outcome of reconciling an interrupted workspace.

    Fields
    ------
    resume_generation_id:
        When non-``None``, the orchestrator should resume this
        generation *in place* — reuse its persisted ``experiment.json`` and
        patches, and run the tournament, which cache-HITs
        every board unit that already has a ``loss.json``. ``None`` means
        there is nothing to resume in place: either the workspace was
        clean, or the partial work was discarded and the next round runs
        fresh and byte-identical to the default path.
    resume_experiment:
        The persisted :class:`~zicato.core.types.Experiment` to reuse
        when ``resume_generation_id`` is set; ``None`` otherwise. Carried
        on the plan so the orchestrator does not re-read it.
    discarded_generation_id:
        The generation directory that was deleted as ambiguous partial
        work, or ``None`` when nothing was discarded. Informational — for
        logging / tests.
    classification:
        A short symbolic label for what the protocol found, for logging
        and tests. One of ``"clean"``, ``"resume_tournament"``,
        ``"discard_partial_proposal"``, ``"discard_unapplied"``,
        ``"discard_no_progress"``, ``"discard_garbled"``, or
        ``"discard_unrecorded_field"``.
    """

    resume_generation_id: str | None = None
    resume_experiment: Experiment | None = None
    discarded_generation_id: str | None = None
    classification: str = "clean"

    @property
    def resumes_in_place(self) -> bool:
        """True iff the orchestrator should reuse a persisted experiment."""
        return self.resume_generation_id is not None and self.resume_experiment is not None


def _generations_root(workspace_root: Path, epoch_id: str) -> Path:
    return WorkspaceLayout.from_root(workspace_root).generations_dir(epoch_id)


def _latest_generation_id(workspace_root: Path, epoch_id: str) -> str | None:
    """Return the highest ``vN`` generation directory, or ``None``.

    Mirrors the liveness rule of
    :func:`zicato.evolve.generation_phase.next_generation_id` and the directory
    store: a generation counts as present when its directory exists.
    Non-``vN`` names (none today) are ignored.
    """
    numbered = [
        generation_id
        for generation_id in generation_ids(WorkspaceLayout.from_root(workspace_root), epoch_id)
        if generation_round_number(generation_id) is not None
    ]
    return numbered[-1] if numbered else None


def clear_runtime_state(workspace_root: Path) -> None:
    """Discard the live ``runtime/`` state of a prior, dead evolve.

    Removes ``heartbeat.json``, the active-tournament event log AND any
    ``active_tournament.json`` snapshot beside it, and every
    ``active_runs/{run_id}.json`` — the files RUNTIME.md §4.1 lists as
    "discarded on restart". The workspace lock is NOT touched here; the
    orchestrator's lock acquisition already stole any stale lock before
    this runs (and holds its own). Best-effort: a missing file is fine,
    and an unlink race never aborts startup.

    The per-run files are the durable record a dead worker left behind;
    the conservative protocol treats them as gone (the worker process is
    long dead) rather than trying to reattach to a pid that no longer
    exists. Their generations' ``loss.json`` outputs, if any completed,
    survive under ``epochs/`` and are picked up by the unit cache.
    """
    for path in (
        heartbeat_path(workspace_root),
        active_tournament_path(workspace_root),
        active_tournament_log_path(workspace_root),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 — runtime cleanup is best-effort
            log.debug("resume: could not remove %s: %s", path, exc)
    runs_dir = active_runs_dir(workspace_root)
    if runs_dir.is_dir():
        for child in runs_dir.iterdir():
            try:
                if child.is_file():
                    child.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 — best-effort
                log.debug("resume: could not remove active run %s: %s", child, exc)


def discard_interrupted_generation(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    *,
    store: GenerationStore | None = None,
    round_index: int | None = None,
) -> None:
    """Delete one interrupted generation and its pending lineage node.

    Candidate creation records ``promoted=null`` before experiment.json. The
    pending node supplies the birth round when the experiment is unreadable.
    """
    matches = [
        (recorded_round, generation_ids)
        for (recorded_round, _parent_id), generation_ids in _pending_lineage_groups(
            workspace_root, epoch_id
        ).items()
        if generation_id in generation_ids
    ]
    if len(matches) > 1:
        raise RuntimeError(f"lineage contains duplicate generation id {generation_id!r}")
    if matches:
        recorded_round, siblings = matches[0]
        if len(siblings) != 1:
            raise RuntimeError("single-generation cleanup received an unresolved field")
        if round_index is not None and round_index != recorded_round:
            raise RuntimeError(f"generation {generation_id!r} has conflicting round coordinates")
        round_index = recorded_round
    if store is None:
        from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

        store = default_generation_store(workspace_root)
    _discard_candidates(
        workspace_root,
        epoch_id,
        ((round_index, (generation_id,)),),
        {generation_id} if matches else set(),
        store,
    )


def _prune_generation_records(
    workspace_root: Path,
    epoch_id: str,
    generation_ids_to_remove: tuple[str, ...],
    store: GenerationStore,
) -> None:
    """Remove source and record directories, then verify both are absent."""
    store.prune_generations(epoch_id, generation_ids_to_remove, dry_run=False)
    for generation_id in generation_ids_to_remove:
        if (
            store.has_generation(epoch_id, generation_id)
            or store.snapshot_path(epoch_id, generation_id).exists()
        ):
            raise RuntimeError(f"generation source cleanup failed for {epoch_id}/{generation_id}")
        generation_dir = _generations_root(workspace_root, epoch_id) / generation_id
        if generation_dir.exists():
            shutil.rmtree(generation_dir)
        if generation_dir.exists():
            raise RuntimeError(f"generation record cleanup failed for {epoch_id}/{generation_id}")


def _lineage_generation_rows(workspace_root: Path, epoch_id: str) -> list[dict[str, Any]]:
    """Every generation node one epoch's lineage holds, resolved or pending."""
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    epoch = next(
        (
            row
            for row in load_lineage(workspace_root).get("epochs", [])
            if isinstance(row, dict) and row.get("id") == epoch_id
        ),
        None,
    )
    if epoch is None:
        return []
    return [row for row in epoch.get("generations", []) if isinstance(row, dict)]


def _pending_lineage_groups(
    workspace_root: Path,
    epoch_id: str,
) -> dict[tuple[int, str], tuple[str, ...]]:
    """Return unresolved generations grouped by their immutable field coordinates."""
    groups: dict[tuple[int, str], list[str]] = {}
    promoted_groups: set[tuple[int, str]] = set()
    seen: set[str] = set()
    for row in _lineage_generation_rows(workspace_root, epoch_id):
        generation_id = row.get("id")
        if not isinstance(generation_id, str) or not generation_id or generation_id in seen:
            raise RuntimeError("lineage contains an invalid or duplicate generation id")
        seen.add(generation_id)
        promoted = row.get("promoted")
        if promoted is not None and not isinstance(promoted, bool):
            raise RuntimeError(
                f"lineage generation {generation_id!r} has invalid promoted state {promoted!r}"
            )
        if promoted is False:
            continue
        round_index = row.get("round_index")
        parent_id = row.get("parent_id")
        if (
            not isinstance(round_index, int)
            or isinstance(round_index, bool)
            or round_index < 0
            or not isinstance(parent_id, str)
            or not parent_id
        ):
            if promoted is None:
                raise RuntimeError("pending lineage contains an invalid field identity")
            continue
        key = (round_index, parent_id)
        if promoted is True:
            promoted_groups.add(key)
        else:
            groups.setdefault(key, []).append(generation_id)
    if promoted_groups & groups.keys():
        raise RuntimeError("unrecorded field has both promoted and pending generations")
    return {
        key: tuple(
            sorted(
                generation_ids,
                key=lambda value: (
                    generation_round_number(value)
                    if generation_round_number(value) is not None
                    else 2**31 - 1,
                    value,
                ),
            )
        )
        for key, generation_ids in groups.items()
    }


def _discard_unrecorded_source(
    workspace_root: Path,
    epoch_id: str,
    store: GenerationStore,
) -> tuple[str, ...]:
    """Discard source generations that no canonical record accounts for.

    A candidate's source tree is committed by
    :meth:`~zicato.epoch.genstore.GenerationStore.derive_generation` before the
    round writes its pending lineage node and its ``experiment.json``. Under
    the Git backend that source is a tag and a worktree outside
    ``epochs/{epoch}/generations/``. A process that stops inside that interval
    therefore leaves source that no canonical enumeration reaches, holding an
    identifier the next proposal will allocate. The directory backend cannot reach
    the same state: its source tree lives under the generation record
    directory, so materialising the source creates the record this
    reconciliation looks for.

    A generation is canonical when the epoch holds a record directory for it,
    or lineage holds a node for it. Recovery, promotion and the derived index
    all read those two registers. Source in neither is interrupted candidate
    creation: nothing proves which patches produced it, nothing places it in a
    round, and no evaluation may be cached against it. It is discarded through
    :func:`_prune_generation_records`, which prunes the store — deleting the
    tag, the reusable worktree and any registered temporary checkout, and
    rewinding an epoch branch that ends at a discarded commit — and verifies
    the source is gone. Its record-directory removal is a no-op here by
    construction: an id in neither register has no record directory, and no
    derived-index row either.

    Returns the discarded ids, empty when every source generation is
    accounted for. Idempotent: a second call lists no source for them.
    """
    canonical = set(generation_ids(WorkspaceLayout.from_root(workspace_root), epoch_id)) | {
        generation_id
        for row in _lineage_generation_rows(workspace_root, epoch_id)
        if isinstance(generation_id := row.get("id"), str)
    }
    unrecorded = tuple(
        generation_id
        for generation_id in store.list_generations(epoch_id)
        if generation_id != _BASELINE_GENERATION_ID and generation_id not in canonical
    )
    if not unrecorded:
        return ()
    _prune_generation_records(workspace_root, epoch_id, unrecorded, store)
    log.warning(
        "resume: discarded source generations with no canonical record: %s",
        ", ".join(unrecorded),
    )
    return unrecorded


def _unrecorded_fields_without_receipts(
    workspace_root: Path,
    epoch_id: str,
    store: GenerationStore,
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Find pending entrants whose field has no settlement receipt."""
    from zicato.evolve.settlement_recovery import (  # noqa: PLC0415
        field_settlement_intent_key,
    )
    from zicato.storage import workspace_backend  # noqa: PLC0415

    backend = workspace_backend(workspace_root, start=False)
    field_size = _configured_field_size(workspace_root, epoch_id)
    fields: list[tuple[int, tuple[str, ...]]] = []
    for (round_index, _parent_id), pending in _pending_lineage_groups(
        workspace_root, epoch_id
    ).items():
        cleanup_started = any(
            not store.has_generation(epoch_id, generation_id)
            or not (_generations_root(workspace_root, epoch_id) / generation_id).exists()
            for generation_id in pending
        )
        if field_size <= 1 and len(pending) == 1 and not cleanup_started:
            continue
        if backend.read_json(field_settlement_intent_key(epoch_id, round_index)) is not None:
            continue
        fields.append((round_index, pending))
    return tuple(sorted(fields))


def _configured_field_size(workspace_root: Path, epoch_id: str) -> int:
    """Return the frozen tournament's requested challenger count."""
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.selection.registry import make_strategy  # noqa: PLC0415

    try:
        spec = load_epoch(workspace_root, epoch_id).scoring.tournament_structure
    except FileNotFoundError:
        return 1
    return make_strategy(spec).field_size()


def _field_cleanup_checkpoint(_boundary: str) -> None:
    """Test seam for crashes between field-cleanup durability boundaries."""


def _discard_unrecorded_fields(
    workspace_root: Path,
    epoch_id: str,
    store: GenerationStore,
) -> tuple[str, ...]:
    """Discard every sibling of a field that died before receipt persistence."""
    fields = _unrecorded_fields_without_receipts(workspace_root, epoch_id, store)
    if not fields:
        return ()

    discarded = tuple(generation_id for _, ids in fields for generation_id in ids)
    _discard_candidates(
        workspace_root,
        epoch_id,
        fields,
        set(discarded),
        store,
    )
    log.warning(
        "resume: discarded an unresolved field with no settlement receipt: %s",
        ", ".join(discarded),
    )
    return discarded


def _discard_candidates(
    workspace_root: Path,
    epoch_id: str,
    fields: tuple[tuple[int | None, tuple[str, ...]], ...],
    lineaged_ids: set[str],
    store: GenerationStore,
) -> None:
    """Remove unresolved candidate fields with lineage as the commit marker."""
    from zicato.core.workspace import field_tournament_path  # noqa: PLC0415
    from zicato.epoch.lineage import discard_pending_generations  # noqa: PLC0415

    _invalidate_index_before_field_discard(workspace_root)
    _field_cleanup_checkpoint("index_invalidated")
    rounds_root = WorkspaceLayout.from_root(workspace_root).rounds_dir(epoch_id)
    for round_index, generation_ids_in_round in fields:
        _prune_generation_records(workspace_root, epoch_id, generation_ids_in_round, store)
        if round_index is not None:
            field_tournament_path(workspace_root, epoch_id, generation_ids_in_round[0]).unlink(
                missing_ok=True
            )
            round_dir = rounds_root / str(round_index)
            if round_dir.exists():
                shutil.rmtree(round_dir)
            if round_dir.exists():
                raise RuntimeError(
                    f"round record cleanup failed for {epoch_id}/round {round_index}"
                )
    _field_cleanup_checkpoint("canonical_records_removed")
    removed = discard_pending_generations(workspace_root, epoch_id, lineaged_ids)
    if set(removed) != lineaged_ids:
        raise RuntimeError("candidate cleanup could not remove every pending lineage node")
    _field_cleanup_checkpoint("lineage_committed")


def _invalidate_index_before_field_discard(workspace_root: Path) -> None:
    """Remove every SQLite file before canonical cleanup can make it stale."""
    index_path = workspace_root / "index.db"
    paths = [index_path.with_name(index_path.name + suffix) for suffix in ("", "-wal", "-shm")]
    for path in paths:
        path.unlink(missing_ok=True)
    remaining = [str(path) for path in paths if path.exists()]
    if remaining:
        raise RuntimeError("could not invalidate derived index: " + ", ".join(remaining))


def _has_any_loss(workspace_root: Path, epoch_id: str, generation_id: str) -> bool:
    """True iff at least one board entry has a ``loss.json`` for this gen.

    The presence of even one per-entry ``loss.json`` is the marker that
    the tournament had started and a completed unit is cacheable — the
    signal that makes resume-in-place worth doing instead of a clean
    re-run from scratch.
    """
    layout = WorkspaceLayout.from_root(workspace_root)
    return any(
        layout.loss(epoch_id, generation_id, entry_id).is_file()
        for entry_id in run_entry_ids(layout, epoch_id, generation_id)
    )


def prepare_resume(workspace_root: Path, epoch_id: str) -> ResumePlan:
    """Recover receipts, discard unusable state, and classify one restart.

    Settlement receipts complete before generation inspection. Source
    generations no canonical record accounts for are discarded next, so an
    interrupted derivation cannot hold an identifier a later proposal
    allocates. Pending siblings without a receipt are then discarded together.
    A remaining single challenger resumes only when its experiment, pending
    lineage coordinates, source snapshot, and at least one cached loss all
    agree; every ambiguous state is discarded so the round can be proposed
    again.
    """
    # A field settlement has a complete, replayable receipt before its first
    # outcome write. Finish those commits before classifying generations: an
    # experiment whose outcome already landed can still have pending lineage,
    # journal, champion-marker, or bracket writes.
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415
    from zicato.evolve.settlement_recovery import (  # noqa: PLC0415
        recover_field_settlements,
    )

    store = default_generation_store(workspace_root)
    recover_field_settlements(workspace_root, epoch_id)
    # Receipt replay writes the canonical records a settled field is missing,
    # so source coordinates are compared against the registers only once those
    # writes have landed.
    _discard_unrecorded_source(workspace_root, epoch_id, store)
    discarded_field = _discard_unrecorded_fields(workspace_root, epoch_id, store)
    clear_runtime_state(workspace_root)
    if discarded_field:
        return ResumePlan(
            discarded_generation_id=discarded_field[0],
            classification="discard_unrecorded_field",
        )

    latest = _latest_generation_id(workspace_root, epoch_id)
    if latest is None or latest == _BASELINE_GENERATION_ID:
        # No challenger generation has ever been minted (only the v0
        # seed, or nothing). Nothing to resume — a fresh round runs as
        # always.
        return ResumePlan(classification="clean")

    # Read the experiment marker. We import lazily to keep this module's
    # import graph narrow (the orchestrator pulls these in anyway).
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    experiment: Experiment | None
    try:
        experiment = read_experiment(workspace_root, epoch_id, latest)
    except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
        # No experiment.json (or it is unreadable / a dangling patch
        # reference). Either way we cannot reconstruct what the proposer
        # intended — discard and re-propose fresh (RUNTIME.md §4.2 last
        # row: "partial proposal → discard, proposer is non-deterministic").
        # Classify (file present but unreadable = garbled write; absent =
        # a never-finished partial proposal) BEFORE discarding, since the
        # discard removes the directory the check looks at.
        cls = (
            "discard_garbled"
            if _experiment_file_present(workspace_root, epoch_id, latest)
            else "discard_partial_proposal"
        )
        log.warning(
            "resume: generation %s has no readable experiment marker (%s); "
            "discarding the partial generation and re-running the round fresh",
            latest,
            exc,
        )
        discard_interrupted_generation(workspace_root, epoch_id, latest, store=store)
        return ResumePlan(discarded_generation_id=latest, classification=cls)

    if experiment.outcome is not None:
        # The latest generation already has a committed outcome — it is a
        # finished round rather than an interruption. (This can happen if the
        # crash landed AFTER the outcome was written but the loop was
        # going to start a new round.) Nothing to resume; the next round
        # advances past it exactly as a fresh start would.
        return ResumePlan(classification="clean")

    # The experiment is readable and un-outcomed: an interrupted round.
    # Decide resume-in-place vs discard from the snapshot + loss markers.
    snapshot_present = store.has_generation(epoch_id, latest)
    if not snapshot_present:
        # Proposed-but-not-applied (or applier crashed mid-derive). No
        # snapshot means no board unit can have run; discard and re-run
        # the round fresh rather than re-deriving from a possibly-partial
        # state (conservative).
        log.warning(
            "resume: generation %s was proposed but has no applied snapshot; "
            "discarding and re-running the round fresh",
            latest,
        )
        discard_interrupted_generation(
            workspace_root,
            epoch_id,
            latest,
            store=store,
            round_index=experiment.round_index,
        )
        return ResumePlan(discarded_generation_id=latest, classification="discard_unapplied")

    if not _has_any_loss(workspace_root, epoch_id, latest):
        # Applied-but-not-running: the snapshot exists but no board unit
        # finished, so there is no cached work to save. Discarding and
        # re-running is byte-identical to "start the tournament from
        # scratch" and keeps the path simple (the orchestrator never has
        # to special-case an experiment with zero cached units).
        log.warning(
            "resume: generation %s was applied but no board unit completed; "
            "discarding and re-running the round fresh",
            latest,
        )
        discard_interrupted_generation(
            workspace_root,
            epoch_id,
            latest,
            store=store,
            round_index=experiment.round_index,
        )
        return ResumePlan(discarded_generation_id=latest, classification="discard_no_progress")

    if not _matches_single_pending_lineage(workspace_root, epoch_id, latest, experiment):
        log.warning(
            "resume: generation %s has cached board units but its experiment does not "
            "match one pending lineage node; discarding the unverifiable cache",
            latest,
        )
        discard_interrupted_generation(workspace_root, epoch_id, latest, store=store)
        return ResumePlan(discarded_generation_id=latest, classification="discard_garbled")

    # The single safe-and-free resume case: a readable, un-outcomed
    # experiment whose snapshot is applied and that has at least one
    # completed board unit on disk. Resume in place — reuse the persisted
    # patches, re-derive the snapshot (idempotent), and let the unit cache
    # HIT every loss.json already on disk.
    log.warning(
        "resume: generation %s was interrupted mid-tournament with completed "
        "board unit(s) on disk; resuming in place (re-running only the entries "
        "that have no loss.json yet)",
        latest,
    )
    return ResumePlan(
        resume_generation_id=latest,
        resume_experiment=experiment,
        classification="resume_tournament",
    )


def _matches_single_pending_lineage(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    experiment: Experiment,
) -> bool:
    """Whether a cached experiment names its one exact in-flight lineage row."""
    parent_id = experiment.parent_generation_id
    round_index = experiment.round_index
    if (
        experiment.epoch_id != epoch_id
        or experiment.generation_id != generation_id
        or not isinstance(parent_id, str)
        or not parent_id
        or not isinstance(round_index, int)
        or isinstance(round_index, bool)
        or round_index < 0
    ):
        return False

    return _pending_lineage_groups(workspace_root, epoch_id).get((round_index, parent_id)) == (
        generation_id,
    )


def _experiment_file_present(workspace_root: Path, epoch_id: str, generation_id: str) -> bool:
    """True iff ``experiment.json`` exists (even if unreadable).

    Distinguishes a garbled-but-present experiment marker (read raised
    despite the file existing — a torn write or dangling patch ref) from
    a never-written one, for the discard classification only.
    """
    from zicato.core.workspace import experiment_json_path  # noqa: PLC0415

    return experiment_json_path(workspace_root, epoch_id, generation_id).is_file()


__all__ = [
    "ResumePlan",
    "clear_runtime_state",
    "discard_interrupted_generation",
    "prepare_resume",
]
