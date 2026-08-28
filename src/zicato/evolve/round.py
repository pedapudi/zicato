"""Scratch validation and patch checks for shared candidate production.

Every tournament structure produces candidates through one batch builder.
This module supplies the proposal-time patch plumbing used by each batch
slot:

* :func:`build_post_apply_validator` — the ``validate_experiment`` hook the
  proposer agent calls on every attempt. It beats the ``applying`` phase,
  derives the child snapshot all-or-nothing from the candidate's patches
  (surfacing a ``derive_generation`` ``ValueError`` as a retryable finding),
  records the derived tree in the caller's ``last_child_snapshot`` slot, and
  runs :func:`zicato.mutation.validator.validate_post_apply`.
* :func:`check_patch_manifest_and_forbidden` — the post-propose manifest
  cross-check: every patch's ``mutation_id`` must resolve against the
  re-enumerated mutation manifest, and no patch may touch a
  ``forbidden_ids`` mutation. Raises :class:`BadPatchSetError` — a
  ``ValueError`` — on either violation (issue #83).

Concurrency note (WS-CONC): under best-of-N slate parallelism the per-slot
``validate`` hook from :func:`build_scratch_validator_factory` calls
``genstore.derive_scratch`` SYNCHRONOUSLY on the event loop — there is no
``await`` between the derive's start and finish — so two slots' derives never
overlap in time; only the LLM propose calls actually yield and run
concurrently. Even a hypothetical overlap would be safe, because each slot
derives into its own disjoint ``ztw-slate-*`` scratch path. The git backend's
one shared touch is the first ``snapshot_root`` that materialises the parent
worktree; that is pre-warmed once here, and its cold-store materialisation is
made idempotent under the worktree-admin lock
(:meth:`zicato.epoch.git_genstore.GitGenerationStore._materialise_worktree`),
so a future threaded caller that raced the pre-warm would still be safe.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.evolve.lifecycle_services import _beat

if TYPE_CHECKING:
    from zicato.core.types import Experiment
    from zicato.runtime.heartbeat import HeartbeatBeater

#: One scratch-validator lease: the ``validate_experiment`` hook the wrapper
#: threads onto ONE slate slot, plus its idempotent cleanup. The wrapper
#: allocates one lease per slot from
#: :func:`build_scratch_validator_factory`, runs the slot through the hook,
#: and calls the cleanup in a ``finally`` (including on propose failure /
#: degrade) so the slot's scratch tree never outlives the slot.
ScratchValidatorLease = tuple[
    "Callable[[Experiment], Awaitable[list[str]]]",
    Callable[[], None],
]
#: The zero-arg factory threaded on ``ProposerContext.scratch_validator_factory``.
#: Each call mints a FRESH, disjoint scratch tree + validator lease.
ScratchValidatorFactory = Callable[[], ScratchValidatorLease]

#: Prefix for a best-of-N slate slot's ephemeral scratch derivation parent.
#: Placed in the OS temp dir so it never sits under the workspace tree —
#: nothing under it can be mistaken for a canonical generation snapshot.
#:
#: Lifecycle — a slate parent is NOT crash-reaped like a run's ``ztw-snap-*``
#: checkout. The supervisor's reaper (``reap.rs``) only removes paths RECORDED
#: as a run's ``snapshot_path``; a slate parent is a per-slot dir private to
#: the orchestrator process and is never recorded, so the reaper cannot see
#: it. Its cleanup is instead: (1) the slot's ``try/finally`` removes it on
#: every normal exit (success, propose failure, or degrade); (2) a SIGKILL
#: that skips the ``finally`` leaks it, and those leaks are collected by the
#: OS temp-dir cleaner and by the best-effort startup sweep
#: :func:`_sweep_stale_slate_scratch` runs from
#: :func:`build_scratch_validator_factory`.
SLATE_SCRATCH_PREFIX = "ztw-slate-"

#: Age past which the startup sweep treats a stray ``ztw-slate-*`` parent as a
#: crash leak. Comfortably longer than any live slate slot (which lives for one
#: propose attempt), so an in-flight sibling orchestrator's dirs are never
#: touched.
_SLATE_SCRATCH_STALE_SECONDS = 24 * 60 * 60


def _sweep_stale_slate_scratch() -> None:
    """Best-effort removal of crash-leaked ``ztw-slate-*`` parents in the temp root.

    A SIGKILL between :func:`tempfile.mkdtemp` and the slot's ``finally`` leaks
    a ``ztw-slate-*`` parent — the ``try/finally`` cannot run, and the
    supervisor's reaper never sees it (it reaps only RECORDED run snapshots).
    This cheap, bounded sweep — glob the OS temp root for our prefix and remove
    the dirs older than :data:`_SLATE_SCRATCH_STALE_SECONDS` — is run once when
    a round builds its scratch-validator factory. It NEVER raises: any error
    (permission, a dir vanishing mid-sweep, a racing sibling) is swallowed so a
    housekeeping hiccup can never fail a round.
    """
    import time  # noqa: PLC0415

    try:
        temp_root = Path(tempfile.gettempdir())
        cutoff = time.time() - _SLATE_SCRATCH_STALE_SECONDS
        for entry in temp_root.glob(f"{SLATE_SCRATCH_PREFIX}*"):
            try:
                if not entry.is_dir():
                    continue
                if entry.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        return


def build_post_apply_validator(
    *,
    genstore: Any,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    mutations: list[Any],
    beater: HeartbeatBeater | None,
    round_index: int,
    last_child_snapshot: dict[str, Path],
) -> Callable[[Experiment], Awaitable[list[str]]]:
    """Build the proposer's ``validate_experiment`` post-apply hook.

    Returns an ``async (candidate: Experiment) -> list[str]`` callable the
    proposer agent invokes once per attempt. The hook:

    1. Beats the ``applying:round_{round_index}:{next_id}`` phase (a no-op
       when ``beater is None``).
    2. ``derive_generation`` copies the parent tree and applies the
       candidate's patch set all-or-nothing into the child snapshot,
       clearing any stale child tree from a prior attempt so a retry
       re-derives cleanly (see ``docs/design/STORAGE.md`` §4-§5).
       ``apply_patches`` runs its own post-apply syntax gate and raises
       ``ValueError`` when a patch left a touched ``.py`` file unparseable;
       that is surfaced as a single retryable post-apply finding rather
       than crashing the evolve loop (issue #11).
    3. Records the derived child tree path in ``last_child_snapshot["path"]``
       — the caller reuses it as the validated tree the tournament mounts,
       so no second apply is needed.
    4. Runs :func:`zicato.mutation.validator.validate_post_apply` and returns
       its findings (empty list ⇒ the patch set validated).

    The previously-inlined closures in the gauntlet and field paths were
    byte-identical bar their local name; this is their single source.
    """
    from zicato.mutation.validator import validate_post_apply  # noqa: PLC0415

    async def _validate(candidate: Experiment) -> list[str]:
        _beat(
            beater,
            epoch_id=epoch_id,
            generation_id=next_id,
            round_index=round_index,
            phase=f"applying:round_{round_index}:{next_id}",
        )
        # derive_generation is the generation-level transaction boundary:
        # it copies the parent tree, applies the patch set all-or-nothing,
        # and clears any stale child tree from a prior attempt — so a
        # retry re-derives cleanly. See docs/design/STORAGE.md §4-§5.
        # apply_patches now runs its own post-apply syntax gate and raises
        # ValueError when a patch left a touched ``.py`` file unparseable;
        # surface that as a retryable post-apply finding rather than letting
        # it crash the evolve loop (issue #11).
        from zicato.telemetry.meta_loop import SPAN_PHASE, meta_span  # noqa: PLC0415

        try:
            # The apply phase span brackets the patch-set derive (HARMONOGRAF.md §7).
            async with meta_span("apply", kind=SPAN_PHASE, meta={"generation_id": next_id}):
                child = genstore.derive_generation(
                    epoch_id=epoch_id,
                    parent_generation_id=parent_id,
                    child_generation_id=next_id,
                    patches=list(candidate.patches),
                )
        except (ValueError, KeyError) as exc:
            # ``ValueError`` is the checked applier's single bad-patch-set
            # signal. ``KeyError`` is caught as defence in depth: the
            # unchecked applier path still raises it for a missing anchor,
            # and a bad patch set must reject ONE candidate rather than
            # abort the whole evolve run (issue #83).
            return [f"derive_generation rejected the patch set: {exc}"]
        last_child_snapshot["path"] = child
        return validate_post_apply(child, list(candidate.patches), mutations)

    return _validate


def build_scratch_validator_factory(
    *,
    genstore: Any,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    mutations: list[Any],
    beater: HeartbeatBeater | None,
    round_index: int,
) -> ScratchValidatorFactory:
    """Build the per-slot scratch ``validate_experiment`` factory (WS-CONC).

    The concurrency-enabling sibling of :func:`build_post_apply_validator`.
    Where that shared hook derives every attempt into the ONE shared
    ``next_id`` tree — the write that serialises the slate — this factory
    hands each slate slot its OWN throwaway scratch tree, so N slots can
    validate concurrently against fully disjoint directories.

    Each ``factory()`` call:

    * allocates a fresh :func:`tempfile.mkdtemp` parent (``ztw-slate-*`` in
      the OS temp dir, OUTSIDE the workspace so nothing under it can be
      mistaken for a canonical snapshot) and a ``child`` scratch path under
      it;
    * returns a ``(validate, cleanup)`` lease. ``validate`` beats the
      ``applying`` phase, calls ``genstore.derive_scratch`` (which applies
      the candidate's patches into the scratch tree WITHOUT touching the
      generation namespace — see
      :meth:`zicato.epoch.genstore.GenerationStore.derive_scratch` — so no
      walker can ever enumerate it), and runs
      :func:`zicato.mutation.validator.validate_post_apply`. A retry within
      the slot re-derives into the SAME scratch tree (idempotent
      clear-and-reapply). ``cleanup`` idempotently removes the whole
      ``ztw-slate-*`` parent.

    The parent generation's source tree is pre-warmed ONCE here (the git
    backend materialises the parent worktree under its admin lock on first
    ``snapshot_root``), so the concurrent ``derive_scratch`` calls find it
    already present and only ever READ it — the derives race on nothing.

    The chosen candidate is still mounted into the real ``next_id`` exactly
    once, AFTER selection, through the shared
    :func:`build_post_apply_validator` hook (the wrapper's unconditional
    final derive) — this factory never writes the canonical tree.
    """
    from zicato.mutation.validator import validate_post_apply  # noqa: PLC0415

    # Sweep any crash-leaked ``ztw-slate-*`` parents left by a prior SIGKILL
    # before this round allocates its own (best-effort, never raises).
    _sweep_stale_slate_scratch()

    # Pre-warm the parent source tree once so concurrent slot derives find it
    # materialised and only read it (git: first snapshot_root checks out the
    # parent worktree under the process worktree-admin lock).
    genstore.snapshot_root(epoch_id, parent_id)

    def _factory() -> ScratchValidatorLease:
        from zicato.epoch.genstore import discard_ephemeral_parent  # noqa: PLC0415

        parent = Path(tempfile.mkdtemp(prefix=SLATE_SCRATCH_PREFIX))
        scratch_root = parent / "child"

        async def _validate(candidate: Experiment) -> list[str]:
            _beat(
                beater,
                epoch_id=epoch_id,
                generation_id=next_id,
                round_index=round_index,
                phase=f"applying:round_{round_index}:{next_id}",
            )
            from zicato.telemetry.meta_loop import SPAN_PHASE, meta_span  # noqa: PLC0415

            try:
                # Apply phase span for the per-slot scratch derive (HARMONOGRAF.md §7).
                async with meta_span("apply", kind=SPAN_PHASE, meta={"generation_id": next_id}):
                    child = genstore.derive_scratch(
                        epoch_id=epoch_id,
                        parent_generation_id=parent_id,
                        patches=list(candidate.patches),
                        scratch_root=scratch_root,
                    )
            except (ValueError, KeyError) as exc:
                # Same unified bad-patch-set boundary as
                # :func:`build_post_apply_validator` — see the note there
                # on why ``KeyError`` is caught too (issue #83).
                return [f"derive_generation rejected the patch set: {exc}"]
            return validate_post_apply(child, list(candidate.patches), mutations)

        def _cleanup() -> None:
            discard_ephemeral_parent(parent)

        return _validate, _cleanup

    return _factory


class BadPatchSetError(ValueError):
    """A patch set the manifest cross-check refuses.

    A ``ValueError`` SUBCLASS, deliberately: issue #83's unification is that
    "this patch set cannot be applied" reaches every boundary as one type,
    and a subclass satisfies every ``except ValueError`` in the apply path
    unchanged. The distinct class exists only so the ``evolve`` CLI can name
    this condition among the operator-actionable errors it renders as a
    clean message instead of a traceback — which is what folding
    :func:`check_patch_manifest_and_forbidden`'s ``RuntimeError`` into
    ``ValueError`` otherwise silently took away (``cli/commands/evolve.py``
    catches ``FileNotFoundError`` and ``RuntimeError`` around the whole
    loop; nothing between it and this raise catches either type).
    """


def check_patch_manifest_and_forbidden(
    experiment: Experiment,
    mutations: list[Any],
    forbidden_ids: Any,
) -> None:
    """Cross-check a proposer experiment's patches against the manifest.

    Two invariants apply to every candidate slot:

    * every patch's ``mutation_id`` must resolve against the re-enumerated
      mutation manifest (a stale id is a hard error — the proposer targeted
      a mutation point that no longer exists);
    * no patch may touch a ``forbidden_ids`` mutation (the operator's
      explicit no-go list).

    Raises :class:`BadPatchSetError` (a ``ValueError``) with the same
    message for the first violation; returns ``None``
    when the patch set is clean.

    The exception TYPE is load-bearing (issue #83): "this patch set cannot
    be applied" is ONE logical condition, and it now reaches callers as
    exactly one type across the whole apply path — this cross-check, the
    :func:`~zicato.mutation.applier.apply_patches` pre-check, its
    apply-time missing-anchor sites and its post-apply syntax gate all
    raise ``ValueError``. This function previously raised ``RuntimeError``,
    a third type for the same class, which no bad-patch-set boundary could
    catch without also swallowing unrelated runtime faults. The severity is
    unchanged: neither call site wraps this in a try, so a stale/forbidden
    id is still a hard error that stops the round — by the time it runs the
    proposer already validated its own patch set, so a violation here means
    the manifest changed under the round.

    The ``ValueError`` SUBCLASS is what keeps the severity claim honest at
    the process boundary too. ``cli/commands/evolve.py`` renders
    ``FileNotFoundError`` / ``RuntimeError`` out of the loop as a clean
    ``Error: …`` line, and this condition used to qualify; a bare
    ``ValueError`` would have started dumping a traceback for an
    operator-actionable fault instead. See :class:`BadPatchSetError`.
    """
    from zicato.mutation.validator import check_forbidden_ids  # noqa: PLC0415

    mutations_by_id = {m.id: m for m in mutations}
    for patch in experiment.patches:
        if patch.mutation_id not in mutations_by_id:
            raise BadPatchSetError(
                f"proposer-emitted patch {patch.id!r} targets unknown "
                f"mutation_id {patch.mutation_id!r}"
            )
    forbidden_violations = check_forbidden_ids(list(experiment.patches), list(forbidden_ids))
    if forbidden_violations:
        raise BadPatchSetError(
            "proposer-emitted patches violate forbidden_ids: " + "; ".join(forbidden_violations)
        )
