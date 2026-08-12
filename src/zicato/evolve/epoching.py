"""Contract-hash auto-epoching split out of :mod:`zicato.orchestrator`.

The "evaluation contract" is the board + proposer brief + scoring + the
registered inner-harness identity (entrypoint + mutable trees). A change
to any of those means generations on either side of the change are no
longer comparable, so the epoch must roll. This module owns the
roll-at-evolve-time decision and its supporting helpers:

* :func:`ensure_epoch_for_contract` — the evolve entry hook that resolves
  the epoch a round runs against, auto-rolling a fresh epoch on drift;
* :func:`_create_epoch_from_contract` — create an epoch from resolved
  contract inputs;
* :func:`_promoted_head_snapshot` — locate an epoch's promoted-head
  snapshot dir (the cross-epoch lineage seed source);
* the per-component sub-hash bookkeeping
  (:func:`_stored_component_hashes`, :func:`_write_component_hashes`,
  :func:`_component_diff_label`) and the v0-seed marker path
  (:func:`_roll_seed_marker`).

Every name here is re-exported from :mod:`zicato.orchestrator` so no
caller import changes. This is a pure move; the behaviour is identical.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from zicato.util import best_effort
from zicato.workspace import WorkspaceLayout

log = logging.getLogger("zicato.orchestrator")

CallLLM = Callable[[str, str, str], Awaitable[str]]


#: Internal sentinel: workspace-level state file recording, for each
#: epoch, where its v0 baseline should be seeded from when the epoch is
#: a contract-roll of a predecessor. Keyed by epoch id; value is the
#: absolute path to the previous epoch's promoted-head snapshot. The
#: file is written by :func:`ensure_epoch_for_contract` and consumed by
#: :func:`_ensure_baseline_snapshot`.
def _roll_seed_marker(workspace_root: Path, epoch_id: str) -> Path:
    return WorkspaceLayout.from_root(workspace_root).roll_seed_marker(epoch_id)


def _component_diff_label(prev_components: dict[str, str], cur_components: dict[str, str]) -> str:
    """Return a human-readable label naming which contract components moved.

    Compares the per-component sub-hashes; returns a comma-joined list
    of the component names that differ (``board``, ``brief``,
    ``scoring``, ``entrypoint``, ``mutable_trees``). Falls back to a
    generic ``"contract"`` when no per-component breakdown is available
    (e.g. a legacy epoch with no stored components).
    """
    if not prev_components:
        return "contract"
    changed = [
        name for name, cur_hash in cur_components.items() if prev_components.get(name) != cur_hash
    ]
    return ", ".join(changed) if changed else "contract"


def _stored_component_hashes(workspace_root: Path, epoch_id: str) -> dict[str, str]:
    """Return the per-component sub-hashes recorded for an epoch.

    The breakdown is written next to ``config.json`` as
    ``contract_components.json`` at epoch creation / roll time. Absent
    for legacy epochs (returns an empty dict — the caller falls back to
    a generic message).
    """
    path = WorkspaceLayout.from_root(workspace_root).contract_components(epoch_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _write_component_hashes(
    workspace_root: Path, epoch_id: str, components: dict[str, str]
) -> None:
    """Persist an epoch's per-component contract sub-hashes."""
    path = WorkspaceLayout.from_root(workspace_root).contract_components(epoch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(components, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def ensure_epoch_for_contract(
    workspace_root: Path,
    *,
    auto_epoch: bool,
    aux_call_llm: CallLLM,
    epoch_name: str | None = None,
) -> str:
    """Resolve the epoch ``evolve`` should run against, auto-rolling on drift.

    The "evaluation contract" is the board + proposer brief + scoring +
    the registered inner-harness identity (entrypoint + mutable trees).
    A change to any of those means generations on either side are no
    longer comparable, so the epoch must roll. This function is the
    roll-at-evolve-time hook: it is called before the orchestrator
    resolves an epoch.

    Logic:

    1. Compute the current contract hash via
       :func:`zicato.epoch.contract.compute_contract_hash`.
    2. ``cur = current_epoch_id(workspace_root)``.
    3. If ``cur`` is ``None``:

       * ``auto_epoch`` True  — create epoch ``e0`` from the contract,
         return it.
       * ``auto_epoch`` False — raise (tell the operator to
         ``zicato epoch new``).
    4. Load ``cur``'s :class:`EpochConfig`.

       * If ``cur.contract_hash is None`` (legacy epoch) OR ``== `` the
         current hash: return ``cur`` (continue, no roll).
       * Else (the contract changed):

         * ``auto_epoch`` True  — close ``cur`` (generating
           ``analysis.md``), create a NEW epoch carrying the new
           contract, baselined from ``cur``'s promoted head, auto-named
           ``e{N+1}``; echo a clear message; return the new id.
         * ``auto_epoch`` False — raise a clear error: the contract
           drifted from the current epoch; revert the files or run
           ``zicato epoch new``.

    ``epoch_name`` overrides the default ``e{N}`` auto-name for any
    epoch this function creates (the first epoch on a fresh workspace,
    or the new epoch after a roll). When ``None``, the ``e{N}`` scheme
    is used.

    Returns the epoch id ``evolve`` should use.
    """
    from zicato.epoch.contract import (  # noqa: PLC0415
        compute_component_hashes,
        compute_contract_hash,
        resolve_contract_inputs,
    )
    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        current_epoch_id,
        list_epochs,
        load_epoch,
    )

    inputs = resolve_contract_inputs(workspace_root)
    current_hash = compute_contract_hash(inputs)
    current_components = compute_component_hashes(inputs)

    cur = current_epoch_id(workspace_root)
    if cur is None:
        if not auto_epoch:
            raise FileNotFoundError(
                f"no current_epoch marker under {workspace_root}; "
                "run `zicato epoch new <name> ...` or drop --no-auto-epoch "
                "so `zicato evolve` can create the first epoch"
            )
        new_id = _create_epoch_from_contract(
            workspace_root,
            inputs=inputs,
            name=epoch_name or "e0",
            aux_call_llm=aux_call_llm,
        )
        _write_component_hashes(workspace_root, new_id, current_components)
        return new_id

    cfg = load_epoch(workspace_root, cur)
    if cfg.contract_hash is None or cfg.contract_hash == current_hash:
        # Legacy epoch (``None`` stored hash → treated as always-matching)
        # or the contract is unchanged. Either way: no roll. The check is
        # ``is None``, NOT ``== ""``: a corrupted/empty real hash must roll
        # rather than silently read as legacy.
        return cur

    # The contract drifted from the current epoch.
    if not auto_epoch:
        drifted = _component_diff_label(
            _stored_component_hashes(workspace_root, cur), current_components
        )
        raise RuntimeError(
            f"evaluation contract has drifted from the current epoch "
            f"{cur!r} (changed: {drifted}); either revert the contract "
            "files or run `zicato epoch new` to start a new epoch. "
            "(Remove --no-auto-epoch to let `zicato evolve` roll the "
            "epoch automatically.)"
        )

    # Auto-roll: close the drifted epoch, open a fresh one carrying the
    # new contract, baselined from the closed epoch's promoted head.
    # close_epoch_async is awaited (we are already inside an event loop;
    # the sync close_epoch would nest asyncio.run and raise).
    from zicato.epoch.lifecycle import close_epoch_async  # noqa: PLC0415

    await close_epoch_async(workspace_root, cur, aux_call_llm=aux_call_llm)

    # Mid-run the publication is refreshed deterministically each round (no
    # LLM), carrying a LIVING DRAFT stamp and preserving whatever prose was
    # last authored. The epoch is now closed, so run the FULL render — the
    # bounded auxiliary-LLM prose pass over the final data — which produces
    # the finished paper and drops the LIVING DRAFT stamp (the masthead now
    # reads "closed"). When no auxiliary callable is available, fall back to
    # the cheap deterministic masthead re-stamp so the persisted files still
    # reflect the closed state. Best-effort — never blocks the epoch roll.
    with best_effort(
        "post-close report render",
        on_error=lambda exc: log.debug("post-close report render skipped: %s", exc),
    ):
        if aux_call_llm is not None:
            from zicato.analyzer import generate_epoch_report  # noqa: PLC0415

            await generate_epoch_report(workspace_root, cur, aux_call_llm)
        else:
            from zicato.analyzer import restamp_persisted_report  # noqa: PLC0415

            restamp_persisted_report(workspace_root, cur)

    next_n = len(list_epochs(workspace_root))
    new_id = _create_epoch_from_contract(
        workspace_root,
        inputs=inputs,
        name=epoch_name or f"e{next_n}",
        aux_call_llm=aux_call_llm,
    )
    _write_component_hashes(workspace_root, new_id, current_components)

    # Record where the new epoch's v0 should be seeded from: the
    # promoted head of the epoch we just closed. `_ensure_baseline_snapshot`
    # reads this marker on the first evolve round of the new epoch.
    prev_head_snapshot = _promoted_head_snapshot(workspace_root, cur)
    if prev_head_snapshot is not None:
        _roll_seed_marker(workspace_root, new_id).write_text(
            str(prev_head_snapshot) + "\n", encoding="utf-8"
        )

    changed = _component_diff_label(
        _stored_component_hashes(workspace_root, cur), current_components
    )
    log.info("contract changed (%s) — rolled %s -> %s", changed, cur, new_id)
    print(f"contract changed ({changed}) — rolled {cur} -> {new_id}")

    # The boundary is when applying a proposer recommendation is FREE: the
    # epoch is rolling anyway, so an edit to the proposer costs nothing extra
    # in comparability. Print the pending queue here (and only here — the
    # no-roll path above returns before this) so the operator meets it at the
    # one moment the decision is cheap. Silent when nothing is pending.
    from zicato.proposer.reflection import echo_pending_recommendations  # noqa: PLC0415

    echo_pending_recommendations(workspace_root)

    # Drain any promote/reject overrides left pending across the roll. They
    # target a bare generation id (e.g. "v3"); generation ids restart at v0
    # in the new epoch, so a survivor would mis-fire on the new epoch's
    # same-named generation. The roll opens a fresh, incomparable contract,
    # so a pending override can only have targeted the epoch just closed.
    with best_effort(
        "drain stale gate overrides on epoch roll",
        on_error=lambda exc: log.debug("stale-override drain skipped: %s", exc),
    ):
        from zicato.runtime.control_consumer import (  # noqa: PLC0415
            drain_stale_gate_overrides,
        )

        drained = drain_stale_gate_overrides(
            workspace_root, reason=f"superseded by epoch roll {cur} -> {new_id}"
        )
        if drained:
            log.info(
                "drained %d stale gate override(s) on epoch roll %s -> %s: %s",
                len(drained),
                cur,
                new_id,
                ", ".join(sorted(drained)),
            )
    # The auto-roll path has no operator interaction surface, so the
    # epoch's ``goal`` field lands empty. Nudge the operator to fill it
    # in later via the dedicated subcommand.
    log.warning(
        "epoch %s opened by auto-roll with no goal recorded; "
        'run `zicato epoch set-goal --epoch %s --goal "..."` to fill it in.',
        new_id,
        new_id,
    )
    print(
        f"NOTE: epoch {new_id} opened by auto-roll with no goal recorded; "
        f'run `zicato epoch set-goal --epoch {new_id} --goal "..."` to fill it in.'
    )
    return new_id


def _create_epoch_from_contract(
    workspace_root: Path,
    *,
    inputs: Any,
    name: str,
    aux_call_llm: CallLLM,
) -> str:
    """Create an epoch from resolved contract inputs; return its id.

    A thin wrapper over :func:`zicato.epoch.lifecycle.new_epoch` that
    loads the scoring weights from the live ``scoring.json`` and carries
    the registered inner-harness identity into the contract hash.
    """
    from zicato.epoch.lifecycle import new_epoch  # noqa: PLC0415
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    if inputs.scoring_path.exists():
        weights = scoring_weights_from_dict(
            json.loads(inputs.scoring_path.read_text(encoding="utf-8"))
        )
    else:
        from zicato.core.types import ScoringWeights  # noqa: PLC0415

        weights = ScoringWeights()

    cfg = new_epoch(
        workspace_root=workspace_root,
        name=name,
        board_source=inputs.board_path,
        brief_source=inputs.brief_path,
        weights=weights,
        auto_close_previous=False,  # ensure_epoch_for_contract closes explicitly
        aux_call_llm=aux_call_llm,
        entrypoint=inputs.entrypoint,
        mutable_trees=tuple(inputs.mutable_trees),
        proposer_path=inputs.proposer_path,
    )
    return cfg.id


def _promoted_head_snapshot(workspace_root: Path, epoch_id: str) -> Path | None:
    """Return the snapshot dir of an epoch's last promoted generation.

    Reads the epoch's ``current_generation`` marker (the promoted head)
    and returns that generation's ``snapshot/`` directory. Returns
    ``None`` when the epoch has no promoted generation beyond a seed
    that was never run, or when the snapshot directory is absent — the
    caller then falls back to seeding from the registered mutable trees.
    """
    # The generation-coordinate resolvers still live in the orchestrator;
    # imported lazily here to keep the move pure and avoid an import cycle.
    from zicato.orchestrator import (  # noqa: PLC0415
        _resolve_current_generation,
        _snapshot_root,
    )

    try:
        head = _resolve_current_generation(workspace_root, epoch_id)
    except FileNotFoundError:
        return None
    snap = _snapshot_root(workspace_root, epoch_id, head)
    if not snap.exists() or not any(snap.iterdir()):
        return None
    return snap
