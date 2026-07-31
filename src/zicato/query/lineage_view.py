"""lineage_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from zicato.query.decisions import (
    experiment_decision,
    promoted_tristate,
)
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _natural_key,
    _read_json_value,
    layout_of,
)
from zicato.query.ratings import RATING_FIELDS, rating_by_generation
from zicato.workspace import iter_epochs

# ---------------------------------------------------------------------------
# Lineage view (directory-derived)
# ---------------------------------------------------------------------------
#
# The decision classifier lives in ``readers.decisions`` — the ONE module the
# lineage view, the epoch experiments feed, and every other reader share, so
# the payloads cannot disagree about what counts as a promotion.


def build_lineage_view(
    paths: WorkspacePaths,
    epoch_id: str | None = None,
    *,
    include_ratings: bool = True,
) -> dict[str, Any]:
    """Every generation directory in every epoch, in-flight or resolved.

    Walks ``epochs/{id}/generations/*`` and emits one node per directory
    with ``{generation_id, epoch_id, parent_generation_id, promoted,
    created_at}`` — ``promoted`` is ``None`` while a generation is still
    being scored. Identical shape to the Rust ``build_lineage_view``.

    A node additionally carries ``rejection_reason`` and the duel's
    ``parent_scalar`` / ``child_scalar`` / ``delta_scalar`` when
    ``lineage.json`` recorded them (issue #124) — present-only, so a
    workspace written before the fields existed keeps its prior payload.
    This is API passthrough; no UI renders them yet.

    ``epoch_id`` scopes the feed to ONE epoch's generations (the
    epoch-scoped generations feed the views consume); ``None`` keeps the
    workspace-global walk. An unknown id yields an empty list — the same
    honest degrade as an epoch with no generations.

    Each node also carries the visibility rating triple ``elo`` /
    ``elo_se`` / ``elo_games`` (DQ2 snake_case), joined server-side from
    the analytical index (never re-derived by the client — DQ1). The join
    is best-effort (DQ3): an absent / cold index — or a generation the
    fold has not rated (zero settled duels, or a pre-reindex file) — reads
    as the null triple, never an error. The rating is visibility-only; it
    never gates promotion.
    """
    legacy: dict[tuple[str, str], dict[str, Any]] = {}
    lineage_file = _read_json_value(paths.lineage)
    if isinstance(lineage_file, dict):
        for ep in lineage_file.get("epochs", []) or []:
            if not isinstance(ep, dict):
                continue
            legacy_eid = str(ep.get("id", ""))
            for gen in ep.get("generations", []) or []:
                if not isinstance(gen, dict):
                    continue
                gid = gen.get("id")
                if not isinstance(gid, str):
                    continue
                legacy[(legacy_eid, gid)] = {
                    "parent_id": gen.get("parent_id"),
                    "created_at": gen.get("created_at") or None,
                    "promoted": gen.get("promoted"),
                    # The settle-time facts the DAG now records (issue
                    # #124) — passed through verbatim below.
                    "rejection_reason": gen.get("rejection_reason"),
                    "parent_scalar": gen.get("parent_scalar"),
                    "child_scalar": gen.get("child_scalar"),
                    "delta_scalar": gen.get("delta_scalar"),
                }

    generations: list[dict[str, Any]] = []
    epoch_created: dict[str, str] = {}
    # Enumerate epochs through the single ordering authority (canonical
    # timestamp-first). The node list is re-sorted by its own key below, so
    # this enumeration order is not load-bearing for the OUTPUT — but routing
    # it through ``iter_epochs`` keeps the workspace walk in one place and
    # reuses the cached ``created_at`` each typed ``Epoch`` already carries.
    for epoch in iter_epochs(layout_of(paths)):
        if epoch_id is not None and epoch.id != epoch_id:
            continue
        eid = epoch.id
        epoch_created[eid] = epoch.created_at
        gens_dir = epoch.directory / "generations"
        if gens_dir.is_dir():
            for gen_dir in sorted(gens_dir.iterdir(), key=lambda p: _natural_key(p.name)):
                if not gen_dir.is_dir():
                    continue
                generation_id = gen_dir.name
                meta = legacy.get((eid, generation_id), {})
                experiment = _read_json_value(gen_dir / "experiment.json")
                experiment = experiment if isinstance(experiment, dict) else None

                parent = None
                if experiment is not None:
                    parent = experiment.get("parent_generation_id")
                if not isinstance(parent, str):
                    parent = meta.get("parent_id")

                promoted: bool | None = None
                if experiment is not None:
                    promoted = promoted_tristate(experiment_decision(experiment))
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
                    "epoch_id": eid,
                    "parent_generation_id": parent if isinstance(parent, str) else None,
                    "promoted": promoted,
                    "created_at": created_at,
                }
                # Only surface round_index when the stamp is present, so a
                # pre-feature payload stays byte-identical (the key is absent,
                # not null) and the dashboard's lineage fallback kicks in.
                if round_index is not None:
                    node["round_index"] = round_index
                # The gate's own account of the decision (issue #124):
                # why the generation was cut, and the two scalars it was
                # cut on. Surfaced only when lineage recorded them, on the
                # same absent-not-null discipline as round_index — a node
                # written before the field existed keeps its prior payload,
                # and the reason is empty on anything but a settled
                # rejection (append_to_lineage enforces that at the write).
                reason = meta.get("rejection_reason")
                if isinstance(reason, str) and reason:
                    node["rejection_reason"] = reason
                for field in ("parent_scalar", "child_scalar", "delta_scalar"):
                    value = meta.get(field)
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        node[field] = float(value)
                generations.append(node)

    # The visibility rating triple, joined server-side from the index
    # (best-effort — the null triple when the index is absent/cold, DQ3).
    # ``include_ratings=False`` skips the index open entirely for internal
    # consumers that discard the triple (rounds/gate/judge composition —
    # they read lineage topology, never ratings); the endpoint path and the
    # gens view keep the default join.
    if include_ratings:
        ratings = rating_by_generation(paths, epoch_id)
        for node in generations:
            triple = ratings.get((node["epoch_id"], node["generation_id"]))
            for field in RATING_FIELDS:
                node[field] = triple.get(field) if triple else None

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
