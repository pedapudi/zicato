"""Round-pipeline **ingest** stage — the live SQLite analytical index IO.

This is the round pipeline's *ingest* seam: the best-effort dual-write that keeps
``index.db`` reflecting each generation's ``experiment.json`` as the loop
runs, plus the paired index *reads* the propose stage threads back into the
proposer (prior-experiment memory + mutation track records).

The index is a pure projection (Part II design principle 1): every write and
read here is best-effort — a missing :mod:`zicato.index` sibling or an
unreadable database is logged at ``debug`` and swallowed. ``experiment.json``
on disk stays canonical and ``zicato repair index`` can always rebuild the index
from scratch, so a hiccup in this stage never aborts a round.

Callers import this owner directly; the dispatcher does not re-export private
index helpers. The module logger is named ``zicato.orchestrator``, so a log
record names the orchestrator wherever the index write lives.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zicato.core.types import PriorExperiment
from zicato.runtime.lock import WorkspaceLock

log = logging.getLogger("zicato.orchestrator")


# ---------------------------------------------------------------------------
# Live SQLite analytical index — best-effort dual-write
# ---------------------------------------------------------------------------


#: Location of the SQLite analytical index, relative to the workspace
#: root (the ``.zicato/`` directory). The :mod:`zicato.index` sibling
#: owns the schema; the orchestrator only knows the path so it can keep
#: the index live as the loop runs.
_INDEX_DB_RELPATH = "index.db"


def _index_db_path(workspace_root: Path) -> Path:
    """Return the SQLite analytical index path for a workspace."""
    return workspace_root / _INDEX_DB_RELPATH


def index_preflight(workspace_root: Path, *, writer: WorkspaceLock | None = None) -> str:
    """Build an absent/stale index, heal a diverged one; report what happened.

    Invocation startup, settled-memory reads, and final cleanup share this
    repair owner and forward the writer they already hold. Dirty epoch revisions
    require a complete projection even when record counts remain unchanged.
    A fresh build already projects every epoch, so it needs no subsequent heal.

    Callers isolate repair failures because canonical execution must continue
    without the derived index. A database with a newer schema is retained and
    produces a warning; this executable cannot safely interpret or repair it.
    """
    from zicato.evolve.settlement_recovery import (  # noqa: PLC0415
        acknowledge_repaired_settlement_indexes,
    )
    from zicato.index.ingest import ensure_index, heal_index  # noqa: PLC0415
    from zicato.index.schema import IndexSchemaNewerError  # noqa: PLC0415

    actions: list[str] = []
    try:
        ensure_index(workspace_root, action_out=actions, writer=writer)
    except IndexSchemaNewerError as exc:
        log.warning(
            "index: %s — this run reads a stale index (no build, no heal). "
            "Recover with: delete the workspace index.db and run `zicato repair index`, "
            "or run this workspace with the newer zicato that wrote it.",
            exc,
        )
        return "index: SKIPPED — index.db was written by a newer zicato"
    built = actions[0] if actions else "present"
    if built.startswith("built:"):
        acknowledge_repaired_settlement_indexes(workspace_root)
        return f"index: built fresh ({built.split(':', 1)[1]})"
    healed = heal_index(workspace_root, writer=writer)
    if healed:
        return "index: healed epochs " + ", ".join(healed)
    return "index: fresh"


def _load_prior_experiments(
    workspace_root: Path,
    epoch_id: str,
    *,
    cross_epoch: bool = False,
    writer: WorkspaceLock | None = None,
) -> list[PriorExperiment]:
    """Best-effort read of the epoch's settled experiment-memory digest.

    Refresh dirty epochs under the invocation's writer before reading. Candidate
    construction subsequently reads mutation track records from that same
    settled projection; it does not launch workers before this boundary.

    The orchestrator threads the result into
    the proposal episode's evidence so the proposer
    sees the ``## What's already been tried`` section. Mirrors
    :func:`_ingest_experiment_into_index`: the :mod:`zicato.index` sibling
    may be absent and a missing / stale index must never abort a round, so
    any failure — a missing module, an unreadable database — is logged at
    ``debug`` level and yields ``[]``. ``experiment.json`` on disk stays
    canonical; an empty digest simply omits the prompt section.

    ``cross_epoch`` is the contract's opt-in
    ``experiment_memory.cross_epoch`` knob (EXPERIMENT-MEMORY.md §3.4):
    when set, settled experiments from prior epochs under the SAME
    contract hash fill the cap-budget the same-epoch entries leave, as
    ``same_contract=False`` entries.
    """
    try:
        from zicato.index.query import prior_experiments_for_epoch  # noqa: PLC0415

        index_preflight(workspace_root, writer=writer)
        return prior_experiments_for_epoch(
            _index_db_path(workspace_root), epoch_id, cross_epoch=cross_epoch
        )
    except ImportError:
        log.debug("zicato.index.query unavailable; proposer runs without experiment memory")
        return []
    except Exception as exc:  # noqa: BLE001 — experiment-memory read is best-effort
        log.debug(
            "prior_experiments_for_epoch skipped for %s: %s",
            epoch_id,
            exc,
        )
        return []


def _load_mutation_track_records(
    workspace_root: Path,
    epoch_id: str,
) -> dict[str, Any]:
    """Best-effort read of the epoch's mutation-point fertility map.

    The orchestrator threads the result onto the
    :class:`~zicato.proposer.agent.ProposerContext` so the prompt renderer
    can annotate each manifest entry with its compact, banded track-record
    line ("experiments touching this point" — advisory, never causal).
    Mirrors :func:`_load_prior_experiments`: the index read is best-effort,
    so a missing :mod:`zicato.index` sibling, a never-built database, or any
    read failure is logged at ``debug`` level and yields ``{}``, which
    renders the manifest with no track-record annotations.
    """
    try:
        from zicato.index.query import mutation_point_track_record  # noqa: PLC0415

        return dict(mutation_point_track_record(_index_db_path(workspace_root), epoch_id))
    except ImportError:
        log.debug("zicato.index.query unavailable; manifest renders without track records")
        return {}
    except Exception as exc:  # noqa: BLE001 — track-record read is best-effort
        log.debug("mutation_point_track_record skipped for %s: %s", epoch_id, exc)
        return {}


def _ingest_experiment_into_index(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> None:
    """Best-effort dual-write of one generation's experiment into the index.

    Called after ``experiment.json`` is written or its outcome updated,
    so the live SQLite analytical index reflects the experiment as the
    loop runs. The :mod:`zicato.index` sibling may not be installed (it
    lands in parallel); the import is lazy and any failure — a missing
    module, a schema mismatch, an I/O error — is logged at ``debug``
    level and swallowed. ``experiment.json`` on disk stays canonical and
    ``zicato repair index`` can always rebuild the index from scratch.
    """
    try:
        from zicato.index.ingest import ingest_experiment  # noqa: PLC0415

        ingest_experiment(
            workspace_root,
            _index_db_path(workspace_root),
            epoch_id,
            generation_id,
        )
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug(
            "live index ingest_experiment skipped for %s/%s: %s",
            epoch_id,
            generation_id,
            exc,
        )
