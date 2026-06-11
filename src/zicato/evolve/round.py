"""Shared Round-pipeline helpers split out of :mod:`zicato.orchestrator`.

The orchestrator runs TWO evolve pipelines — the gauntlet single-duel path
(:func:`zicato.orchestrator.evolve_once`) and the multi-challenger field
(:func:`zicato.orchestrator._evolve_multi_challenger`). Their *tails*
diverge deeply (one challenger vs an N-wide strategy-driven field, a single
``run_tournament`` vs ``resolve_tournament`` with live structure publishing,
one persisted experiment vs N with crowning invariants, an operator-gate
override present only on the gauntlet, …), so they are NOT a single pipeline
and are deliberately left distinct.

What IS provably identical between them is the **propose-time patch
plumbing** that each used to inline as its own closure:

* :func:`build_post_apply_validator` — the ``validate_experiment`` hook the
  proposer agent calls on every attempt. It beats the ``applying`` phase,
  derives the child snapshot all-or-nothing from the candidate's patches
  (surfacing a ``derive_generation`` ``ValueError`` as a retryable finding),
  records the derived tree in the caller's ``last_child_snapshot`` slot, and
  runs :func:`zicato.mutation.validator.validate_post_apply`.
* :func:`check_patch_manifest_and_forbidden` — the post-propose manifest
  cross-check: every patch's ``mutation_id`` must resolve against the
  re-enumerated mutation manifest, and no patch may touch a
  ``forbidden_ids`` mutation. Raises :class:`RuntimeError` on either
  violation.

Both helpers are exact extractions of the previously-triplicated closures;
the gauntlet path and the field path now call the SAME code. Behaviour is
identical — the validator factory reproduces the closure's beat / derive /
validate sequence verbatim, and the manifest check raises the same two
``RuntimeError`` messages.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.evolve.lifecycle_services import _beat

if TYPE_CHECKING:
    from zicato.core.types import Experiment
    from zicato.runtime.heartbeat import HeartbeatBeater


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
        try:
            child = genstore.derive_generation(
                epoch_id=epoch_id,
                parent_generation_id=parent_id,
                child_generation_id=next_id,
                patches=list(candidate.patches),
            )
        except ValueError as exc:
            return [f"derive_generation rejected the patch set: {exc}"]
        last_child_snapshot["path"] = child
        return validate_post_apply(child, list(candidate.patches), mutations)

    return _validate


def check_patch_manifest_and_forbidden(
    experiment: Experiment,
    mutations: list[Any],
    forbidden_ids: Any,
) -> None:
    """Cross-check a proposer experiment's patches against the manifest.

    Two invariants both pipelines enforced inline with byte-identical code:

    * every patch's ``mutation_id`` must resolve against the re-enumerated
      mutation manifest (a stale id is a hard error — the proposer targeted
      a mutation point that no longer exists);
    * no patch may touch a ``forbidden_ids`` mutation (the operator's
      explicit no-go list).

    Raises :class:`RuntimeError` with the same message either pipeline raised
    on the first violation; returns ``None`` when the patch set is clean.
    """
    from zicato.mutation.validator import check_forbidden_ids  # noqa: PLC0415

    mutations_by_id = {m.id: m for m in mutations}
    for patch in experiment.patches:
        if patch.mutation_id not in mutations_by_id:
            raise RuntimeError(
                f"proposer-emitted patch {patch.id!r} targets unknown "
                f"mutation_id {patch.mutation_id!r}"
            )
    forbidden_violations = check_forbidden_ids(list(experiment.patches), list(forbidden_ids))
    if forbidden_violations:
        raise RuntimeError(
            "proposer-emitted patches violate forbidden_ids: " + "; ".join(forbidden_violations)
        )
