"""Conservative crash-resume protocol for ``zicato evolve``.

When ``evolve`` is killed mid-tournament (operator SIGTERM, host reboot,
OOM) the durable artifacts under ``epochs/{epoch}/`` survive — promoted
generations, the journal, ``experiment.json``, and any per-board
``loss.json`` a completed run wrote (see RUNTIME.md §4.1). The live
``runtime/`` state (heartbeat, active runs, active tournament) does
not; it is rebuilt from scratch on restart.

This module reads the durable resume *markers* (RUNTIME.md §4.2,
ROBUSTNESS.md §2.6) and decides, for the most recent **un-outcomed**
generation, whether the prior ``evolve`` can safely pick up where it
left off or whether the partial work must be discarded and re-run from
the last clean checkpoint.

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
persisted experiment is absent, unreadable, already outcomed, or its
snapshot cannot be reconciled with the patches, this module DISCARDS
the generation directory so the next round re-proposes cleanly into a
fresh ``vN`` — never reusing a unit cache it cannot vouch for.

Lineage / journal safety
------------------------
A generation is appended to ``lineage.json`` and journaled only at the
very end of one evolve round, *after* its outcome is decided. An
un-outcomed generation therefore has NO lineage or journal entry, so
both discarding it and resuming it leave those append-only records
untouched. This is what makes the protocol corruption-free by
construction.

Scope
-----
This first cut covers the **gauntlet** path (one champion, one
challenger, one full-board duel — the default and back-compat anchor).
A multi-challenger field (swiss / elim / racing) under interruption is
treated conservatively: its in-flight challengers are discarded and the
round re-runs from scratch. Extending in-place resume through the
non-gauntlet structures is a follow-up.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.runtime.paths import (
    active_runs_dir,
    active_tournament_log_path,
    active_tournament_path,
    heartbeat_path,
)
from zicato.workspace import WorkspaceLayout

if TYPE_CHECKING:
    from zicato.core.types import Experiment

log = logging.getLogger("zicato.runtime.resume")


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """The outcome of reconciling an interrupted workspace.

    Fields
    ------
    resume_generation_id:
        When non-``None``, the orchestrator should resume this
        generation *in place* — reuse its persisted ``experiment.json``
        + patches (do NOT re-propose), re-derive its snapshot from those
        patches (idempotent), and run the tournament, which cache-HITs
        every board unit that already has a ``loss.json``. ``None`` means
        there is nothing to resume in place: either the workspace was
        clean, or the partial work was discarded and the next round runs
        fresh and byte-identical to today.
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
        ``"discard_no_progress"``, ``"discard_complete_unreadable"``, or
        ``"discard_garbled"``.
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
    gens_root = _generations_root(workspace_root, epoch_id)
    if not gens_root.is_dir():
        return None
    best: tuple[int, str] | None = None
    for child in gens_root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("v") and name[1:].isdigit():
            n = int(name[1:])
            if best is None or n > best[0]:
                best = (n, name)
    return best[1] if best is not None else None


def clear_runtime_state(workspace_root: Path) -> None:
    """Discard the live ``runtime/`` state of a prior, dead evolve.

    Removes ``heartbeat.json``, the active-tournament event log AND its
    legacy ``active_tournament.json`` snapshot, and every
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


def _discard_generation(workspace_root: Path, epoch_id: str, generation_id: str) -> None:
    """Delete one interrupted generation's directory, all-or-nothing.

    The directory holds nothing append-only — a generation is journaled
    and added to ``lineage.json`` only once its outcome is decided, which
    by definition has not happened for an un-outcomed generation. So
    removing it cannot corrupt the journal or the lineage; it only frees
    the ``vN`` id for a clean re-propose by the next round.
    """
    gen_dir = _generations_root(workspace_root, epoch_id) / generation_id
    if gen_dir.is_dir():
        shutil.rmtree(gen_dir, ignore_errors=True)


def _has_any_loss(workspace_root: Path, epoch_id: str, generation_id: str) -> bool:
    """True iff at least one board entry has a ``loss.json`` for this gen.

    The presence of even one per-entry ``loss.json`` is the marker that
    the tournament had started and a completed unit is cacheable — the
    signal that makes resume-in-place worth doing instead of a clean
    re-run from scratch.
    """
    runs_dir = _generations_root(workspace_root, epoch_id) / generation_id / "runs"
    if not runs_dir.is_dir():
        return False
    for entry_dir in runs_dir.iterdir():
        if entry_dir.is_dir() and (entry_dir / "loss.json").is_file():
            return True
    return False


def prepare_resume(workspace_root: Path, epoch_id: str) -> ResumePlan:
    """Reconcile an interrupted workspace into a resumable / clean state.

    Called once at ``evolve`` start, after the workspace lock is held and
    before the round loop. Two phases:

    1. **Clear stale runtime state.** The live ``runtime/`` files of a
       dead evolve are removed (:func:`clear_runtime_state`); the unit
       cache under ``epochs/`` is the only thing the resume relies on.

    2. **Classify the latest generation.** If the highest ``vN`` has a
       committed ``outcome`` (or there is no generation at all, or the
       seed ``v0``), the workspace is clean and the next round runs
       byte-identically to today. Otherwise ``vN`` is an interrupted
       generation and the conservative inference table (RUNTIME.md §4.2)
       decides resume-in-place vs discard-and-rerun.

    The inference, conservatively (discard on ANY ambiguity):

    ===============================================  ====================
    On-disk state of the un-outcomed latest gen      Action
    ===============================================  ====================
    experiment readable + snapshot/ + >=1 loss.json  resume in place
    experiment readable + snapshot/ + 0 loss.json    discard (re-run)
    experiment readable + no snapshot/               discard (re-run)
    experiment present but unreadable / outcome set  discard (garbled)
    no experiment.json                               discard (partial)
    ===============================================  ====================

    Only the first row resumes in place — the one case where the persisted
    patches are known-good and the on-disk ``loss.json`` units are a sound
    cache HIT. Every other row discards the directory so the next round
    re-proposes a fresh ``vN``.

    Returns a :class:`ResumePlan`. A clean workspace yields the default
    (no resume, nothing discarded) so behavior is byte-identical to a
    cold start.
    """
    clear_runtime_state(workspace_root)

    latest = _latest_generation_id(workspace_root, epoch_id)
    if latest is None or latest == "v0":
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
        _discard_generation(workspace_root, epoch_id, latest)
        return ResumePlan(discarded_generation_id=latest, classification=cls)

    if experiment.outcome is not None:
        # The latest generation already has a committed outcome — it is a
        # finished round, not an interruption. (This can happen if the
        # crash landed AFTER the outcome was written but the loop was
        # going to start a new round.) Nothing to resume; the next round
        # advances past it exactly as a fresh start would.
        return ResumePlan(classification="clean")

    # The experiment is readable and un-outcomed: an interrupted round.
    # Decide resume-in-place vs discard from the snapshot + loss markers.
    snapshot_present = (_generations_root(workspace_root, epoch_id) / latest / "snapshot").is_dir()
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
        _discard_generation(workspace_root, epoch_id, latest)
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
        _discard_generation(workspace_root, epoch_id, latest)
        return ResumePlan(discarded_generation_id=latest, classification="discard_no_progress")

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
    "prepare_resume",
]
