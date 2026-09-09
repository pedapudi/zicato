"""events_index — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from zicato.core.measurement import (
    TOURNAMENT_DRAW,
    UNKNOWN_SEED,
    iter_measurement_artifacts,
    measurement_artifact_path,
    unit_artifact_name,
)
from zicato.core.workspace import measurement_from_run_id, run_coordinates_from_dir, run_id_for_unit
from zicato.epoch._storage import RecordError
from zicato.epoch.contract import read_component_hashes
from zicato.epoch.journal import read_experiment_body
from zicato.proposer.brief import brief_goal
from zicato.query._sqlite import open_index_ro_or_none
from zicato.query.decisions import (
    experiment_decision,
    promoted_tristate,
)
from zicato.query.epoch_view import (
    _normalize_structure,
    _read_epoch_brief,
    _tournament_block_from_scoring,
)
from zicato.query.foe_episode import is_episode_log
from zicato.query.gate_view import _mean_drift_loss_per_generation
from zicato.query.paths import (
    WorkspacePaths,
    _is_finite,
    _preview,
    _read_json_value,
    layout_of,
    list_epoch_ids,
    read_current_epoch,
)
from zicato.workspace import (
    events_measurement,
    generation_ids,
    is_events_file,
    iter_epochs,
)
from zicato.workspace.reads import generation_base_seed

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


def _current_events_files(epochs: Path, *, epoch_id: str = "") -> list[Path]:
    """Every current replicate events file, excluding ``*.prev.jsonl``."""
    return sorted(
        path
        for run_dir in epochs.glob(f"{epoch_id or '*'}/generations/*/runs/*")
        for path in iter_measurement_artifacts(run_dir, "events")
        if is_events_file(path)
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
    return list(iter_measurement_artifacts(run_dir, "events"))


def _loss_twin(events_path: Path) -> Path | None:
    """Return the loss sibling carrying the same replicate index."""
    measurement = events_measurement(events_path)
    if measurement is None:
        return None
    return events_path.with_name(unit_artifact_name("loss", measurement))


def _build_run_id_index(paths: WorkspacePaths, *, epoch_id: str = "") -> dict[str, Path]:
    """Scan ``epochs/*/generations/*/runs/*/events.jsonl`` → ``{run_id: path}``.

    Matches on the ``runId`` carried inside each events file rather than on
    the run-directory name, which is the board ENTRY id rather than the run
    id. Results are cached per file. Appending to a stream whose id is already
    known reuses that id without reopening the file; a stream that is new,
    replaced, truncated, or was empty at the last scan is parsed on demand.
    """
    epochs = paths.epochs
    cache_key = str(epochs / epoch_id)
    if not epochs.is_dir():
        return {}
    events_files = (
        _current_events_files(epochs, epoch_id=epoch_id)
        if epoch_id
        else _current_events_files(epochs)
    )
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


def _find_run_events_in_index(
    paths: WorkspacePaths, run_id: str, *, epoch_id: str = ""
) -> Path | None:
    """Fast lookup that touches only a cached run's own file on live appends."""
    cache_key = str(paths.epochs / epoch_id)
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
    return _build_run_id_index(paths, epoch_id=epoch_id).get(run_id)


def find_run_events_path(paths: WorkspacePaths, run_id: str, *, epoch_id: str = "") -> Path | None:
    """Locate an active run or match its recorded event identity in canonical runs.

    A named epoch confines the active-record path and the event index lookup.
    Missing captures return None.
    """
    run_file = paths.active_runs_dir / f"{run_id}.json"
    run = _read_json_value(run_file)
    if isinstance(run, dict):
        events = run.get("events_jsonl_path")
        if isinstance(events, str) and events and Path(events).exists():
            candidate = Path(events)
            if not epoch_id or candidate.resolve().is_relative_to(
                (paths.epochs / epoch_id).resolve()
            ):
                return candidate

    indexed = _find_run_events_in_index(paths, run_id, epoch_id=epoch_id)
    return indexed if indexed is not None and indexed.exists() else None


def find_generation_entry_events(
    paths: WorkspacePaths, generation_id: str, entry_id: str
) -> Path | None:
    """Resolve the selected draw, strictly within the requested board entry.

    Historical generations without selection provenance retain unqualified
    events. A missing selected-seed capture cannot borrow another seed's events.
    """
    found = find_generation_run(paths, generation_id, entry_id)
    return found[1] if found is not None else None


def resolve_transcript_events(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str | None = None,
    match_id: str | None = None,
) -> Path | None:
    """Resolve a transcript within its requested epoch, generation, and entry.

    A runtime run id selects its exact seed and draw. A match id selects the
    capture whose paired loss names that matchup. An unmatched supplied id
    returns None. Without an id, the generation score selects ordinary draw zero.
    """
    if not paths.epochs.is_dir():
        return None

    layout = layout_of(paths)
    run_dir: Path | None = None
    epoch_ids = [epoch_id] if epoch_id else [epoch.id for epoch in iter_epochs(layout)]
    for candidate_epoch in epoch_ids:
        candidate = layout.run_dir(candidate_epoch, generation_id, entry_id)
        if candidate.is_dir():
            run_dir = candidate
            break
    if run_dir is None:
        return None

    disambiguator = run_id or match_id
    if disambiguator:
        if run_id:
            measurement = measurement_from_run_id(
                generation_id, entry_id, run_id, epoch_id=epoch_id
            )
            if measurement is not None:
                exact = measurement_artifact_path(
                    run_dir,
                    "events",
                    measurement,
                    base_seed=measurement.base_seed,
                )
                return exact if exact.exists() else None
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

        return None

    coordinates = run_coordinates_from_dir(run_dir)
    if coordinates is None:
        return None
    try:
        seed = generation_base_seed(layout, coordinates[0], generation_id)
    except ValueError:
        return None
    own = measurement_artifact_path(run_dir, "events", TOURNAMENT_DRAW, base_seed=seed)
    return own if own.is_file() else None


def find_proposal_episode_log(
    paths: WorkspacePaths, epoch_id: str, generation_id: str, *, slot_index: int | None = None
) -> Path | None:
    """Locate the Foe ``episode.jsonl`` that proposed one generation.

    A round writes one episode directory per candidate under the epoch's
    ``episodes/``, named for the generation it proposes and, in a best-of-N
    slate, for the slot as well. ``slot_index`` names one slate slot; without
    it the whole-generation directory is preferred and the lowest-numbered
    slot answers for a slate, so a caller that knows only the generation
    still reaches an episode.
    Repeated attempts retain separate logs; the most recently written valid
    attempt answers for its candidate or slot.

    A named epoch confines the lookup to that epoch, including when its
    episode is absent. An omitted epoch searches the workspace in canonical
    epoch order. The returned path is always a Foe episode log: a file that is not one
    (:func:`zicato.query.foe_episode.is_episode_log`) is refused rather than
    served, because a proposer transcript reconstructed from any other format
    would claim a fidelity the format does not give.
    """
    layout = layout_of(paths)
    epoch_ids = [epoch_id] if epoch_id else [epoch.id for epoch in iter_epochs(layout)]
    for candidate_epoch in epoch_ids:
        directories: list[Path] = []
        if slot_index is None:
            directories.append(layout.proposal_episode_dir(candidate_epoch, generation_id))
            directories += _slate_episode_dirs(layout.episodes_dir(candidate_epoch), generation_id)
        else:
            directories.append(
                layout.proposal_episode_dir(candidate_epoch, generation_id, slot_index)
            )
        for directory in directories:
            attempts: list[tuple[int, Path]] = []
            for path in (directory / "attempts").glob("*/episode.jsonl"):
                try:
                    attempts.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
            attempts.sort(reverse=True)
            for log in [*(path for _, path in attempts), directory / "episode.jsonl"]:
                if log.exists() and is_episode_log(log):
                    return log
    return None


