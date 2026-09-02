"""events_index — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from zicato.core.workspace import replicate_index_from_run_id
from zicato.query._sqlite import open_index_ro_or_none
from zicato.query.decisions import (
    experiment_decision,
    promoted_tristate,
)
from zicato.query.epoch_view import (
    _distill_brief_goal,
    _normalize_structure,
    _read_epoch_brief,
    _tournament_block_from_scoring,
)
from zicato.query.gate_view import _mean_drift_loss_per_generation
from zicato.query.paths import (
    WorkspacePaths,
    _is_finite,
    _read_json_value,
    layout_of,
    list_epoch_ids,
    read_current_epoch,
)
from zicato.workspace import (
    events_replicate_index,
    generation_ids,
    is_events_file,
    iter_epochs,
    read_experiment,
    run_entry_ids,
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
# layout names run directories by ENTRY id rather than run id, so the only way
# to map a run id to its events file is to read the ``runId`` field out of each
# current events file. Cache each file independently: a live append keeps the
# already-discovered id, while a file that is new, replaced, truncated, or was
# empty at the last scan is reparsed. This avoids reopening the workspace on every
# event appended by an in-progress run.
_RunIdFileState = tuple[int, int, int, int, str | None]
_RunIdIndexState = tuple[dict[str, _RunIdFileState], dict[str, Path]]
_RUN_ID_INDEX_CACHE: dict[str, _RunIdIndexState] = {}


def _current_events_files(epochs: Path) -> list[Path]:
    """Every current replicate events file, excluding ``*.prev.jsonl``."""
    return sorted(
        path for path in epochs.glob("*/generations/*/runs/*/events*.jsonl") if is_events_file(path)
    )


def _run_id_file_state(path: Path, cached: _RunIdFileState | None) -> _RunIdFileState | None:
    """Return metadata + id, retaining a resolved id across pure appends.

    An append (same inode, grown size) keeps the id already parsed out of
    the file; anything else reparses. The one case this cannot see is a
    truncate-in-place that lands on the same size within the same mtime
    tick, which would keep a stale id. Production never does that: a
    ``mode="write"`` sink archives the old file to its ``.prev.jsonl``
    sibling first (:func:`zicato.telemetry.sink.archive_prior_events`), so
    the replacement is a NEW inode and the identity comparison catches it.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if cached is not None:
        old_dev, old_ino, old_mtime, old_size, run_id = cached
        unchanged = identity == (old_dev, old_ino, old_mtime, old_size)
        appended = (
            run_id is not None
            and (stat.st_dev, stat.st_ino) == (old_dev, old_ino)
            and stat.st_size > old_size
        )
        if unchanged or appended:
            return (*identity, run_id)
    return (*identity, _run_id_of_events_file(path))


def _replicate_events_in_run(run_dir: Path) -> list[Path]:
    """Return the run directory's current replicate event files."""
    return sorted(path for path in run_dir.glob("events*.jsonl") if is_events_file(path))


def _loss_twin(events_path: Path) -> Path | None:
    """Return the loss sibling carrying the same replicate index."""
    replicate_index = events_replicate_index(events_path)
    if replicate_index is None:
        return None
    if replicate_index == 0:
        return events_path.with_name("loss.json")
    return events_path.with_name(f"loss.r{replicate_index}.json")


def _nested_events_for_disambiguator(run_dir: Path, disambiguator: str) -> Path | None:
    """Resolve one transcript in the nested-rung layout inside an entry run dir."""
    direct = run_dir / disambiguator / "events.jsonl"
    if direct.exists():
        return direct
    if not run_dir.is_dir():
        return None
    for child in sorted(run_dir.iterdir()):
        if not child.is_dir():
            continue
        events = child / "events.jsonl"
        if not events.exists():
            continue
        if _run_id_of_events_file(events) == disambiguator:
            return events
        loss = _read_json_value(child / "loss.json")
        if isinstance(loss, dict) and (
            loss.get("run_id") == disambiguator or loss.get("match_id") == disambiguator
        ):
            return events
    return None


