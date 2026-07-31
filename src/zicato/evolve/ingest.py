"""Round-pipeline **ingest** stage — the live SQLite analytical index IO.

Split out of :mod:`zicato.orchestrator` as part of the Finding-2 typed
round-pipeline decomposition (``docs/design/REIMPLEMENTATION.md``). This is
the pipeline's *ingest* seam: the best-effort dual-write that keeps
``index.db`` reflecting each generation's ``experiment.json`` as the loop
runs, plus the paired index *reads* the propose stage threads back into the
proposer (prior-experiment memory + mutation track records).

The index is a pure projection (Part II design principle 1): every write and
read here is best-effort — a missing :mod:`zicato.index` sibling or an
unreadable database is logged at ``debug`` and swallowed. ``experiment.json``
on disk stays canonical and ``zicato reindex`` can always rebuild the index
from scratch, so a hiccup in this stage never aborts a round.

Every name here is re-exported from :mod:`zicato.orchestrator` so no caller
(``from zicato.orchestrator import _load_prior_experiments`` in the propose
CLI, the dashboard projection's ``_index_db_path`` back-edge, …) breaks. The
module logger is named ``zicato.orchestrator`` so the emitted ``debug``
records are byte-identical to the pre-split ones.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from zicato.core.types import PriorExperiment
from zicato.core.workspace import generation_dir

log = logging.getLogger("zicato.orchestrator")


def _cache_gen_score(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    aggregate: dict[str, Any],
    *,
    round_index: int | None = None,
) -> None:
    """Persist the generation aggregate so fast-mode can read it later.

    ``gen_score.json`` is keyed by ``(epoch, generation)`` with NO round
    dimension, and a champion defends across many rounds: under
    ``--mode full`` the champion is re-measured every round and each
    measurement overwrote the one before it, and within a single field
    round each matchup's side-write overwrote its predecessor (last
    matchup wins). The canonical flat file still holds the LATEST
    aggregate — every reader is untouched — and the outgoing measurement
    is retained beside it in ``gen_score.history.jsonl``: one JSON line
    per write, the FULL payload (``per_entry`` included) stamped with the
    caller's ``round_index`` and a monotonic ``seq``.

    ``round_index`` is the evolve round this measurement was taken in;
    ``None`` when the caller has no round in scope (the history line then
    records ``null``, never a fabricated 0).

    Size: the history grows by one aggregate per write — that is per
    (round × side-write), a handful of KiB per round for a board of
    ordinary size. It is an append-only record, never read on the hot
    path, and is not pruned.
    """
    gdir = generation_dir(workspace_root, epoch_id, generation_id)
    gdir.mkdir(parents=True, exist_ok=True)
    payload = dict(aggregate)
    payload.setdefault("generation_id", generation_id)
    _append_gen_score_history(gdir, payload, round_index=round_index)
    (gdir / "gen_score.json").write_text(
        json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


#: Append-only archive of every generation aggregate ever written for a
#: generation, beside the canonical ``gen_score.json`` it shadows.
GEN_SCORE_HISTORY_FILENAME = "gen_score.history.jsonl"


def _append_gen_score_history(
    generation_dir_path: Path,
    payload: dict[str, Any],
    *,
    round_index: int | None,
) -> None:
    """Append one measurement to a generation's ``gen_score.history.jsonl``.

    Best-effort by construction: the history is a record kept ALONGSIDE
    the canonical write, so an unwritable / unreadable archive must never
    cost the caller its ``gen_score.json``. The ``seq`` stamp is the
    count of entries already present, so the file reads back in write
    order even if two writes land in the same clock tick.
    """
    path = generation_dir_path / GEN_SCORE_HISTORY_FILENAME
    seq = 0
    try:
        with open(path, encoding="utf-8") as fh:
            seq = sum(1 for line in fh if line.strip())
    except OSError:
        seq = 0
    record = dict(payload)
    record["seq"] = seq
    record["round_index"] = round_index
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    except OSError as exc:  # pragma: no cover — unwritable workspace
        log.debug("gen_score history append skipped for %s: %s", path, exc)


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


def _load_prior_experiments(
    workspace_root: Path,
    epoch_id: str,
    *,
    cross_epoch: bool = False,
) -> list[PriorExperiment]:
    """Best-effort read of the epoch's settled experiment-memory digest.

    The orchestrator threads the result into
    :func:`zicato.proposer.proposer.propose_experiment` so the proposer
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
    Mirrors :func:`_load_prior_experiments` exactly: the index read is
    best-effort — a missing :mod:`zicato.index` sibling, a never-built
    database, or any read failure is logged at ``debug`` level and yields
    ``{}``, which renders a byte-identical manifest.
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
    ``zicato reindex`` can always rebuild the index from scratch.
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