def _slate_episode_dirs(episodes: Path, generation_id: str) -> list[Path]:
    """One slate's episode directories, in slot order.

    A generation id is ``v`` followed by a round number
    (:func:`zicato.workspace.epochs.next_generation_id`), so a trailing
    ``-<digits>`` on a directory name is the slate slot and nothing else.
    """
    if not episodes.is_dir():
        return []
    slots: list[tuple[int, Path]] = []
    for child in episodes.iterdir():
        prefix, separator, slot = child.name.rpartition("-")
        if separator and prefix == generation_id and slot.isdigit():
            slots.append((int(slot), child))
    return [directory for _slot, directory in sorted(slots)]


def find_generation_run(
    paths: WorkspacePaths, generation_id: str, entry_id: str
) -> tuple[str, Path] | None:
    """Locate the ordinary draw selected by a generation's persisted score.

    A known selected seed returns its canonical runtime identity. Without seed
    provenance, only the historical unqualified entry remains available for
    audit. A live seed-qualified run before score publication requires its exact
    producer run id; this lookup cannot infer selection from runtime defaults.
    """
    layout = layout_of(paths)
    for epoch in iter_epochs(layout):
        try:
            seed = generation_base_seed(layout, epoch.id, generation_id)
        except ValueError:
            continue
        events = layout.events(epoch.id, generation_id, entry_id, base_seed=seed)
        if events.is_file():
            run_id = (
                entry_id
                if seed is UNKNOWN_SEED
                else run_id_for_unit(generation_id, entry_id, base_seed=seed, epoch_id=epoch.id)
            )
            return run_id, events
    return None