def _build_run_id_index(paths: WorkspacePaths) -> dict[str, Path]:
    """Scan ``epochs/*/generations/*/runs/*/events.jsonl`` → ``{run_id: path}``.

    Matches on the ``runId`` carried inside each events file rather than on
    the run-directory name, which is the board ENTRY id rather than the run
    id. Results are cached per file. Appending to a stream whose id is already
    known reuses that id without reopening the file; a stream that is new,
    replaced, truncated, or was empty at the last scan is parsed on demand.
    """
    epochs = paths.epochs
    cache_key = str(epochs)
    if not epochs.is_dir():
        return {}
    events_files = _current_events_files(epochs)
    cached_entry = _RUN_ID_INDEX_CACHE.get(cache_key)
    cached = cached_entry[0] if cached_entry is not None else {}

    index: dict[str, Path] = {}
    current: dict[str, _RunIdFileState] = {}
    for events_path in events_files:
        path_key = str(events_path)
        state = _run_id_file_state(events_path, cached.get(path_key))
        if state is None:
            continue
        current[path_key] = state
        rid = state[-1]
        if rid and rid not in index:
            index[rid] = events_path
    _RUN_ID_INDEX_CACHE[cache_key] = (current, index)
    return index


def _find_run_events_in_index(paths: WorkspacePaths, run_id: str) -> Path | None:
    """Fast lookup that touches only a cached run's own file on live appends."""
    cache_key = str(paths.epochs)
    cached = _RUN_ID_INDEX_CACHE.get(cache_key)
    if cached is not None:
        states, index = cached
        events_path = index.get(run_id)
        if events_path is not None:
            path_key = str(events_path)
            state = _run_id_file_state(events_path, states.get(path_key))
            if state is not None and state[-1] == run_id:
                states[path_key] = state
                return events_path
            index.pop(run_id, None)
            if state is None:
                states.pop(path_key, None)
            else:
                states[path_key] = state
                discovered = state[-1]
                if discovered:
                    index.setdefault(discovered, events_path)
    return _build_run_id_index(paths).get(run_id)


