"""Snapshot GC / retention — prune generation SOURCE TREES, never records.

A long epoch accumulates one materialised source tree per generation:
``generations/{id}/snapshot/`` directories under the directory backend, tagged
commits + materialised ``repo-worktrees/`` checkouts under the git backend. The
trees of dead branches (rejected challengers) are pure disk cost once their
tournament settled — every analytical consumer (journal, dashboard, reindex)
reads the RECORDS rather than the trees. This module reclaims that cost.

What pruning means, per backend
-------------------------------
* **Directory backend** — remove the generation's ``snapshot/``
  directory. Everything else under ``generations/{id}/`` (the
  ``experiment.json`` record, ``gen_score.json``, per-run ``runs/``
  telemetry) is kept: the generation stays fully analysable, it just no
  longer has a source tree.
* **Git backend** — delete the generation's tag and its materialised
  worktree, then ``git worktree prune`` + ``git gc --auto``. A rejected
  generation's commit is reachable ONLY through its tag (the epoch
  branch was reset back to the promoted parent before the next derive),
  so dropping the tag makes the commit unreachable and collectable.
  A PROMOTED generation's commit is an ancestor of the epoch branch and
  would stay reachable regardless — which is consistent, because
  promoted generations are never pruned (below). Space held by
  unreachable objects is reclaimed as git's reflog entries expire and
  ``gc`` repacks; ``--auto`` keeps that maintenance incremental rather
  than blocking the operator.

What pruning NEVER touches
--------------------------
``lineage.json``, the epoch journal, ``experiment.json`` /
``gen_score.json`` records, and run telemetry are never modified — GC
removes source TREES only. That covers the measurement archives beside
them (``gen_score.history.jsonl``, ``loss.archive.jsonl``,
``events.prev.jsonl``; issue #122) with no special case: pruning
deletes ONE directory per generation — its ``snapshot/`` — and never
enumerates or removes files under ``generations/{id}/`` individually,
so keeping the records while dropping the snapshots is a structural
property of the prune rather than a filename list that has to be kept in
sync. The dashboard's tree/diff views degrade to
an explicit "no source tree" response for a pruned generation (they
already tolerate a missing tree), and the patch/mutation views keep
rendering through ``StorageBackend`` from the surviving experiment and
per-patch records.

Retention policy
----------------
Exactly one of two policies selects the prune set; BOTH share a safety
floor that is never pruned:

* generations whose lineage decision is ``promoted`` (the champion
  chain — the epoch's actual history),
* generations still IN FLIGHT (lineage ``promoted`` is ``null``),
* generations with no lineage record at all (unknown ⇒ conservative),
* the epoch's seed ``v0``.

Policies:

* ``keep_last_n=N`` — additionally keep the N newest generations by
  numeric ``v{N}`` order (a settled rejected challenger you may still
  want to eyeball), pruning older rejected trees.
* ``keep_promoted_only=True`` — keep only the safety floor, pruning
  every settled rejected generation's tree.

Operator surface: ``zicato epoch gc`` (dry-run by default, ``--apply``
to execute), plus an opt-in epoch-close hook (the ``storage_gc``
workspace-config block, default off) that prunes an epoch as it closes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.epoch.genstore import default_generation_store
from zicato.workspace import natural_key

log = logging.getLogger(__name__)

#: Workspace ``config.json`` key holding the snapshot-GC options block.
#: Shape: ``{"on_epoch_close": bool, "keep_last_n": int}`` or
#: ``{"on_epoch_close": bool, "keep_promoted_only": true}``. Absent (the
#: default) means the epoch-close hook is OFF; ``zicato epoch gc`` is
#: always available regardless.
STORAGE_GC_KEY = "storage_gc"

#: Numeric ``v{N}`` generation ids sort by N; anything else sorts after,
#: lexicographically (conservative: a non-conventional id reads as
#: "newest", so ``keep_last_n`` retains it).


@dataclass(frozen=True, slots=True)
class PruneReport:
    """The outcome (or dry-run plan) of one :func:`prune_generations` call.

    ``pruned`` lists the generation ids whose source trees were removed
    — or, under ``dry_run``, WOULD be removed. ``bytes_reclaimed`` is a
    best-effort measure of the on-disk tree bytes freed (directory
    snapshot / materialised worktree); for the git backend it excludes
    object-store space, which git reclaims asynchronously as reflogs
    expire and ``gc`` repacks.
    """

    epoch_id: str
    backend: str
    policy: str
    examined: tuple[str, ...]
    kept: tuple[str, ...]
    pruned: tuple[str, ...]
    dry_run: bool
    bytes_reclaimed: int


def _lineage_decisions(workspace_root: Path, epoch_id: str) -> dict[str, bool | None]:
    """Map generation id → lineage decision for one epoch.

    ``True`` = promoted, ``False`` = rejected, ``None`` = still in
    flight. A generation absent from the map has no lineage record at
    all and is treated conservatively (kept) by the caller.
    """
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    decisions: dict[str, bool | None] = {}
    for epoch in load_lineage(workspace_root).get("epochs", []):
        if epoch.get("id") != epoch_id:
            continue
        for gen in epoch.get("generations", []):
            gen_id = gen.get("id")
            if isinstance(gen_id, str):
                promoted = gen.get("promoted")
                decisions[gen_id] = promoted if isinstance(promoted, bool) else None
    return decisions


def prune_generations(
    workspace_root: Path,
    epoch_id: str,
    *,
    keep_last_n: int | None = None,
    keep_promoted_only: bool = False,
    dry_run: bool = True,
) -> PruneReport:
    """Prune settled-rejected generation source trees under one epoch.

    Exactly one policy must be selected — ``keep_last_n`` (keep the N
    newest generations that still have sources, in addition to the safety
    floor) or ``keep_promoted_only`` (keep only the floor). The store
    enumerates source-bearing generations only, so an already-pruned
    generation is not one of the N. The safety floor —
    promoted generations, in-flight generations, generations with no
    lineage record, and the seed ``v0`` — is NEVER pruned under either
    policy; see the module docstring for the reasoning.

    ``dry_run=True`` (the default) computes and returns the plan without
    touching disk. Records (``lineage.json``, journal, experiment / score
    files, run telemetry) are never modified either way.

    Raises :class:`ValueError` on a policy mis-specification.
    """
    if keep_promoted_only and keep_last_n is not None:
        raise ValueError("prune_generations: pass keep_last_n OR keep_promoted_only, not both")
    if not keep_promoted_only and keep_last_n is None:
        raise ValueError("prune_generations: one of keep_last_n / keep_promoted_only is required")
    if keep_last_n is not None and keep_last_n < 1:
        raise ValueError(f"prune_generations: keep_last_n must be >= 1, got {keep_last_n}")

    store = default_generation_store(workspace_root)
    backend = store.backend_name
    policy = "keep_promoted_only" if keep_promoted_only else f"keep_last_n={keep_last_n}"

    generations = sorted(store.list_generations(epoch_id), key=natural_key)
    decisions = _lineage_decisions(workspace_root, epoch_id)

    keep: set[str] = set()
    for gen_id in generations:
        decision = decisions.get(gen_id, "unknown")
        if decision is False:
            continue  # settled-rejected: the only prune-eligible class
        # Promoted (True), in-flight (None), or no lineage record — keep.
        keep.add(gen_id)
    if generations:
        keep.add("v0")
    if keep_last_n is not None:
        keep.update(generations[-keep_last_n:])

    # A candidate must actually HAVE a source tree. The store enumerates
    # source-bearing generations, so this normally holds for every listed
    # id; it is re-asserted because the listing and the removal are two
    # reads of a directory an operator or a concurrent GC can change
    # between them, and a report naming a generation it did not remove
    # would overstate what the run reclaimed.
    pruned = tuple(g for g in generations if g not in keep and store.has_generation(epoch_id, g))
    kept = tuple(g for g in generations if g in keep)

    bytes_reclaimed = store.prune_generations(epoch_id, pruned, dry_run=dry_run)

    report = PruneReport(
        epoch_id=epoch_id,
        backend=backend,
        policy=policy,
        examined=tuple(generations),
        kept=kept,
        pruned=pruned,
        dry_run=dry_run,
        bytes_reclaimed=bytes_reclaimed,
    )
    log.info(
        "epoch gc %s: %s — %d examined, %d kept, %d pruned (%d bytes)%s",
        epoch_id,
        policy,
        len(report.examined),
        len(report.kept),
        len(report.pruned),
        report.bytes_reclaimed,
        " [dry run]" if dry_run else "",
    )
    return report


def _read_storage_gc_config(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract a well-formed ``storage_gc`` block, or ``None`` when off.

    Returns the block only when it is a dict with ``on_epoch_close``
    truthy AND a usable policy (``keep_last_n`` >= 1 or
    ``keep_promoted_only``). Anything else — absent block, malformed
    values, hook disabled — yields ``None`` so the close path stays
    byte-identical to a workspace that never heard of GC.
    """
    if not isinstance(config, dict):
        return None
    block = config.get(STORAGE_GC_KEY)
    if not isinstance(block, dict) or not block.get("on_epoch_close"):
        return None
    keep_promoted_only = bool(block.get("keep_promoted_only", False))
    keep_last_n = block.get("keep_last_n")
    if keep_promoted_only:
        return {"keep_promoted_only": True}
    if isinstance(keep_last_n, int) and not isinstance(keep_last_n, bool) and keep_last_n >= 1:
        return {"keep_last_n": keep_last_n}
    return None


def maybe_prune_on_epoch_close(workspace_root: Path, epoch_id: str) -> PruneReport | None:
    """Opt-in epoch-close hook: prune the closing epoch when configured.

    Reads the workspace ``config.json`` ``storage_gc`` block; absent or
    disabled (the default) is a no-op returning ``None``. Best-effort by
    design — closing an epoch must never fail because GC hiccuped, so
    every error is logged and swallowed.
    """
    try:
        from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

        try:
            config = read_workspace_config(workspace_root)
        except (OSError, ValueError):
            return None
        policy = _read_storage_gc_config(config.raw)
        if policy is None:
            return None
        return prune_generations(workspace_root, epoch_id, dry_run=False, **policy)
    except Exception as exc:  # noqa: BLE001 — the close path must never fail on GC
        log.warning("epoch gc on close skipped for %s: %s", epoch_id, exc)
        return None


__all__ = [
    "PruneReport",
    "STORAGE_GC_KEY",
    "maybe_prune_on_epoch_close",
    "prune_generations",
]