def read_run_result(events_path: Path) -> dict[str, Any] | None:
    """Read the loss beside a transcript and expose the fields used by the dashboard."""
    from zicato.core.measurement import artifact_measurement, unit_artifact_name

    measurement = artifact_measurement(events_path.name, "events")
    if measurement is None:
        return None
    loss_path = events_path.with_name(unit_artifact_name("loss", measurement))
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
            brief_unreadable: str | None = None
            if goal is None:
                try:
                    distilled = brief_goal(_read_epoch_brief(epoch_dir))
                    if distilled:
                        goal = _preview(distilled)
                except RecordError as exc:
                    brief_unreadable = str(exc)

            # Walk this epoch's generations from the on-disk lineage —
            # not from the analytical index, which is a best-effort
            # mirror. Promotion + parent are read from the index when
            # available (build_lineage_view will fall back).
            gen_ids = generation_ids(layout, epoch_id)

            best_scalar: float | None = None
            best_gen_id: str | None = None
            promoted_count = 0
            unreadable_generations: list[str] = []
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
                    try:
                        exp = read_experiment_body(layout.root, epoch_id, gid)
                    except RecordError as exc:
                        # A record that will not parse states nothing about
                        # promotion, so it counts as neither promoted nor not
                        # — which leaves ``promoted_count`` understated by an
                        # unknown amount. The row carries the reason so the
                        # count is read as incomplete rather than as a fact.
                        unreadable_generations.append(str(exc))
                        continue
                    if exp is not None:
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

            row: dict[str, Any] = {
                "epoch_id": epoch_id,
                "goal": goal,
                "best_scalar": best_scalar,
                "best_generation_id": best_gen_id,
                "generation_count": len(gen_ids),
                "promoted_count": promoted_count,
                "closed": closed,
                "parent_epoch_id": parent_epoch_id,
            }
            # CONDITIONAL key: present only when a generation record in this
            # epoch would not parse, so a workspace whose records are intact
            # keeps its prior payload byte for byte.
            if unreadable_generations:
                row["unreadable_generations"] = unreadable_generations
            if brief_unreadable is not None:
                row["unreadable"] = brief_unreadable
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


# The contract view displays these evaluation components. Proposer identity
# appears in the ledger; future stored keys remain readable without widening this view.
_CONTRACT_COMPONENT_NAMES = (
    "board",
    "brief",
    "scoring",
    "evaluator_revision",
    "adapter",
    "mutable_trees",
)


def _read_contract_components(
    paths: WorkspacePaths, epoch_id: str
) -> tuple[dict[str, str], str | None]:
    """Project accepted hashes and retain a refusal for the consuming view."""
    try:
        return read_component_hashes(layout_of(paths).contract_components(epoch_id)) or {}, None
    except RecordError as exc:
        return {}, str(exc)


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
    cur, error = _read_contract_components(paths, epoch_id)

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
        prev, previous_error = _read_contract_components(paths, predecessor)
        error = "; ".join(filter(None, (error, previous_error))) or None

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
        **({"error": error} if error else {}),
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
        previous_error: str | None = None
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

            cur_hashes, current_error = _read_contract_components(paths, epoch_id)
            error = "; ".join(filter(None, (current_error, previous_error)))
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
                    **({"error": error} if error else {}),
                }
            )

            prev_hashes = cur_hashes
            previous_error = current_error
            prev_structure = structure

    return {"current_epoch_id": current, "epochs": rows}