# Cache: workspace epochs dir → {run_id: gen×entry events.jsonl path}. In
# successive-halving racing the fixed champion (e.g. v0) is RE-RACED / REUSED
# across rungs, so the same gen×entry yields MULTIPLE per-rung run records —
# but only ONE rung actually executed and emitted its own events.jsonl; the
# rest are score-reuse records carrying a distinct ``run_id`` with NO
# transcript of their own. Each such record is written as a
# ``runs/<entry>/loss.json`` carrying both its ``run_id`` and the gen×entry it
# belongs to (the run directory it lives under). Mapping every such ``run_id``
# to its gen×entry ``events.jsonl`` lets a transcript-less reuse run_id resolve
# to the one real transcript for that pair. Memoized on the epochs-dir mtime
# (the reuse records are settled rather than live streams).
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
       id rather than run id, so the run id only appears inside the events.

    Returns ``None`` when nothing matches.
    """
    run_file = paths.active_runs_dir / f"{run_id}.json"
    run = _read_json_value(run_file)
    if isinstance(run, dict):
        events = run.get("events_jsonl_path")
        if isinstance(events, str) and events and Path(events).exists():
            return Path(events)

    layout = layout_of(paths)
    for epoch in iter_epochs(layout):
        for generation_id in generation_ids(layout, epoch.id):
            events = layout.events(epoch.id, generation_id, run_id)
            if events.exists():
                return events

    # Fall back to the run-id → events.jsonl index (matches the canonical
    # board-run layout, where the run directory is named by entry id).
    indexed = _find_run_events_in_index(paths, run_id)
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
    successive-halving champion fallback: an absent gen×entry
    must NOT fabricate some other entry's transcript. Returns ``None`` when
    no such file exists.
    """
    for epoch in iter_epochs(layout_of(paths)):
        events = (
            epoch.directory / "generations" / generation_id / "runs" / entry_id / "events.jsonl"
        )
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

    The deterministic triple is the primary key. A ``run_id`` / ``match_id``
    disambiguates both sibling replicate files (``events.rN.jsonl``) and the
    nested directories used by successive-halving reruns. With no
    disambiguator the canonical replicate-0 ``events.jsonl`` remains the
    default.

    Resolution, strict to this entry's own run directory (never a sibling's):

    1. Locate ``generations/<gen>/runs/<entry>`` in the requested
       ``epoch_id`` (then any epoch carrying that generation, since a
       generation id is unique workspace-wide).
    2. Disambiguator: a ``match_id`` first selects the nested-rung
       layout; a ``run_id`` first selects an exact sibling
       ``events.rN.jsonl`` by validated runtime id, event ``runId``, or its
       matching loss record. Each then falls back to the other layout.
    3. Default or unmatched disambiguator: return canonical
       ``events.jsonl`` (replicate 0).

    Returns ``None`` only when no events.jsonl exists for this gen×entry at
    all — the genuine-absence case the honest "could not be reconstructed"
    message is reserved for.
    """
    if not paths.epochs.is_dir():
        return None

    # Locate this entry's run directory. Prefer the requested epoch; a
    # generation id is unique workspace-wide, so fall back to any epoch that
    # carries it (covers a mis-scoped epoch_id from the caller).
    layout = layout_of(paths)
    run_dir: Path | None = None
    primary = layout.run_dir(epoch_id, generation_id, entry_id)
    if primary.is_dir() or (primary / "events.jsonl").exists():
        run_dir = primary
    else:
        for epoch in iter_epochs(layout):
            cand = epoch.directory / "generations" / generation_id / "runs" / entry_id
            if cand.is_dir() or (cand / "events.jsonl").exists():
                run_dir = cand
                break
    if run_dir is None:
        return None

    disambiguator = run_id or match_id
    if disambiguator:
        # A match id is a rung coordinate. Prefer the nested-rung layout
        # before looking at top-level loss metadata: the
        # canonical replicate's loss can carry the same match id and must not
        # shadow the rung's own transcript.
        if match_id:
            nested = _nested_events_for_disambiguator(run_dir, match_id)
            if nested is not None:
                return nested

        # Replicates share the entry directory, so resolve their sibling file
        # before considering the nested-directory layout used by racing.
        if run_id:
            replicate_index = replicate_index_from_run_id(generation_id, entry_id, run_id)
            if replicate_index is not None:
                exact = (
                    run_dir / "events.jsonl"
                    if replicate_index == 0
                    else run_dir / f"events.r{replicate_index}.jsonl"
                )
                if exact.exists():
                    return exact
        for events in _replicate_events_in_run(run_dir):
            if run_id and _run_id_of_events_file(events) == run_id:
                return events
            loss_path = _loss_twin(events)
            loss = _read_json_value(loss_path) if loss_path is not None else None
            if isinstance(loss, dict) and (
                (run_id and loss.get("run_id") == run_id)
                or (match_id and loss.get("match_id") == match_id)
            ):
                return events

        if run_id:
            nested = _nested_events_for_disambiguator(run_dir, run_id)
            if nested is not None:
                return nested
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
    layout = layout_of(paths)
    for epoch in iter_epochs(layout):
        # Exact directory match on the entry id is the common layout.
        events = layout.events(epoch.id, generation_id, entry_id)
        if events.exists():
            return (entry_id, events)
        # Otherwise scan run records and match one whose events.jsonl
        # carries this entry id (rare alternate layout).
        for run_entry_id in run_entry_ids(layout, epoch.id, generation_id):
            ev = layout.events(epoch.id, generation_id, run_entry_id)
            if ev.exists():
                return (run_entry_id, ev)
    return None


def read_run_result(run_dir: Path) -> dict[str, Any] | None:
    """Project a run directory's ``loss.json`` into a small dashboard shape.

    The frontend needs enough to render an honest "what happened" panel
    for a zero-turn complete run — wall-clock budget exceeded, runtime,
    pass/fail verdict, expectation outcome, and the user-visible metric
    counts (LLM calls, output chars, anything else loss.json already
    publicly exposes). The full ``LossProfile`` would leak internal
    fields (the drift scalar's weight breakdown, schema versioning, the
    canonical adk session id) that the dashboard does not render today;
    project to the subset that matters.

    Returns ``None`` when the run directory has no readable
    ``loss.json`` — the frontend then falls back to the existing
    "This run produced no transcript turns" message. The degrade is the
    same-shaped ``None`` the caller already handles.
    """
    if not isinstance(run_dir, Path):
        return None
    loss_path = run_dir / "loss.json"
    if not loss_path.exists():
        return None
    try:
        with open(loss_path, encoding="utf-8") as f:
            loss = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loss, dict):
        return None

    expectation: dict[str, Any] | None = None
    raw_exp = loss.get("expectation_result")
    if isinstance(raw_exp, dict):
        expectation = {
            "kind": str(raw_exp.get("kind") or ""),
            "passed": bool(raw_exp.get("passed", False)),
            "detail": str(raw_exp.get("detail") or ""),
        }

    metric_counts: list[dict[str, Any]] = []
    raw_metrics = loss.get("metric_counts")
    if isinstance(raw_metrics, list):
        for m in raw_metrics:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            count = m.get("count")
            if not isinstance(name, str) or count is None:
                continue
            try:
                count_f = float(count)
            except (TypeError, ValueError):
                continue
            metric_counts.append(
                {
                    "name": name,
                    "count": count_f,
                    "severity": str(m.get("severity") or ""),
                }
            )

    pass_fail = loss.get("pass_fail")
    return {
        "wall_clock_budget_exceeded": bool(loss.get("wall_clock_budget_exceeded", False)),
        "runtime_ms": int(loss.get("runtime_ms") or 0),
        "pass_fail": None if pass_fail is None else bool(pass_fail),
        "expectation_result": expectation,
        "metric_counts": metric_counts,
        "drift_loss": (
            float(loss["drift_loss"]) if isinstance(loss.get("drift_loss"), int | float) else None
        ),
    }


# ---------------------------------------------------------------------------
# Level-aligned views: the workspace summary and the epoch contract diff
# ---------------------------------------------------------------------------


def build_workspace_view(paths: WorkspacePaths) -> dict[str, Any]:
    """The workspace-level cross-epoch summary.

    Returns the whole-workspace ribbon the dashboard's Workspace shell needs:
    the per-epoch lineage with a single best (lowest) scalar per epoch, plus a
    flat ``sparkline`` list of those best scalars in epoch order, so the
    workspace view paints a cross-epoch curve without re-fanning to per-epoch
    endpoints.

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
    surfaced as the top-level ``current_epoch_id`` so the workspace view can
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

    with open_index_ro_or_none(paths.index_db) as conn:
        layout = layout_of(paths)
        for epoch in iter_epochs(layout):
            epoch_dir = epoch.directory
            epoch_id = epoch.id

            cfg = _read_json_value(epoch_dir / "config.json")
            closed = False
            if isinstance(cfg, dict) and isinstance(cfg.get("closed"), bool):
                closed = bool(cfg["closed"])

            # Goal — prefer the frozen ``epochs.goal`` field (Task #178);
            # fall back to ``config.json`` then to the brief's ``## Goal``
            # heading, so an epoch whose record carries no goal field still
            # surfaces something.
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
            gen_ids = generation_ids(layout, epoch_id)

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
                # disk) rather than the index, so this is robust to an absent
                # or stale ``promotions`` table.
                for gid in gen_ids:
                    exp = read_experiment(layout, epoch_id, gid)
                    if isinstance(exp, dict):
                        outcome = exp.get("outcome")
                        if isinstance(outcome, dict):
                            if promoted_tristate(experiment_decision(exp)) is True:
                                promoted_count += 1

            # Lineage edge — read ``parent_epoch_id`` from the index
            # when available so the workspace lineage table can render arrows
            # between consecutive epochs. Best-effort: a v1 or never-indexed
            # database surfaces ``None`` and the view falls back to directory
            # order.
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

    # The cross-epoch COMPOSED META-LOOP LEDGER matrix: one
    # ordered row per epoch carrying the held floor, the champion that set it,
    # the generation_count (effort), the frozen structure, and the
    # per-component change map vs the predecessor — including the ``proposer``
    # + ``structure`` levers the epoch contract-diff omits. Derived from the same
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
    "evaluator_revision",
    "adapter",
    "mutable_trees",
)


