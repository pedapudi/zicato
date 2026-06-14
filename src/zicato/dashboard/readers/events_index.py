"""events_index — extracted from zicato.dashboard.state_reader (pure move)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from zicato.dashboard.readers._sqlite import (
    _IndexAbsent,
    _open_index,
)
from zicato.dashboard.readers.epoch_view import (
    _distill_brief_goal,
    _normalize_structure,
    _read_epoch_brief,
    _tournament_block_from_scoring,
)
from zicato.dashboard.readers.gate_view import _mean_drift_loss_per_generation
from zicato.dashboard.readers.lineage_view import (
    _PROMOTED_DECISIONS,
    _experiment_decision,
)
from zicato.dashboard.readers.paths import (
    WorkspacePaths,
    _epoch_sort_key,
    _is_finite,
    _natural_key,
    _read_json_value,
    list_epoch_ids,
    read_current_epoch,
)

# ---------------------------------------------------------------------------
# Run-directory discovery — for the conversation / matchup endpoints
# ---------------------------------------------------------------------------


def _run_id_of_events_file(events_path: Path) -> str | None:
    """Best-effort read of the goldfive ``runId`` from an events file.

    Every event envelope carries the same ``runId`` (camelCase from the
    persistence sink; ``run_id`` from the reducer's proto-reparse path),
    so the first parseable line is sufficient. Returns ``None`` on any
    read / parse failure or when no run id field is present.
    """
    try:
        with open(events_path, encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                evt = json.loads(stripped)
                if not isinstance(evt, dict):
                    continue
                rid = evt.get("runId") or evt.get("run_id")
                return str(rid) if isinstance(rid, str) and rid else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


# Cache: workspace epochs dir → {run_id: events.jsonl path}. The board-run
# layout names run directories by ENTRY id, not run id, so the only way to
# map a run id to its events file is to read the ``runId`` field out of
# each ``events.jsonl``. We do that scan once per workspace and memoize it,
# keyed on the epochs-dir path plus its current mtime so a new generation
# (which bumps the dir mtime) invalidates a stale map.
_RUN_ID_INDEX_CACHE: dict[str, tuple[float, dict[str, Path]]] = {}


def _build_run_id_index(paths: WorkspacePaths) -> dict[str, Path]:
    """Scan ``epochs/*/generations/*/runs/*/events.jsonl`` → ``{run_id: path}``.

    Matches on the ``runId`` carried inside each events file rather than on
    the run-directory name (which is the board ENTRY id, not the run id).
    Results are cached per workspace, invalidated when the epochs dir mtime
    changes.
    """
    epochs = paths.epochs
    cache_key = str(epochs)
    try:
        mtime = epochs.stat().st_mtime
    except OSError:
        return {}

    cached = _RUN_ID_INDEX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    index: dict[str, Path] = {}
    if epochs.is_dir():
        for events_path in epochs.glob("*/generations/*/runs/*/events.jsonl"):
            rid = _run_id_of_events_file(events_path)
            if rid and rid not in index:
                index[rid] = events_path
    _RUN_ID_INDEX_CACHE[cache_key] = (mtime, index)
    return index


# Cache: workspace epochs dir → {run_id: gen×entry events.jsonl path}. In
# successive-halving racing the fixed champion (e.g. v0) is RE-RACED /
# REUSED across rungs, so the same gen×entry yields MULTIPLE per-rung run
# records — but only ONE rung actually executed and emitted its own
# events.jsonl; the rest are score-reuse records carrying a distinct
# ``run_id`` with NO transcript of their own. Each such record is written
# as a ``runs/<entry>/loss.json`` carrying both its ``run_id`` and the
# gen×entry it belongs to (the run directory it lives under). Mapping every
# such ``run_id`` to its gen×entry ``events.jsonl`` lets a transcript-less
# reuse run_id resolve to the one real transcript for that pair. Memoized
# on the epochs-dir mtime, like the run-id index above.
_REUSE_RUN_ID_INDEX_CACHE: dict[str, tuple[float, dict[str, Path]]] = {}


def _build_reuse_run_id_index(paths: WorkspacePaths) -> dict[str, Path]:
    """Scan ``runs/<entry>/loss.json`` → ``{run_id: gen×entry events.jsonl}``.

    Every per-entry ``loss.json`` carries the ``run_id`` of the record it
    settles, and lives in the ``generations/<gen>/runs/<entry>/`` directory
    whose ``events.jsonl`` is the gen×entry's one real transcript. A
    successive-halving reuse record's ``run_id`` differs from the run id
    inside that ``events.jsonl`` (the run that actually executed), so this
    index maps the reuse ``run_id`` onto the real transcript file. Only
    pairs whose ``events.jsonl`` actually exists are indexed, so a resolve
    through this map always lands on a readable transcript. Cached per
    workspace, invalidated when the epochs dir mtime changes.
    """
    epochs = paths.epochs
    cache_key = str(epochs)
    try:
        mtime = epochs.stat().st_mtime
    except OSError:
        return {}

    cached = _REUSE_RUN_ID_INDEX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    index: dict[str, Path] = {}
    if epochs.is_dir():
        for loss_path in epochs.glob("*/generations/*/runs/*/loss.json"):
            events_path = loss_path.parent / "events.jsonl"
            if not events_path.exists():
                continue
            loss = _read_json_value(loss_path)
            if not isinstance(loss, dict):
                continue
            rid = loss.get("run_id")
            if isinstance(rid, str) and rid and rid not in index:
                index[rid] = events_path
    _REUSE_RUN_ID_INDEX_CACHE[cache_key] = (mtime, index)
    return index


def find_run_events_path(paths: WorkspacePaths, run_id: str) -> Path | None:
    """Locate the ``events.jsonl`` for one run id.

    Tries, in order:

    1. The run's ``active_runs/{run_id}.json`` (``events_jsonl_path``).
    2. A directory named ``run_id`` directly under
       ``epochs/*/generations/*/runs/`` carrying an ``events.jsonl``
       (an alternate layout some tooling uses).
    3. The run-id index built by scanning every
       ``epochs/*/generations/*/runs/*/events.jsonl`` and matching on the
       ``runId`` field inside the file. This is the layout the board
       runner actually writes: run directories are named by board ENTRY
       id, not run id, so the run id only appears inside the events.

    Returns ``None`` when nothing matches.
    """
    run_file = paths.active_runs_dir / f"{run_id}.json"
    run = _read_json_value(run_file)
    if isinstance(run, dict):
        events = run.get("events_jsonl_path")
        if isinstance(events, str) and events and Path(events).exists():
            return Path(events)

    if paths.epochs.is_dir():
        for epoch_dir in paths.epochs.iterdir():
            gens = epoch_dir / "generations"
            if not gens.is_dir():
                continue
            for gen_dir in gens.iterdir():
                run_dir = gen_dir / "runs" / run_id
                events = run_dir / "events.jsonl"
                if events.exists():
                    return events

    # Fall back to the run-id → events.jsonl index (matches the canonical
    # board-run layout, where the run directory is named by entry id).
    indexed = _build_run_id_index(paths).get(run_id)
    if indexed is not None and indexed.exists():
        return indexed

    # Final fallback: a successive-halving REUSE run_id — the fixed
    # champion re-raced across rungs emits a per-rung loss.json carrying
    # this run_id but NO events.jsonl of its own; only its gen×entry has
    # the one real transcript. Map the reuse run_id → that gen×entry
    # events.jsonl so the champion side renders rather than reporting
    # "could not be reconstructed".
    reused = _build_reuse_run_id_index(paths).get(run_id)
    if reused is not None and reused.exists():
        return reused
    return None


def find_generation_entry_events(
    paths: WorkspacePaths, generation_id: str, entry_id: str
) -> Path | None:
    """STRICT ``(generation_id, entry_id)`` → events.jsonl resolution.

    Unlike :func:`find_generation_run`, this requires the events file to
    live in the entry's OWN run directory
    (``generations/<gen>/runs/<entry>/events.jsonl``) — no fallback to an
    arbitrary sibling run dir. This is the right primitive for the
    successive-halving champion fallback: a genuinely-absent gen×entry
    must NOT fabricate some other entry's transcript. Returns ``None`` when
    no such file exists.
    """
    if not paths.epochs.is_dir():
        return None
    for epoch_dir in paths.epochs.iterdir():
        events = epoch_dir / "generations" / generation_id / "runs" / entry_id / "events.jsonl"
        if events.exists():
            return events
    return None


def resolve_transcript_events(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    match_id: str | None = None,
) -> Path | None:
    """PRIMARY transcript resolver: ``(epoch, gen, entry)`` → events.jsonl.

    The deterministic triple is the primary key — the events file lives at
    ``epochs/<epoch>/generations/<gen>/runs/<entry>/events.jsonl`` and the
    pane always knows all three coordinates. A ``run_id`` / ``match_id`` is
    only a DISAMBIGUATOR: when a gen×entry has MULTIPLE runs (e.g.
    successive-halving racing re-races a champion across rungs, each rung
    landing in its own sub-directory), the disambiguator selects a specific
    rung's events file. With no disambiguator we DEFAULT to the entry's own
    ``runs/<entry>/events.jsonl`` — the gen×entry's one canonical transcript.

    Resolution, strict to this entry's own run directory (never a sibling's):

    1. Default: ``generations/<gen>/runs/<entry>/events.jsonl`` for the
       requested ``epoch_id`` (then any epoch carrying that generation, since
       a generation id is unique workspace-wide).
    2. Disambiguator: when ``run_id`` / ``match_id`` is given, prefer a
       nested ``runs/<entry>/<disambiguator>/events.jsonl`` (or any nested
       run dir whose own ``runId`` / loss.json ``run_id`` / ``match_id``
       matches) before falling back to (1).

    Returns ``None`` only when no events.jsonl exists for this gen×entry at
    all — the genuine-absence case the honest "could not be reconstructed"
    message is reserved for.
    """
    if not paths.epochs.is_dir():
        return None

    # Locate this entry's run directory. Prefer the requested epoch; a
    # generation id is unique workspace-wide, so fall back to any epoch that
    # carries it (covers a mis-scoped epoch_id from the caller).
    run_dir: Path | None = None
    primary = paths.epochs / epoch_id / "generations" / generation_id / "runs" / entry_id
    if primary.is_dir() or (primary / "events.jsonl").exists():
        run_dir = primary
    else:
        for epoch_d in paths.epochs.iterdir():
            cand = epoch_d / "generations" / generation_id / "runs" / entry_id
            if cand.is_dir() or (cand / "events.jsonl").exists():
                run_dir = cand
                break
    if run_dir is None:
        return None

    disambiguator = run_id or match_id
    if disambiguator:
        # A specific rung was requested. First a directly-named nested dir,
        # then any nested run dir whose events/loss carry the disambiguator.
        nested = run_dir / disambiguator / "events.jsonl"
        if nested.exists():
            return nested
        if run_dir.is_dir():
            for child in sorted(run_dir.iterdir()):
                if not child.is_dir():
                    continue
                ev = child / "events.jsonl"
                if not ev.exists():
                    continue
                if _run_id_of_events_file(ev) == disambiguator:
                    return ev
                loss = _read_json_value(child / "loss.json")
                if isinstance(loss, dict) and (
                    loss.get("run_id") == disambiguator or loss.get("match_id") == disambiguator
                ):
                    return ev
        # Disambiguator did not match a specific rung — fall through to the
        # entry's own canonical events file rather than 404-ing.

    own = run_dir / "events.jsonl"
    if own.exists():
        return own
    return None


def find_generation_run(
    paths: WorkspacePaths, generation_id: str, entry_id: str
) -> tuple[str, Path] | None:
    """Locate the run directory for one ``(generation_id, entry_id)`` pair.

    Returns ``(run_id, events_jsonl_path)``. The run id is the run
    directory's name (the convention zicato uses for board-entry runs).
    Returns ``None`` when no events file is found.
    """
    if not paths.epochs.is_dir():
        return None
    for epoch_dir in paths.epochs.iterdir():
        gen_dir = epoch_dir / "generations" / generation_id
        runs_dir = gen_dir / "runs"
        if not runs_dir.is_dir():
            continue
        # Exact directory match on the entry id is the common layout.
        direct = runs_dir / entry_id
        events = direct / "events.jsonl"
        if events.exists():
            return (entry_id, events)
        # Otherwise scan run dirs and match a run whose events.jsonl
        # carries this entry id (rare alternate layout).
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            ev = run_dir / "events.jsonl"
            if ev.exists():
                return (run_dir.name, ev)
    return None


# ---------------------------------------------------------------------------
# Phase-0 redesign: level-aligned views (L0 workspace, L1 contract diff)
# ---------------------------------------------------------------------------


def build_workspace_view(paths: WorkspacePaths) -> dict[str, Any]:
    """L0 (workspace-level) cross-epoch summary.

    Returns the whole-workspace ribbon the new dashboard's Workspace shell
    needs: the per-epoch lineage with a single best (lowest) scalar
    surfaced per epoch, plus a flat ``sparkline`` list of those best
    scalars in epoch (directory) order so the L0 view can paint a tiny
    cross-epoch curve without re-fanning to per-epoch endpoints.

    Each epoch row carries:

    * ``epoch_id``      — directory name on disk.
    * ``goal``          — one-line goal distilled from the proposer brief
      (mirrors :func:`build_epochs_summary`); ``None`` when absent.
    * ``best_scalar``   — the lowest finite per-generation scalar across
      every generation in that epoch, or ``None`` when the index is
      absent or no generation has a scalar yet. Lower is better — the
      tournament gate ranks by it.
    * ``best_generation_id`` — generation id that achieved
      ``best_scalar``; ``None`` paired with a ``None`` scalar.
    * ``generation_count`` — total generations on disk for the epoch.
    * ``promoted_count``   — number of generations marked promoted.
    * ``closed``        — ``True`` when the epoch's ``config.json`` is
      flagged ``closed``; ``False`` otherwise (covers both "open" and
      "no config" cases — open is the only reasonable default).

    The single live ``epoch_id`` (the current epoch marker on disk) is
    surfaced as the top-level ``current_epoch_id`` so the L0 view can
    render the active row with a "live" affordance.

    Every component degrades independently: a missing or unreadable
    input becomes an empty / ``None`` value, never an exception.
    """
    current = read_current_epoch(paths)
    rows: list[dict[str, Any]] = []
    sparkline: list[dict[str, Any]] = []
    if not paths.epochs.is_dir():
        return {
            "current_epoch_id": current,
            "epochs": rows,
            "sparkline": sparkline,
            "ledger": [],
        }

    # Open the analytical index once for all epochs. Absent index = every
    # epoch surfaces a ``None`` best scalar but the row list still renders.
    conn: sqlite3.Connection | None
    try:
        conn = _open_index(paths.index_db)
    except (_IndexAbsent, sqlite3.Error):
        conn = None

    try:
        for epoch_dir in sorted(paths.epochs.iterdir(), key=_epoch_sort_key):
            if not epoch_dir.is_dir():
                continue
            epoch_id = epoch_dir.name

            cfg = _read_json_value(epoch_dir / "config.json")
            closed = False
            if isinstance(cfg, dict) and isinstance(cfg.get("closed"), bool):
                closed = bool(cfg["closed"])

            # Goal — prefer the frozen ``epochs.goal`` field (Task #178);
            # fall back to ``config.json`` then to the brief's ``## Goal``
            # heading so legacy epochs still surface something.
            goal: str | None = None
            if isinstance(cfg, dict):
                raw_goal = cfg.get("goal")
                if isinstance(raw_goal, str) and raw_goal.strip():
                    goal = raw_goal.strip()
            if goal is None:
                distilled = _distill_brief_goal(_read_epoch_brief(epoch_dir))
                if distilled:
                    goal = distilled

            # Walk this epoch's generations from the on-disk lineage —
            # not from the analytical index, which is a best-effort
            # mirror. Promotion + parent are read from the index when
            # available (build_lineage_view will fall back).
            gens_dir = epoch_dir / "generations"
            gen_ids: list[str] = []
            if gens_dir.is_dir():
                for child in sorted(gens_dir.iterdir(), key=lambda p: _natural_key(p.name)):
                    if child.is_dir():
                        gen_ids.append(child.name)

            best_scalar: float | None = None
            best_gen_id: str | None = None
            promoted_count = 0
            if conn is not None:
                for gid in gen_ids:
                    scalar, _entries = _mean_drift_loss_per_generation(conn, epoch_id, gid)
                    if scalar is None or not _is_finite(scalar):
                        continue
                    if best_scalar is None or scalar < best_scalar:
                        best_scalar = scalar
                        best_gen_id = gid
                # Promotion count comes from experiment.json (durable on
                # disk), not the index, so this is robust to an absent /
                # stale ``promotions`` table.
                for gid in gen_ids:
                    exp = _read_json_value(gens_dir / gid / "experiment.json")
                    if isinstance(exp, dict):
                        outcome = exp.get("outcome")
                        if isinstance(outcome, dict):
                            decision = _experiment_decision(exp)
                            if (
                                decision is not None
                                and decision.strip().lower() in _PROMOTED_DECISIONS
                            ):
                                promoted_count += 1

            # Lineage edge — read ``parent_epoch_id`` from the index
            # when available so the L0 lineage table can render arrows
            # between consecutive epochs. Best-effort: a v1 / never-
            # indexed database surfaces ``None`` and the L0 view falls
            # back to directory order.
            parent_epoch_id: str | None = None
            if conn is not None:
                try:
                    row_ep = conn.execute(
                        "SELECT parent_epoch_id FROM epochs WHERE epoch_id = ?",
                        (epoch_id,),
                    ).fetchone()
                    if row_ep is not None:
                        raw_p = row_ep["parent_epoch_id"]
                        if isinstance(raw_p, str) and raw_p:
                            parent_epoch_id = raw_p
                except sqlite3.Error:
                    parent_epoch_id = None

            row = {
                "epoch_id": epoch_id,
                "goal": goal,
                "best_scalar": best_scalar,
                "best_generation_id": best_gen_id,
                "generation_count": len(gen_ids),
                "promoted_count": promoted_count,
                "closed": closed,
                "parent_epoch_id": parent_epoch_id,
            }
            rows.append(row)
            sparkline.append({"epoch_id": epoch_id, "scalar": best_scalar})
    finally:
        if conn is not None:
            conn.close()

    # The cross-epoch COMPOSED META-LOOP LEDGER matrix (study opt 7): one
    # ordered row per epoch carrying the held floor, the champion that set it,
    # the generation_count (effort), the frozen structure, and the
    # per-component change map vs the predecessor — including the ``proposer``
    # + ``structure`` levers the L1 contract-diff omits. Derived from the same
    # on-disk records; degrades independently to an empty list. Surfaced as a
    # sibling field so the home view reads the ledger from the SAME
    # ``/api/workspace`` read it already consumes (no extra fan-out).
    ledger = build_meta_loop_ledger(paths).get("epochs", [])

    return {
        "current_epoch_id": current,
        "epochs": rows,
        "sparkline": sparkline,
        "ledger": ledger,
    }


# Component names recorded in ``contract_components.json`` (mirrors the
# orchestrator's :func:`_changed_components` set). Pinned here so a stray
# / unknown key on disk does not silently change the diff output shape.
_CONTRACT_COMPONENT_NAMES = (
    "board",
    "brief",
    "scoring",
    "entrypoint",
    "mutable_trees",
)


def _read_contract_components(paths: WorkspacePaths, epoch_id: str) -> dict[str, str]:
    """Return the per-component contract sub-hashes for one epoch.

    Mirrors the orchestrator's ``_stored_component_hashes`` reader: the
    breakdown is written next to ``config.json`` as
    ``contract_components.json`` when an epoch is created or rolled.
    Returns an empty dict when the file is missing or unreadable so the
    diff caller can render a "no breakdown available" state for legacy
    epochs.
    """
    path = paths.epochs / epoch_id / "contract_components.json"
    raw = _read_json_value(path)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def build_contract_diff(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """L1 (epoch-level) contract diff vs the predecessor epoch.

    Compares the named epoch's ``contract_components.json`` against the
    immediately preceding epoch's. The predecessor is resolved as the
    epoch whose id sorts just before ``epoch_id`` in the on-disk listing
    (matches the convention :func:`build_epochs_summary` uses).

    Returns::

        {
            "epoch_id": str,
            "predecessor_epoch_id": str | None,
            "components": [
                { "name": str, "current_hash": str|None,
                  "previous_hash": str|None, "changed": bool }
            ],
            "any_changed": bool,
        }

    A component is listed even when both hashes are missing (so the L1
    view can render a stable five-row matrix). ``changed`` is ``True``
    iff the two hashes differ AND both are non-empty (an unknown
    predecessor hash is "no diff signal", not "everything changed").

    The first epoch on disk reports ``predecessor_epoch_id = None`` and
    every component as not-changed: there is nothing to diff against.
    """
    cur = _read_contract_components(paths, epoch_id)

    # Resolve predecessor: the epoch immediately before ``epoch_id`` in the
    # CANONICAL (timestamp-first) order — the same single authority every
    # epoch-list view orders by, so the contract diff attributes against the
    # true chronological predecessor rather than the lexically-prior id.
    predecessor: str | None = None
    ids = list_epoch_ids(paths)
    if epoch_id in ids:
        idx = ids.index(epoch_id)
        if idx > 0:
            predecessor = ids[idx - 1]

    prev: dict[str, str] = {}
    if predecessor is not None:
        prev = _read_contract_components(paths, predecessor)

    components: list[dict[str, Any]] = []
    any_changed = False
    for name in _CONTRACT_COMPONENT_NAMES:
        cur_hash = cur.get(name) or None
        prev_hash = prev.get(name) or None
        changed = (
            predecessor is not None
            and cur_hash is not None
            and prev_hash is not None
            and cur_hash != prev_hash
        )
        if changed:
            any_changed = True
        components.append(
            {
                "name": name,
                "current_hash": cur_hash,
                "previous_hash": prev_hash,
                "changed": changed,
            }
        )

    return {
        "epoch_id": epoch_id,
        "predecessor_epoch_id": predecessor,
        "components": components,
        "any_changed": any_changed,
    }


# The surfaced ledger components, in heatstrip column order. This SUPERSETS
# :data:`_CONTRACT_COMPONENT_NAMES` with the two levers the per-epoch
# contract-diff endpoint omits:
#
#   * ``structure`` — NOT a ``contract_components.json`` sub-hash (structure is
#     a per-epoch tournament attribute, not a contract-hash component). It is
#     derived from each epoch's frozen ``scoring.json`` ``tournament.structure``
#     and folded in as its own change signal so a structure roll is attributed.
#   * ``proposer`` — IS persisted in ``contract_components.json`` (the
#     orchestrator's :func:`compute_component_hashes` emits it), but the L1
#     contract-diff endpoint surfaces only the original five. The meta-loop
#     ledger restores it: "proposer/skills change rolls the epoch", so it must
#     read as a first-class lever in the cross-epoch attribution.
_LEDGER_COMPONENT_NAMES = (
    "board",
    "brief",
    "scoring",
    "entrypoint",
    "mutable_trees",
    "structure",
    "proposer",
)


def _epoch_structure(paths: WorkspacePaths, epoch_id: str) -> str:
    """Return one epoch's frozen tournament structure token.

    Reads the per-epoch ``scoring.json`` ``tournament`` block (the
    contract-frozen structure, the same source the Epoch view names). A
    ``scoring.json`` that predates per-epoch structure (no ``tournament``
    key) degrades to ``"gauntlet"`` — the data model's default and the
    same fallback :func:`_tournament_block_from_scoring` applies.
    """
    block = _tournament_block_from_scoring(
        _read_json_value(paths.epochs / epoch_id / "scoring.json")
    )
    if isinstance(block, dict):
        return _normalize_structure(block.get("structure"))
    return "gauntlet"


def build_meta_loop_ledger(paths: WorkspacePaths) -> dict[str, Any]:
    """The cross-epoch COMPOSED META-LOOP LEDGER matrix (study opt 7).

    One ordered row per epoch (canonical timestamp-first order) carrying the
    three braided signals the composed ledger renders:

    * ``floor``            — the held loss FLOOR: the lowest finite
      per-generation scalar in the epoch (== ``best_scalar``; lower is
      better). ``None`` when no generation has a scalar yet.
    * ``champion_gen``     — the generation that SET that floor (the
      champion reign tick); ``None`` paired with a ``None`` floor.
    * ``champion_index``   — the 0-based ordinal of ``champion_gen`` among
      the epoch's generations in their natural (sorted) order; this anchors
      the champion-reign tick so its position encodes WHEN in the epoch the
      floor was set (early → left of the band, late → right). ``None`` when
      the champion can't be located in the ordered list (never a guess).
    * ``generation_count`` — the epoch's generation count (effort → the
      effort-proportional band width).
    * ``structure``        — the epoch's frozen tournament structure token.
    * ``closed`` / ``open`` — lifecycle, so the open epoch dashes.
    * ``changed_components`` — the per-component change MAP vs the
      PREDECESSOR epoch over :data:`_LEDGER_COMPONENT_NAMES` (the five
      surfaced contract components PLUS ``structure`` and ``proposer``).
      A component is ``True`` iff it has a comparable signal that differs
      from the predecessor: contract sub-hashes are compared when BOTH are
      present (an absent legacy hash is "no signal", not "changed");
      ``structure`` is compared by its derived token. The first epoch has
      an all-``False`` map (nothing to diff against).
    * ``changed_list``     — the changed components as an ordered list (a
      convenience for the change-chip rail).
    * ``soft``             — ``True`` when this roll changed ``structure``:
      the cross-roll floor comparison is a SOFT one (the figure stripes it).

    Every datum is DERIVED from existing per-epoch records — no new
    persistence: the floor / champion / generation_count mirror
    :func:`build_workspace_view`, the component map reuses the
    contract-component reader, and ``structure`` reads the frozen
    ``scoring.json``. Each component degrades independently to a ``None`` /
    ``False`` value, never an exception.
    """
    current = read_current_epoch(paths)
    rows: list[dict[str, Any]] = []
    if not paths.epochs.is_dir():
        return {"current_epoch_id": current, "epochs": rows}

    # CANONICAL (timestamp-first) order — the single epoch-ordering authority.
    # The ledger is surfaced as ``build_workspace_view``'s ``ledger`` field
    # alongside its timestamp-ordered ``epochs`` rows, so the two MUST agree;
    # and the per-row predecessor change-map is only meaningful against the
    # true chronological predecessor.
    epoch_ids = list_epoch_ids(paths)

    conn: sqlite3.Connection | None
    try:
        conn = _open_index(paths.index_db)
    except (_IndexAbsent, sqlite3.Error):
        conn = None

    try:
        prev_hashes: dict[str, str] = {}
        prev_structure: str | None = None
        for idx, epoch_id in enumerate(epoch_ids):
            epoch_dir = paths.epochs / epoch_id

            cfg = _read_json_value(epoch_dir / "config.json")
            closed = bool(
                isinstance(cfg, dict) and isinstance(cfg.get("closed"), bool) and cfg["closed"]
            )

            gens_dir = epoch_dir / "generations"
            gen_ids: list[str] = []
            if gens_dir.is_dir():
                gen_ids = sorted(c.name for c in gens_dir.iterdir() if c.is_dir())

            floor: float | None = None
            champion_gen: str | None = None
            if conn is not None:
                for gid in gen_ids:
                    scalar, _entries = _mean_drift_loss_per_generation(conn, epoch_id, gid)
                    if scalar is None or not _is_finite(scalar):
                        continue
                    if floor is None or scalar < floor:
                        floor = scalar
                        champion_gen = gid

            # ``champion_index`` — the 0-based ordinal of the floor-setting
            # champion among the epoch's generations in their natural
            # (sorted) order; ``None`` when the champion can't be located
            # in the ordered list (never a guess).
            champion_index: int | None = None
            if champion_gen is not None:
                try:
                    champion_index = gen_ids.index(champion_gen)
                except ValueError:
                    champion_index = None

            cur_hashes = _read_contract_components(paths, epoch_id)
            structure = _epoch_structure(paths, epoch_id)

            # Component-change map vs the PREDECESSOR. The first epoch has
            # nothing to diff against → an all-False map.
            changed: dict[str, bool] = {}
            changed_list: list[str] = []
            first = idx == 0
            for name in _LEDGER_COMPONENT_NAMES:
                is_changed = False
                if not first:
                    if name == "structure":
                        # structure is derived per epoch, not a sub-hash; a
                        # change is a token difference (always comparable).
                        is_changed = prev_structure is not None and structure != prev_structure
                    else:
                        cur_h = cur_hashes.get(name) or None
                        prev_h = prev_hashes.get(name) or None
                        is_changed = cur_h is not None and prev_h is not None and cur_h != prev_h
                changed[name] = is_changed
                if is_changed:
                    changed_list.append(name)

            rows.append(
                {
                    "epoch_id": epoch_id,
                    "floor": floor,
                    "champion_gen": champion_gen,
                    "champion_index": champion_index,
                    "generation_count": len(gen_ids),
                    "structure": structure,
                    "closed": closed,
                    "open": not closed,
                    "changed_components": changed,
                    "changed_list": changed_list,
                    "soft": bool(changed.get("structure")),
                }
            )

            prev_hashes = cur_hashes
            prev_structure = structure
    finally:
        if conn is not None:
            conn.close()

    return {"current_epoch_id": current, "epochs": rows}
