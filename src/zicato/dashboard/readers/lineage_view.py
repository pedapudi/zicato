"""lineage_view — extracted from zicato.dashboard.state_reader (pure move)."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from zicato.dashboard.readers.paths import (
    WorkspacePaths,
    _epoch_created_at,
    _iso,
    _natural_key,
    _read_json_value,
)

# ---------------------------------------------------------------------------
# Lineage view (directory-derived)
# ---------------------------------------------------------------------------

_PROMOTED_DECISIONS = frozenset({"promoted", "promote", "accepted", "accept", "win", "won"})


def _experiment_decision(exp: dict[str, Any]) -> str | None:
    outcome = exp.get("outcome")
    if outcome is None:
        return None
    if isinstance(outcome, str):
        return outcome
    if isinstance(outcome, dict):
        for key in ("decision", "tournament_decision", "verdict"):
            val = outcome.get(key)
            if isinstance(val, str):
                return val
    return None


def build_lineage_view(paths: WorkspacePaths) -> dict[str, Any]:
    """Every generation directory in every epoch, in-flight or resolved.

    Walks ``epochs/{id}/generations/*`` and emits one node per directory
    with ``{generation_id, epoch_id, parent_generation_id, promoted,
    created_at}`` — ``promoted`` is ``None`` while a generation is still
    being scored. Identical shape to the Rust ``build_lineage_view``.
    """
    legacy: dict[tuple[str, str], dict[str, Any]] = {}
    lineage_file = _read_json_value(paths.lineage)
    if isinstance(lineage_file, dict):
        for ep in lineage_file.get("epochs", []) or []:
            if not isinstance(ep, dict):
                continue
            epoch_id = str(ep.get("id", ""))
            for gen in ep.get("generations", []) or []:
                if not isinstance(gen, dict):
                    continue
                gid = gen.get("id")
                if not isinstance(gid, str):
                    continue
                legacy[(epoch_id, gid)] = {
                    "parent_id": gen.get("parent_id"),
                    "created_at": gen.get("created_at") or None,
                    "promoted": gen.get("promoted"),
                }

    generations: list[dict[str, Any]] = []
    epoch_created: dict[str, str] = {}
    if paths.epochs.is_dir():
        for epoch_dir in sorted(paths.epochs.iterdir(), key=lambda p: _natural_key(p.name)):
            if not epoch_dir.is_dir():
                continue
            epoch_id = epoch_dir.name
            epoch_created[epoch_id] = _epoch_created_at(epoch_dir)
            gens_dir = epoch_dir / "generations"
            if not gens_dir.is_dir():
                continue
            for gen_dir in sorted(gens_dir.iterdir(), key=lambda p: _natural_key(p.name)):
                if not gen_dir.is_dir():
                    continue
                generation_id = gen_dir.name
                meta = legacy.get((epoch_id, generation_id), {})
                experiment = _read_json_value(gen_dir / "experiment.json")
                experiment = experiment if isinstance(experiment, dict) else None

                parent = None
                if experiment is not None:
                    parent = experiment.get("parent_generation_id")
                if not isinstance(parent, str):
                    parent = meta.get("parent_id")

                promoted: bool | None = None
                if experiment is not None:
                    decision = _experiment_decision(experiment)
                    if decision is not None:
                        promoted = decision.strip().lower() in _PROMOTED_DECISIONS
                if promoted is None:
                    legacy_promoted = meta.get("promoted")
                    if isinstance(legacy_promoted, bool):
                        promoted = legacy_promoted

                # The evolve-round that MINTED this generation (its birth round
                # within the epoch's outer loop). A separate stamp writes this to
                # experiment.json; until it lands the field is simply absent and
                # the dashboard derives the rounds from the field-tournament
                # records / lineage instead. Read it tolerantly (int only).
                round_index: int | None = None
                if experiment is not None:
                    raw_round = experiment.get("round_index")
                    if isinstance(raw_round, bool):
                        raw_round = None
                    if isinstance(raw_round, int):
                        round_index = raw_round
                    elif isinstance(raw_round, str) and raw_round.strip().lstrip("-").isdigit():
                        round_index = int(raw_round.strip())

                created_at: str | None = None
                if experiment is not None:
                    for key in ("proposed_at", "created_at"):
                        val = experiment.get(key)
                        if isinstance(val, str) and val:
                            created_at = val
                            break
                if created_at is None:
                    legacy_created = meta.get("created_at")
                    if isinstance(legacy_created, str) and legacy_created:
                        created_at = legacy_created
                if created_at is None:
                    try:
                        ctime = gen_dir.stat().st_ctime
                        created_at = _iso(_dt.datetime.fromtimestamp(ctime, _dt.UTC))
                    except OSError:
                        created_at = None

                node: dict[str, Any] = {
                    "generation_id": generation_id,
                    "epoch_id": epoch_id,
                    "parent_generation_id": parent if isinstance(parent, str) else None,
                    "promoted": promoted,
                    "created_at": created_at,
                }
                # Only surface round_index when the stamp is present, so a
                # pre-feature payload stays byte-identical (the key is absent,
                # not null) and the dashboard's lineage fallback kicks in.
                if round_index is not None:
                    node["round_index"] = round_index
                generations.append(node)

    # Sort by the RECORDED creation timestamp first (epoch ``config.json``
    # created_at, then the generation's proposed_at/created_at), with the
    # numeric-aware id as a deterministic tiebreaker / fallback. So the ledger
    # is chronological even when ids diverge from creation order (date-named
    # epochs, carried champions), and never lexical (v1, v10, v11, v2).
    generations.sort(
        key=lambda g: (
            epoch_created.get(g["epoch_id"], ""),
            _natural_key(g["epoch_id"]),
            g.get("created_at") or "",
            _natural_key(g["generation_id"]),
        )
    )
    return {"generations": generations}