def _read_contract_components(paths: WorkspacePaths, epoch_id: str) -> dict[str, str]:
    """Return the per-component contract sub-hashes for one epoch.

    Mirrors the orchestrator's ``_stored_component_hashes`` reader: the
    breakdown is written next to ``config.json`` as
    ``contract_components.json`` when an epoch is created or rolled.
    Returns an empty dict when the file is missing or unreadable so the
    diff caller can render a "no breakdown available" state for an epoch
    that has no breakdown file.
    """
    path = layout_of(paths).contract_components(epoch_id)
    raw = _read_json_value(path)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def build_contract_diff(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """The epoch-level contract diff against the predecessor epoch.

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

    A component is listed even when both hashes are missing, so the
    contract-diff view renders a stable six-row matrix. ``changed`` is ``True``
    iff the two hashes differ AND both are non-empty: an unknown predecessor
    hash is "no diff signal" rather than "everything changed".

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
#     a per-epoch tournament attribute rather than a contract-hash component).
#     It is
#     derived from each epoch's frozen ``scoring.json`` ``tournament.structure``
#     and folded in as its own change signal so a structure roll is attributed.
#   * ``proposer`` — IS persisted in ``contract_components.json`` (the
#     orchestrator's :func:`compute_component_hashes` emits it), but the epoch
#     contract-diff endpoint surfaces only six contract sub-hashes. The
#     meta-loop
#     ledger restores it: "proposer/skills change rolls the epoch", so it must
#     read as a first-class lever in the cross-epoch attribution.
_LEDGER_COMPONENT_NAMES = (
    "board",
    "brief",
    "scoring",
    "evaluator_revision",
    "adapter",
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
    block = _tournament_block_from_scoring(_read_json_value(layout_of(paths).scoring(epoch_id)))
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
      PREDECESSOR epoch over :data:`_LEDGER_COMPONENT_NAMES` (the six surfaced
      contract components PLUS ``structure`` and ``proposer``). A component is
      ``True`` iff it has a comparable signal that differs from the
      predecessor: contract sub-hashes are compared when BOTH are present (a
      hash absent from an older record is "no signal" rather than "changed");
      ``structure`` is compared by its derived token. The first epoch has an
      all-``False`` map (nothing to diff against).
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

    layout = layout_of(paths)
    with open_index_ro_or_none(paths.index_db) as conn:
        prev_hashes: dict[str, str] = {}
        prev_structure: str | None = None
        for idx, epoch_id in enumerate(epoch_ids):
            epoch_dir = layout.epoch_dir(epoch_id)

            cfg = _read_json_value(epoch_dir / "config.json")
            closed = bool(
                isinstance(cfg, dict) and isinstance(cfg.get("closed"), bool) and cfg["closed"]
            )

            gen_ids = generation_ids(layout, epoch_id)

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
                        # structure is derived per epoch rather than being a
                        # sub-hash; a
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

    return {"current_epoch_id": current, "epochs": rows}
