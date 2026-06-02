"""Read-only workspace state assembly for the dashboard service.

This module is the dashboard's equivalent of the (now-retired) Rust
supervisor's ``reader.rs`` / ``tournaments.rs`` / ``run_log.rs`` /
``epoch.rs`` / ``index_db.rs``. It reads the live ``.zicato/`` workspace
on disk and assembles the exact JSON shapes the vanilla-JS dashboard
expects.

Every function here is best-effort: a missing or transiently-truncated
file degrades to an empty / ``None`` value rather than raising, so no
endpoint built on top of this ever returns a 500.

State parsing reuses the typed readers in :mod:`zicato.runtime.state`
(``read_heartbeat`` / ``list_active_runs`` / ``read_active_tournament``)
and the atomic-read helper :func:`zicato.runtime._atomic.read_json`. The
SQLite analytical index is read directly with :mod:`sqlite3` opened
read-only.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from zicato.runtime._atomic import read_json
from zicato.runtime.state import (
    list_active_runs,
    read_active_tournament,
    read_heartbeat,
)

# Default / ceiling for the ``/api/run-log`` ``?limit=`` query — these
# match the Rust supervisor's ``run_log::DEFAULT_LIMIT`` / ``MAX_LIMIT``.
RUN_LOG_DEFAULT_LIMIT = 40
RUN_LOG_MAX_LIMIT = 500

# Char ceiling on truncated text previews (board input, mutation body),
# matching the Rust ``epoch::PREVIEW_CHARS``.
_PREVIEW_CHARS = 120

# Envelope keys that are *not* the payload kind in a goldfive event.
_ENVELOPE_KEYS = frozenset(
    {
        "emittedAt",
        "emitted_at",
        "eventId",
        "event_id",
        "runId",
        "run_id",
        "sessionId",
        "session_id",
        "sequence",
        "seq",
        "kind",
        "payload",
    }
)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _iso(dt: _dt.datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Workspace path layout
# ---------------------------------------------------------------------------


class WorkspacePaths:
    """The ``.zicato/`` layout the dashboard reads.

    ``root`` is the ``.zicato`` directory itself, matching the convention
    every other zicato helper uses (``runtime/`` and ``epochs/`` hang
    directly off it).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def epochs(self) -> Path:
        return self.root / "epochs"

    @property
    def heartbeat(self) -> Path:
        return self.runtime / "heartbeat.json"

    @property
    def lock(self) -> Path:
        return self.runtime / "lock.json"

    @property
    def active_runs_dir(self) -> Path:
        return self.runtime / "active_runs"

    @property
    def active_tournament(self) -> Path:
        return self.runtime / "active_tournament.json"

    @property
    def control_dir(self) -> Path:
        return self.runtime / "control"

    @property
    def current_epoch_marker(self) -> Path:
        return self.root / "current_epoch"

    @property
    def lineage(self) -> Path:
        return self.root / "lineage.json"

    @property
    def index_db(self) -> Path:
        return self.root / "index.db"

    def epoch_health_dir(self, epoch_id: str) -> Path:
        return self.epochs / epoch_id / "health"


# ---------------------------------------------------------------------------
# Small primitives
# ---------------------------------------------------------------------------


def _read_json_value(path: Path) -> Any | None:
    """Best-effort JSON read; missing / empty / malformed -> ``None``."""
    try:
        return read_json(path)
    except Exception:
        return None


def to_snake(name: str) -> str:
    """Convert a ``camelCase`` / ``PascalCase`` identifier to ``snake_case``.

    Idempotent on input already in snake_case. Mirrors the Rust
    ``run_log::to_snake`` so event kinds key on one stable vocabulary
    (the zicato#1 normalization).
    """
    out: list[str] = []
    prev_lower_or_digit = False
    for ch in name:
        if ch.isascii() and ch.isupper():
            if prev_lower_or_digit:
                out.append("_")
            out.append(ch.lower())
            prev_lower_or_digit = False
        else:
            out.append(ch)
            prev_lower_or_digit = ch.isascii() and (ch.islower() or ch.isdigit())
    return "".join(out)


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "..."


def read_current_epoch(paths: WorkspacePaths) -> str | None:
    """Return the current epoch id from the ``current_epoch`` marker."""
    try:
        text = paths.current_epoch_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def list_epoch_ids(paths: WorkspacePaths) -> list[str]:
    """Every epoch id on disk (the ``epochs/`` subdirectories), sorted.

    The set of epochs a ``?epoch=<id>`` request may legally resolve to.
    Returns an empty list when the workspace has no ``epochs/`` directory.
    """
    if not paths.epochs.is_dir():
        return []
    return sorted(d.name for d in paths.epochs.iterdir() if d.is_dir())


def _resolve_epoch_id(paths: WorkspacePaths, epoch_id: str | None) -> str | None:
    """Validate + resolve the epoch a scoped build should read.

    ``None`` resolves to the current epoch (the unchanged default — every
    existing caller). A given id is validated against the on-disk epoch set
    and rejected (``ValueError``) when unknown or path-unsafe, so a
    ``?epoch=../foo`` cannot escape the workspace. The validated id is
    returned verbatim.
    """
    if epoch_id is None:
        return read_current_epoch(paths)
    # reject path-traversal / separators outright — an epoch id is a single
    # directory name, never a path.
    if (
        not isinstance(epoch_id, str)
        or not epoch_id
        or "/" in epoch_id
        or "\\" in epoch_id
        or epoch_id in (".", "..")
        or "\x00" in epoch_id
    ):
        raise ValueError(f"invalid epoch id: {epoch_id!r}")
    if epoch_id not in list_epoch_ids(paths):
        raise ValueError(f"unknown epoch id: {epoch_id!r}")
    return epoch_id


# ---------------------------------------------------------------------------
# Runtime state — heartbeat / lock / active runs / active tournament
# ---------------------------------------------------------------------------


def read_heartbeat_dict(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The heartbeat as a plain dict, or ``None`` when absent."""
    try:
        hb = read_heartbeat(paths.root)
    except Exception:
        return None
    return hb.to_dict() if hb is not None else None


def read_lock_dict(paths: WorkspacePaths) -> dict[str, Any] | None:
    return _read_json_value(paths.lock)


# Canonical per-entry lifecycle vocabulary the dashboard renders. The
# orchestrator writes ``ActiveTournamentEntry.status`` as one of
# ``queued`` / ``running`` / ``completed`` / ``aborted`` (see
# :class:`zicato.runtime.state.ActiveTournamentEntry`), and older /
# adjacent producers have used ``complete`` / ``done`` / ``in_progress``
# / ``active`` / ``error`` / ``fail``. The dashboard renders exactly four
# buckets, so a finished run written as ``completed`` must NOT fall
# through a ``status === 'done'`` comparison and paint as ``queued``.
# Normalising here — at the single read site every endpoint funnels
# through — means the queued mislabel cannot recur regardless of which
# producer spelling lands on disk.
_ENTRY_STATUS_CANONICAL = {
    "queued": "queued",
    "pending": "queued",
    "running": "running",
    "in_progress": "running",
    "active": "running",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    # Fast-mode champion rows: the run was not executed this round —
    # the cached aggregate's per-entry scalar is reused. The dashboard
    # buckets it with ``done`` (it is a settled side with a known
    # scalar) but the producer's ``cached`` spelling is preserved on
    # ``status_raw`` so the renderer can surface a distinct label.
    "cached": "done",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "aborted": "failed",
}


def normalize_entry_status(raw: Any) -> str:
    """Map any producer's entry-status spelling to a canonical bucket.

    Returns one of ``queued`` / ``running`` / ``done`` / ``failed``. An
    unknown or absent value degrades to ``queued`` (the safe pre-start
    default), never raising.
    """
    if not isinstance(raw, str):
        return "queued"
    return _ENTRY_STATUS_CANONICAL.get(raw.strip().lower(), "queued")


def _normalize_tournament_statuses(tournament: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of an active-tournament dict with canonical statuses.

    Each entry gains a ``status`` rewritten to the canonical bucket and a
    ``status_raw`` preserving exactly what the producer wrote (so a
    post-mortem can still see ``aborted`` vs ``error``). Each entry is
    also stamped with an explicit ``generation_id`` — derived from the
    tournament's ``parent_generation_id`` / ``child_generation_id`` by
    the entry's ``side`` — so consumers (the matchup-conversations
    fetcher) can look up the run's events directly per-entry. For a
    fast-mode champion row this is the *cached* source generation
    (``status_raw == "cached"``); v1's events on disk live under that
    same generation directory, NOT under the current round's challenger
    generation, so a per-entry generation_id is the right lookup key.
    The tournament's own ``phase`` is left untouched — it is a separate
    vocabulary.
    """
    if not isinstance(tournament, dict):
        return tournament
    entries = tournament.get("entries")
    if not isinstance(entries, list):
        return tournament
    out = dict(tournament)
    parent_gen = tournament.get("parent_generation_id")
    child_gen = tournament.get("child_generation_id")
    new_entries: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            new_entries.append(entry)
            continue
        e = dict(entry)
        raw = e.get("status")
        e["status_raw"] = raw
        e["status"] = normalize_entry_status(raw)
        # Stamp an explicit per-entry generation_id so the matchup-
        # conversations fetcher can resolve to the correct events.jsonl
        # without having to guess from the tournament-level fields. A
        # producer that already wrote a non-empty generation_id is
        # respected (covers a hypothetical multi-source future where the
        # cached side's gen differs from the tournament's parent_id).
        existing_gen = e.get("generation_id")
        if not isinstance(existing_gen, str) or not existing_gen:
            side = e.get("side")
            if side == "parent" and isinstance(parent_gen, str) and parent_gen:
                e["generation_id"] = parent_gen
            elif side == "child" and isinstance(child_gen, str) and child_gen:
                e["generation_id"] = child_gen
            elif isinstance(side, str) and side:
                # Non-gauntlet structures widen ``side`` to an OPAQUE
                # competitor key — the competitor's generation_id itself
                # (TOURNAMENT-DATA-MODEL.md §2.3). For those rows ``side``
                # is the right events-lookup key, so pass it through as the
                # generation_id rather than dropping the row. A gauntlet
                # row ("parent"/"child") is already handled above.
                e["generation_id"] = side
        new_entries.append(e)
    out["entries"] = new_entries
    return out


def read_active_tournament_dict(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The active tournament as a plain dict, or ``None`` when absent.

    Per-entry ``status`` values are normalized to the canonical
    ``queued`` / ``running`` / ``done`` / ``failed`` vocabulary so a run
    the orchestrator finished (written as ``completed``) never renders as
    ``queued`` in the dashboard. The producer's exact spelling is kept on
    each entry as ``status_raw``.
    """
    try:
        t = read_active_tournament(paths.root)
    except Exception:
        # Fall back to the raw file so a shape the typed reader rejects
        # still surfaces rather than vanishing.
        return _normalize_tournament_statuses(_read_json_value(paths.active_tournament))
    return _normalize_tournament_statuses(t.to_dict()) if t is not None else None


def _parse_iso(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt


def _compute_run_progress(
    run: dict[str, Any], now: _dt.datetime
) -> tuple[float | None, int | None, int | None]:
    """Elapsed / budget / clamped-fraction triple for one run.

    Mirrors the Rust ``reader::compute_run_progress``: any missing input
    degrades that field to ``None`` rather than guessing.
    """
    started = _parse_iso(run.get("started_at"))
    if started is None:
        return (None, None, None)
    elapsed = max(0, int((now - started).total_seconds()))
    deadline = _parse_iso(run.get("deadline"))
    if deadline is None:
        return (None, elapsed, None)
    budget = int((deadline - started).total_seconds())
    if budget <= 0:
        return (1.0, elapsed, max(0, budget))
    fraction = min(1.0, max(0.0, elapsed / budget))
    return (fraction, elapsed, budget)


def read_adk_session_id_from_events(events_jsonl_path: str | None) -> str:
    """Best-effort read of the ADK session id from the first event line.

    goldfive carries ``sessionId`` (camelCase) on every event envelope.
    Reading just the first line is cheap — the session id is the same
    on every event, so one line is sufficient. Returns ``""`` on any
    failure so the caller degrades gracefully.

    .. warning::
        Do NOT call this from :func:`read_active_runs_view` or any
        function that runs in the SSE hot path (``build_snapshot`` →
        ``read_active_runs_view``).  Opening ``events.jsonl`` inside the
        SSE handler triggers the filesystem watchdog and emits a spurious
        ``run_log`` event before the expected ``state_change``, breaking
        SSE ordering tests.  Use this utility only from non-hot-path
        callers (e.g. a dedicated API endpoint, or the post-run reducer).
        The per-run ``adk_session_id`` is persisted in ``loss.json`` by
        the reducer and surfaced through ``build_matchup_detail`` via the
        ``ab_grid`` cells — that is the preferred read path for completed
        runs.
    """
    if not events_jsonl_path:
        return ""
    try:
        p = Path(events_jsonl_path)
        if not p.exists():
            return ""
        with open(p, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                evt = json.loads(raw)
                if not isinstance(evt, dict):
                    continue
                sid = evt.get("sessionId") or evt.get("session_id") or ""
                return str(sid) if sid else ""
    except Exception:  # noqa: BLE001 — best-effort
        return ""
    return ""


def read_active_runs_view(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """``active_runs/*.json`` enriched with computed deadline progress.

    Each row inlines every on-disk ``ActiveRun`` field and adds
    ``progress`` (deadline fraction), ``elapsed_seconds`` and
    ``budget_seconds`` — exactly what ``/api/active-runs`` returns from
    the Rust ``read_active_runs_view``.

    ``adk_session_id`` is intentionally NOT read here: opening
    ``events.jsonl`` files in this hot path (called from
    ``build_snapshot`` on every SSE connection) triggers the filesystem
    watchdog and emits a spurious ``run_log`` event, breaking SSE
    ordering invariants.  For completed runs the ``adk_session_id`` is
    available via ``build_matchup_detail`` → ``ab_grid`` cells (the
    reducer persists it in ``loss.json``).
    """
    now = _utc_now()
    out: list[dict[str, Any]] = []
    try:
        runs = list_active_runs(paths.root)
    except Exception:
        runs = []
    for run in runs:
        d = run.to_dict()
        progress, elapsed, budget = _compute_run_progress(d, now)
        d["progress"] = progress
        d["elapsed_seconds"] = elapsed
        d["budget_seconds"] = budget
        out.append(d)
    return out


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
    if paths.epochs.is_dir():
        for epoch_dir in sorted(paths.epochs.iterdir()):
            if not epoch_dir.is_dir():
                continue
            epoch_id = epoch_dir.name
            gens_dir = epoch_dir / "generations"
            if not gens_dir.is_dir():
                continue
            for gen_dir in sorted(gens_dir.iterdir()):
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

                generations.append(
                    {
                        "generation_id": generation_id,
                        "epoch_id": epoch_id,
                        "parent_generation_id": parent if isinstance(parent, str) else None,
                        "promoted": promoted,
                        "created_at": created_at,
                    }
                )

    generations.sort(key=lambda g: (g["epoch_id"], g["generation_id"]))
    return {"generations": generations}


# ---------------------------------------------------------------------------
# Epoch view
# ---------------------------------------------------------------------------


def _board_input_preview(entry: dict[str, Any]) -> str | None:
    text = entry.get("input")
    if isinstance(text, str):
        return _preview(text)
    turns = entry.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if isinstance(turn, str):
                return _preview(turn)
            if isinstance(turn, dict):
                for key in ("input", "text", "content"):
                    val = turn.get(key)
                    if isinstance(val, str):
                        return _preview(val)
    persona = entry.get("persona")
    if isinstance(persona, dict):
        goal = persona.get("goal")
        if isinstance(goal, str):
            return _preview(goal)
    goal = entry.get("goal")
    if isinstance(goal, str):
        return _preview(goal)
    return None


def _parse_board(path: Path) -> list[dict[str, Any]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # The board's first JSONL line is a `board_meta` header object
        # (it carries `disable_drift`, not an entry's fields). Skip it
        # so it does not surface as a spurious all-`—` board row.
        if obj.get("board_meta") is True:
            continue
        expectation = obj.get("expectation")
        expectation_kind = expectation.get("kind") if isinstance(expectation, dict) else None
        budget = obj.get("wall_clock_budget_seconds")
        if budget is None:
            budget = obj.get("budget_s")
        tags = obj.get("tags")
        tags_list = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        entries.append(
            {
                "id": obj.get("id"),
                "kind": obj.get("kind"),
                "input_preview": _board_input_preview(obj),
                "expectation_kind": expectation_kind if isinstance(expectation_kind, str) else None,
                "budget_s": float(budget) if isinstance(budget, int | float) else None,
                "weight": float(obj["weight"])
                if isinstance(obj.get("weight"), int | float)
                else None,
                "tags": tags_list,
            }
        )
    return entries


def _parse_mutations(path: Path) -> list[dict[str, Any]] | None:
    value = _read_json_value(path)
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for m in value:
        if not isinstance(m, dict):
            continue
        start = m.get("line_start")
        end = m.get("line_end")
        start_i = int(start) if isinstance(start, int | float) else None
        end_i = int(end) if isinstance(end, int | float) else None
        if start_i is not None and end_i is not None:
            lines = str(start_i) if start_i == end_i else f"{start_i}-{end_i}"
        elif start_i is not None:
            lines = str(start_i)
        elif end_i is not None:
            lines = str(end_i)
        else:
            lines = None
        content = m.get("content")
        out.append(
            {
                "id": m.get("id"),
                "kind": m.get("kind"),
                "file": m.get("file"),
                "lines": lines,
                "preview": _preview(content) if isinstance(content, str) else None,
            }
        )
    return out


def _read_harness(paths: WorkspacePaths) -> dict[str, Any] | None:
    cfg = _read_json_value(paths.root / "config.json")
    if not isinstance(cfg, dict):
        return None
    adapter = cfg.get("adapter")
    adapter = adapter if isinstance(adapter, dict) else {}
    entrypoint = adapter.get("entrypoint") or cfg.get("adk_entrypoint") or cfg.get("entrypoint")
    trees = adapter.get("mutable_trees")
    if trees is None:
        trees = cfg.get("mutable_trees")
    mutable_trees = [t for t in trees if isinstance(t, str)] if isinstance(trees, list) else []
    return {
        "entrypoint": entrypoint if isinstance(entrypoint, str) else None,
        "mutable_trees": mutable_trees,
    }


def _read_text_best_effort(path: Path) -> str:
    """Best-effort UTF-8 text read; any error -> empty string."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _read_epoch_brief(epoch_dir: Path) -> str:
    """The proposer brief text for an epoch.

    ``brief.md`` post-rename; ``rubric.md`` is the legacy name and is
    read as a fallback so pre-rename epochs still resolve. Any read
    error degrades to an empty string.
    """
    for name in ("brief.md", "rubric.md"):
        try:
            return (epoch_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            break
    return ""


def _distill_brief_goal(brief: str) -> str | None:
    """Distil a one-line goal summary from a proposer brief.

    The brief carries a ``## Goal`` section (see the dogfood targets'
    ``brief.md``). The summary is the first non-empty prose line of that
    section — the operator's one-line statement of what the epoch is
    reaching for. A list-item or sub-heading line is skipped so the
    summary is always a sentence. Returns ``None`` when the brief has no
    ``Goal`` section or no prose line within it.
    """
    if not brief:
        return None
    lines = brief.replace("\r\n", "\n").split("\n")
    in_goal = False
    for raw in lines:
        line = raw.strip()
        heading = line.lstrip("#").strip() if line.startswith("#") else None
        if heading is not None:
            if in_goal:
                # A later heading closes the Goal section.
                break
            if heading.lower() == "goal":
                in_goal = True
            continue
        if not in_goal or not line:
            continue
        # Skip list items / blockquotes — the summary should read as a
        # sentence, not a bullet fragment.
        if line[0] in "-*>":
            continue
        return _preview(line)
    return None


def build_epochs_summary(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """One row per epoch on disk: ``{epoch_id, goal}``.

    ``goal`` is a one-line summary distilled from that epoch's proposer
    brief (its ``## Goal`` section), or ``None`` when the brief is
    absent or carries no goal. Epochs are listed in directory-name order
    so the Overview's epochs table can annotate each row with what the
    epoch is trying to accomplish without a per-epoch ``/api/epoch``
    fetch.
    """
    out: list[dict[str, Any]] = []
    if not paths.epochs.is_dir():
        return out
    for epoch_dir in sorted(paths.epochs.iterdir()):
        if not epoch_dir.is_dir():
            continue
        goal = _distill_brief_goal(_read_epoch_brief(epoch_dir))
        out.append({"epoch_id": epoch_dir.name, "goal": goal})
    return out


def _read_epoch_experiments(epoch_dir: Path) -> list[dict[str, Any]]:
    """Walk ``generations/*/experiment.json`` for the epoch.

    Returns a list of experiment records, one per generation that has an
    ``experiment.json``, sorted by generation id. Each record carries the
    raw ``experiment.json`` fields plus a ``patch_content`` mapping from
    mutation id to the raw patch dict (from ``patches/*.json``) so the
    frontend can render diffs without a second round-trip.
    """
    gens_dir = epoch_dir / "generations"
    if not gens_dir.is_dir():
        return []
    experiments: list[dict[str, Any]] = []
    for gen_dir in sorted(gens_dir.iterdir()):
        if not gen_dir.is_dir():
            continue
        exp = _read_json_value(gen_dir / "experiment.json")
        if not isinstance(exp, dict):
            continue
        # Collect patches keyed by mutation_id so the render layer can
        # display the diff alongside the hypothesis.
        patches: dict[str, Any] = {}
        patches_dir = gen_dir / "patches"
        if patches_dir.is_dir():
            for patch_file in sorted(patches_dir.iterdir()):
                if patch_file.suffix != ".json":
                    continue
                patch = _read_json_value(patch_file)
                if not isinstance(patch, dict):
                    continue
                mutation_id = patch.get("mutation_id")
                if isinstance(mutation_id, str) and mutation_id:
                    patches[mutation_id] = patch
        record = dict(exp)
        # Always stamp generation_id from the directory name so the
        # frontend can key on it even when the JSON omits it.
        record["generation_id"] = gen_dir.name
        record["patches"] = patches
        experiments.append(record)
    return experiments


def compute_epoch_delta_summary(
    experiments: list[dict[str, Any]],
) -> dict[str, float | None]:
    """Aggregate per-experiment ``scalar_score_delta`` for the Epoch view.

    Two numbers fall out of one walk over the per-generation experiment
    records:

    * ``champion_spine`` — the sum of ``scalar_score_delta`` across the
      promoted lineage only, i.e. the meta-loop's actual progress.
      Computed by walking the parent → child chain through promoted
      generations (the same shape :func:`_champion_lineage` and the
      analyzer's ``_promoted_lineage`` build). ``None`` when the spine
      has fewer than two promoted generations — a single promotion is
      the default first-tournament outcome and does not yet read as
      meta-loop progress, so the caller renders it as a "—" tile.
    * ``gross`` — the sum across **every** experiment that carries a
      finite delta, promoted or not. This is the historical "net" tile
      and is kept as a secondary signal. ``None`` when no experiment
      carries a finite delta.

    Both fields are best-effort: a malformed entry (non-dict outcome,
    non-numeric delta, missing ids) is silently skipped, never raised.
    The meta-loop's progress is the spine sum; ``gross`` includes
    rejected experiments and is therefore the wrong headline for
    framing whether the epoch is moving the loss in the right direction.
    """
    # Per-generation deltas + a parent → child map confined to promoted
    # generations. We use the experiment record's `parent_generation_id`
    # for the edge so the walk does not depend on the SQLite index being
    # rebuilt (the analyzer's `_promoted_lineage` reads the same field).
    by_gen: dict[str, dict[str, Any]] = {}
    promoted_set: set[str] = set()
    gross_total = 0.0
    gross_have = False
    for exp in experiments:
        if not isinstance(exp, dict):
            continue
        gid = exp.get("generation_id")
        if not isinstance(gid, str) or not gid:
            continue
        by_gen[gid] = exp
        outcome = exp.get("outcome")
        if isinstance(outcome, dict):
            ds = outcome.get("scalar_score_delta")
            if isinstance(ds, int | float) and _is_finite(ds):
                gross_total += float(ds)
                gross_have = True
            decision = _experiment_decision(exp)
            if decision is not None and decision.strip().lower() in _PROMOTED_DECISIONS:
                promoted_set.add(gid)

    # Edges among promoted generations only. A promoted child whose
    # parent is *not* promoted (or is missing) is a spine root.
    child_of: dict[str, str] = {}
    roots: list[str] = []
    for gid in promoted_set:
        exp = by_gen[gid]
        parent = exp.get("parent_generation_id")
        if isinstance(parent, str) and parent in promoted_set:
            # First-wins so a duplicated edge does not push later
            # promotions off the chain.
            child_of.setdefault(parent, gid)
        else:
            roots.append(gid)

    # Walk one spine. When the workspace records multiple promotion
    # roots (e.g. a re-seeded epoch), the sorted-first id is the spine
    # we report on — matching :func:`_champion_lineage`. The total is
    # the sum of `scalar_score_delta` for every promoted hop the spine
    # walks. The tile reads "—" when the spine has zero or one promoted
    # generation: a single promotion is the default first-tournament
    # outcome (parent → first child), not yet meta-loop progress.
    chain: list[str] = []
    if roots:
        chain = [sorted(roots)[0]]
        seen = {chain[0]}
        cur = chain[0]
        while cur in child_of:
            nxt = child_of[cur]
            if nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
    spine_total = 0.0
    if len(chain) >= 2:
        for gid in chain:
            outcome = by_gen[gid].get("outcome")
            if not isinstance(outcome, dict):
                continue
            ds = outcome.get("scalar_score_delta")
            if isinstance(ds, int | float) and _is_finite(ds):
                spine_total += float(ds)

    return {
        "champion_spine": spine_total if len(chain) >= 2 else None,
        "gross": gross_total if gross_have else None,
    }


def _is_finite(value: float) -> bool:
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except TypeError:
        return False


#: The closed enum of tournament structures (TOURNAMENT-DATA-MODEL.md §1.1).
#: A reader uses this only to normalize an unknown token to the gauntlet
#: default — semantics live with the selection agent.
_TOURNAMENT_STRUCTURES: tuple[str, ...] = (
    "gauntlet",
    "single_elim",
    "double_elim",
    "swiss",
    "racing",
)


def _normalize_structure(value: Any) -> str:
    """Map an opaque ``structure`` token to a known one, else ``gauntlet``."""
    if isinstance(value, str) and value in _TOURNAMENT_STRUCTURES:
        return value
    return "gauntlet"


def _tournament_block_from_scoring(scoring: Any) -> dict[str, Any] | None:
    """Extract the ``{structure, params}`` block from a frozen scoring dict.

    Returns ``None`` when ``scoring`` carries no ``tournament`` key (so the
    Epoch view omits the block and the frontend falls back to gauntlet —
    byte-identical to pre-feature reads). When present, an unknown
    structure token degrades to ``"gauntlet"`` and a non-object ``params``
    degrades to ``{}`` (the data model treats per-key validation as the
    selection agent's job, §1.4).
    """
    if not isinstance(scoring, dict):
        return None
    raw = scoring.get("tournament")
    if not isinstance(raw, dict):
        return None
    params = raw.get("params")
    return {
        "structure": _normalize_structure(raw.get("structure")),
        "params": params if isinstance(params, dict) else {},
    }


def build_epoch_view(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """An epoch's full evaluation contract.

    ``epoch_id`` defaults to the CURRENT epoch (unchanged behaviour); given a
    validated id, the view resolves THAT epoch instead — the only true fix for
    viewing a non-current epoch from the dashboard.

    Matches the Rust ``epoch::build_epoch_view`` shape: no current epoch
    yields ``{"epoch_id": null}``; every other component degrades to
    empty / ``null``.

    Extended fields (added for the experiment-log / journal / analysis
    panels in the Epoch view):

    * ``experiments`` — list of per-generation experiment records, each
      carrying hypothesis, outcome, and inline patch content so the
      frontend can render {hypothesis → exact change → outcome} in one
      place without a second fetch.
    * ``delta_scalar_summary`` — ``{champion_spine, gross}`` aggregates
      over the per-experiment ``scalar_score_delta``. The spine number
      is the meta-loop's actual progress (sum across promoted hops);
      the gross number sums every experiment and is the wrong headline
      for framing meta-loop direction. Either field is ``None`` when
      no experiment of the relevant kind carries a finite delta.
    * ``journal`` — ``journal.md`` text (empty string when absent).
    * ``analysis_md`` — ``analysis.md`` text (empty string when absent).
    * ``analysis_html_inline`` — paper-styled HTML fragment for the
      Epoch view's inline Analysis section (empty string when no
      report yet). Same renderer as the standalone ``analysis.html``
      so both surfaces read as a paper.
    * ``analysis_html_available`` — ``True`` when ``analysis.html``
      exists on disk; the frontend can link directly to
      ``/api/epoch/{id}/analysis.html``.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    if epoch_id is None:
        return {"epoch_id": None}

    epoch_dir = paths.epochs / epoch_id
    view: dict[str, Any] = {"epoch_id": epoch_id}

    cfg = _read_json_value(epoch_dir / "config.json")
    if isinstance(cfg, dict):
        if isinstance(cfg.get("contract_hash"), str):
            view["contract_hash"] = cfg["contract_hash"]
        if isinstance(cfg.get("created_at"), str):
            view["created_at"] = cfg["created_at"]
        if isinstance(cfg.get("closed"), bool):
            view["closed"] = cfg["closed"]

    harness = _read_harness(paths)
    if harness is not None:
        view["harness"] = harness

    board = _parse_board(epoch_dir / "board.jsonl")
    if board is not None:
        view["board"] = board

    # Proposer brief: ``brief.md`` post-rename; ``rubric.md`` is the
    # legacy name (read as a fallback). Any read error -> empty string.
    view["brief"] = _read_epoch_brief(epoch_dir)

    scoring = _read_json_value(epoch_dir / "scoring.json")
    if scoring is not None:
        view["scoring"] = scoring

    # Tournament structure block (TOURNAMENT-DATA-MODEL.md §3.1). Echo the
    # epoch's resolved ``{structure, params}`` from the frozen
    # ``scoring.json`` so the Epoch view can name the structure without a
    # second fetch. Absent ⇒ default to gauntlet (the frontend's default),
    # so an epoch that predates the feature still reports a coherent
    # structure rather than omitting the block.
    tournament_block = _tournament_block_from_scoring(scoring)
    if tournament_block is not None:
        view["tournament"] = tournament_block
    # else: omit — the frontend defaults to gauntlet (§3.1). Keeping the
    # block absent for a scoring.json that predates the feature preserves
    # byte-identical reads for every gauntlet epoch on disk today.

    # mutations.json is optional; absent -> empty list (never null).
    mutations = _parse_mutations(epoch_dir / "mutations.json")
    view["mutations"] = mutations if mutations is not None else []

    # Experiment log: per-generation hypothesis + outcome + patch content.
    view["experiments"] = _read_epoch_experiments(epoch_dir)

    # Δscalar aggregates — the Epoch header's headline number. The
    # champion-spine sum frames meta-loop progress (promoted hops only);
    # the gross sum across *every* experiment is kept as a secondary
    # signal but is the wrong number to lead with (it includes rejected
    # challengers, which never enter the lineage).
    view["delta_scalar_summary"] = compute_epoch_delta_summary(view["experiments"])

    # Journal: epoch-level markdown log of hypothesis+outcome rounds.
    view["journal"] = _read_text_best_effort(epoch_dir / "journal.md")

    # Frozen goal — Task #178's first-class field on EpochConfig and
    # the index ``epochs.goal`` column. The index is best-effort; on a
    # never-indexed workspace fall back to the goal recorded in
    # ``config.json`` (the canonical durable copy). Brief-distilled
    # fallback is preserved for legacy epochs whose ``config.json``
    # predates the field.
    goal_text = ""
    if isinstance(cfg, dict):
        raw_goal = cfg.get("goal")
        if isinstance(raw_goal, str):
            goal_text = raw_goal.strip()
    if not goal_text:
        try:
            from zicato.index.query import all_epochs as _all_epochs  # noqa: PLC0415

            for row in _all_epochs(paths.index_db):
                if (
                    row["epoch_id"] == epoch_id
                    and isinstance(row.keys(), object)
                    and "goal" in row.keys()
                ):
                    raw = row["goal"]
                    if isinstance(raw, str):
                        goal_text = raw.strip()
                    break
        except Exception:  # noqa: BLE001 — best-effort
            goal_text = ""
    if not goal_text:
        # Last resort: distill from the brief's ``## Goal`` heading.
        distilled = _distill_brief_goal(view.get("brief") or "")
        if distilled:
            goal_text = distilled
    view["goal"] = goal_text

    # Analysis: the post-epoch analysis report.
    analysis_md = _read_text_best_effort(epoch_dir / "analysis.md")
    view["analysis_md"] = analysis_md
    view["analysis_html_available"] = (epoch_dir / "analysis.html").is_file()
    # Inline paper-styled HTML fragment so the Epoch view's Analysis
    # section reads as a paper inline; best-effort — empty string if
    # render fails or the analysis is not yet written.
    view["analysis_html_inline"] = ""
    if analysis_md.strip():
        try:
            from zicato.analyzer.report import render_report_html_fragment
            from zicato.analyzer.report_data import gather_epoch_report_data

            data = gather_epoch_report_data(paths.root, epoch_id)
            view["analysis_html_inline"] = render_report_html_fragment(
                epoch_id, analysis_md, data=data
            )
        except Exception:  # noqa: BLE001 — best-effort
            view["analysis_html_inline"] = ""

    return view


def read_epoch_analysis_html(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """Return the raw HTML of the analysis report, or ``None`` when absent.

    Used by the ``GET /api/epoch/{id}/analysis.html`` endpoint so the
    dashboard can embed or link the self-contained analysis report.
    """
    path = paths.epochs / epoch_id / "analysis.html"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# Run-log tail
# ---------------------------------------------------------------------------


def clamp_run_log_limit(requested: int | None) -> int:
    if requested is None or requested <= 0:
        return RUN_LOG_DEFAULT_LIMIT
    return min(requested, RUN_LOG_MAX_LIMIT)


def _str_either(obj: dict[str, Any], camel: str, snake: str) -> str | None:
    val = obj.get(camel)
    if val is None:
        val = obj.get(snake)
    return val if isinstance(val, str) else None


def _extract_seq(obj: dict[str, Any]) -> int | None:
    val = obj.get("sequence")
    if val is None:
        val = obj.get("seq")
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return None
    return None


def _extract_ts(obj: dict[str, Any]) -> str | None:
    raw = obj.get("emittedAt")
    if raw is None:
        raw = obj.get("emitted_at")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        secs = raw.get("seconds")
        nanos = raw.get("nanos", 0)
        if not isinstance(secs, int | float | str):
            return None
        if not isinstance(nanos, int | float | str):
            nanos = 0
        try:
            secs_i = int(secs)
            nanos_i = int(nanos)
        except (TypeError, ValueError):
            return None
        try:
            dt = _dt.datetime.fromtimestamp(secs_i + nanos_i / 1_000_000_000, _dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.isoformat().replace("+00:00", "Z")
    return None


def _kind_and_payload(obj: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    kind = obj.get("kind")
    if isinstance(kind, str):
        payload = obj.get("payload")
        return to_snake(kind), payload if isinstance(payload, dict) else None
    for key, val in obj.items():
        if key in _ENVELOPE_KEYS:
            continue
        return to_snake(key), val if isinstance(val, dict) else None
    return "unknown", None


def _summarize(kind: str, payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return kind

    def get(camel: str, snake: str) -> str | None:
        return _str_either(payload, camel, snake)

    detail: str | None = None
    if kind == "run_started":
        detail = get("goalSummary", "goal_summary")
    elif kind == "conversation_started":
        detail = get("conversationId", "conversation_id")
    elif kind == "goal_derived":
        goals = payload.get("goals")
        if isinstance(goals, list) and goals and isinstance(goals[0], dict):
            s = goals[0].get("summary")
            detail = s if isinstance(s, str) else None
    elif kind == "drift_detected":
        agent = get("currentAgentId", "current_agent_id")
        what = get("detail", "detail")
        detail = f"{agent}: {what}" if agent and what else (agent or what)
    elif kind == "steering_decision_made":
        agent = get("agentName", "agent_name")
        outcome = get("outcome", "outcome")
        detail = f"{agent}: {outcome}" if agent and outcome else (agent or outcome)
    elif kind == "reasoning_judge_invoked":
        detail = get("classification", "classification") or get("reason", "reason")
    elif kind == "task_progress":
        task = get("taskId", "task_id")
        frac = payload.get("fraction")
        if task and isinstance(frac, int | float):
            detail = f"{task} ({frac * 100:.0f}%)"
        elif task:
            detail = task
        elif isinstance(frac, int | float):
            detail = f"{frac * 100:.0f}%"
    elif kind in ("task_started", "task_completed"):
        detail = get("detail", "detail") or get("summary", "summary") or get("taskId", "task_id")
    elif kind == "task_transitioned":
        task = get("taskId", "task_id")
        to = get("toStatus", "to_status")
        detail = f"{task} -> {to}" if task and to else (task or to)
    elif kind == "delegation_observed":
        frm = get("fromAgent", "from_agent")
        to = get("toAgent", "to_agent")
        detail = f"{frm} -> {to}" if frm and to else None
    elif kind in (
        "agent_invocation_started",
        "agent_invocation_completed",
        "invocation_boundary_entered",
        "invocation_boundary_exited",
    ):
        detail = get("agentName", "agent_name")
    elif kind in ("goldfive_llm_call_start", "goldfive_llm_call_end"):
        detail = get("name", "name")
    elif kind == "pin_resolved":
        detail = get("agentName", "agent_name") or get("taskId", "task_id")

    if not detail:
        detail = (
            get("agentName", "agent_name")
            or get("detail", "detail")
            or get("summary", "summary")
            or get("reason", "reason")
            or get("taskId", "task_id")
        )

    if detail and detail.strip():
        detail = detail.strip()
        if len(detail) > 100:
            return f"{kind}: {detail[:100]}..."
        return f"{kind}: {detail}"
    return kind


def _parse_log_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    kind, payload = _kind_and_payload(obj)
    return {
        "seq": _extract_seq(obj),
        "kind": kind,
        "ts": _extract_ts(obj),
        "summary": _summarize(kind, payload),
    }


def _tail_events(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    records = [r for r in (_parse_log_line(ln) for ln in text.splitlines()) if r]
    if len(records) > limit:
        records = records[len(records) - limit :]
    return records


def _newest_active_run_events(paths: WorkspacePaths) -> Path | None:
    runs_dir = paths.active_runs_dir
    if not runs_dir.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for entry in runs_dir.iterdir():
        if entry.suffix != ".json":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0.0
        if newest is None or mtime > newest[0]:
            newest = (mtime, entry)
    if newest is None:
        return None
    run = _read_json_value(newest[1])
    if not isinstance(run, dict):
        return None
    events_path = run.get("events_jsonl_path")
    return Path(events_path) if isinstance(events_path, str) and events_path else None


def _newest_epoch_events(paths: WorkspacePaths) -> Path | None:
    if not paths.epochs.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for events in paths.epochs.rglob("events.jsonl"):
        try:
            mtime = events.stat().st_mtime
        except OSError:
            mtime = 0.0
        if newest is None or mtime > newest[0]:
            newest = (mtime, events)
    return newest[1] if newest is not None else None


def locate_events_file(paths: WorkspacePaths) -> Path | None:
    """The ``events.jsonl`` the run-log tails — newest active run first."""
    candidate = _newest_active_run_events(paths)
    if candidate is not None and candidate.exists():
        return candidate
    return _newest_epoch_events(paths)


def _event_cursor(event: dict[str, Any], fallback_index: int) -> int:
    """A monotone cursor for one run-log event.

    Prefers the event's own ``sequence`` / ``seq``; an event with no
    sequence falls back to its line index so the run-log tail can still
    advance append-only against a producer that omits sequence numbers.
    """
    seq = _extract_seq(event)
    return seq if seq is not None else fallback_index


def build_run_log(paths: WorkspacePaths, limit: int, after: int | None = None) -> dict[str, Any]:
    """``GET /api/run-log`` body — run-log events plus an append cursor.

    Returns ``{"events": [...], "cursor": <int|None>, "events_path": str}``.

    * With ``after`` ``None`` the body is the last ``limit`` parseable
      events (the initial paint).
    * With ``after`` set, only events whose cursor is strictly greater
      than ``after`` are returned — the dashboard appends these to its
      log tail rather than re-rendering the whole list, which is what
      stops the visible flashing.

    ``cursor`` is the largest cursor in the file (``None`` when empty);
    the client passes it back as the next ``after``.
    """
    path = locate_events_file(paths)
    all_events = _tail_events(path, RUN_LOG_MAX_LIMIT) if path is not None else []
    cursor: int | None = None
    if all_events:
        cursor = max(_event_cursor(ev, i) for i, ev in enumerate(all_events))

    if after is None:
        events = all_events[-limit:] if len(all_events) > limit else all_events
    else:
        events = [ev for i, ev in enumerate(all_events) if _event_cursor(ev, i) > after]
        if len(events) > limit:
            events = events[-limit:]

    return {
        "events": events,
        "cursor": cursor,
        "events_path": str(path) if path is not None else None,
    }


# ---------------------------------------------------------------------------
# Composite /api/state snapshot
# ---------------------------------------------------------------------------


def build_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """The full ``/api/state`` snapshot, mirroring the Rust ``Snapshot``."""
    return {
        "heartbeat": read_heartbeat_dict(paths),
        "lock": read_lock_dict(paths),
        "active_runs": read_active_runs_view(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "lineage": _read_json_value(paths.lineage),
        "epoch_id": read_current_epoch(paths),
        "epoch": build_epoch_view(paths),
        "generated_at": _iso(_utc_now()),
    }


# ---------------------------------------------------------------------------
# SQLite analytical index — bracket / matchup / health
# ---------------------------------------------------------------------------


class _IndexAbsent(Exception):
    """``index.db`` does not exist on disk."""


def _open_index(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _IndexAbsent
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []


def _opt_json(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _champion_lineage(generations: list[dict[str, Any]]) -> list[str]:
    promoted = {
        g["generation_id"] for g in generations if g.get("promoted") and g.get("generation_id")
    }
    if not promoted:
        return []
    parent = {
        g["generation_id"]: g.get("parent_generation_id")
        for g in generations
        if g.get("promoted") and g.get("generation_id")
    }
    roots = sorted(
        gid for gid in promoted if parent.get(gid) is None or parent.get(gid) not in promoted
    )
    if not roots:
        return []
    root = roots[0]
    child_of: dict[str, str] = {}
    for g in generations:
        if not g.get("promoted"):
            continue
        p = g.get("parent_generation_id")
        c = g.get("generation_id")
        if isinstance(p, str) and isinstance(c, str) and p in promoted:
            child_of[p] = c
    chain = [root]
    seen = {root}
    cur = root
    while cur in child_of:
        nxt = child_of[cur]
        if nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain


def build_bracket(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/tournaments`` — the bracket for an epoch.

    ``epoch_id`` defaults to the current epoch; a validated id scopes to that
    epoch instead.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    try:
        conn = _open_index(paths.index_db)
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "champion_lineage": [],
            "matchups": [],
            "note": "index not built; run zicato reindex",
        }
    except sqlite3.Error:
        return {"epoch_id": epoch_id, "champion_lineage": [], "matchups": []}

    try:
        if epoch_id is None:
            return {"epoch_id": None, "champion_lineage": [], "matchups": []}

        gen_rows = _query(
            conn,
            "SELECT epoch_id, generation_id, parent_generation_id, promoted "
            "FROM generations WHERE epoch_id = ?",
            (epoch_id,),
        )
        generations = [
            {
                "generation_id": r["generation_id"],
                "parent_generation_id": r["parent_generation_id"],
                "promoted": bool(r["promoted"]),
            }
            for r in gen_rows
        ]
        champion_lineage = _champion_lineage(generations)

        # Select the structure-aware columns alongside the legacy
        # per-matchup ones. The v3 columns (structure / *_json) may be
        # absent on an index that predates the migration — the SELECT is
        # split so a missing-column error on the structure columns does
        # not blank out the legacy matchups (back-compat: gauntlet reads
        # must stay intact). ``_query`` swallows the sqlite error and
        # returns [] for the structure-aware query in that case.
        tour_rows = _query(
            conn,
            "SELECT t.tournament_id, t.parent_generation_id, t.child_generation_id, "
            "t.decision, t.delta_scalar, t.rejection_reason, t.ran_at, "
            "e.hypothesis_core_idea "
            "FROM tournaments t "
            "LEFT JOIN experiments e "
            "ON e.epoch_id = t.epoch_id AND e.generation_id = t.child_generation_id "
            "WHERE t.epoch_id = ? "
            "ORDER BY t.ran_at ASC, t.tournament_id ASC",
            (epoch_id,),
        )
        matchups = [
            {
                "champion": r["parent_generation_id"],
                "challenger": r["child_generation_id"],
                "decision": r["decision"],
                "delta_scalar": r["delta_scalar"],
                "rejection_reason": r["rejection_reason"],
                "hypothesis_core_idea": r["hypothesis_core_idea"],
                "ran_at": r["ran_at"],
            }
            for r in tour_rows
        ]

        # Structure-aware envelope (§3.1). Read the v3 columns
        # defensively: if they are absent (pre-migration index) the query
        # returns [] and the structure degenerates to gauntlet — leaving
        # the legacy ``matchups`` / ``champion_lineage`` byte-identical.
        struct_rows = _query(
            conn,
            "SELECT tournament_id, structure, structure_params_json, "
            "competitors_json, rounds_json, standings_json, ran_at "
            "FROM tournaments WHERE epoch_id = ? "
            "ORDER BY ran_at ASC, tournament_id ASC",
            (epoch_id,),
        )
        tournaments: list[dict[str, Any]] = []
        epoch_structure = "gauntlet"
        epoch_structure_params: dict[str, Any] = {}
        for r in struct_rows:
            structure = _normalize_structure(r["structure"])
            params = _opt_json(r["structure_params_json"])
            params = params if isinstance(params, dict) else {}
            # The epoch's structure is the contract-frozen value; every
            # tournament in the epoch shares it, so the last non-gauntlet
            # value wins (they should all agree).
            if structure != "gauntlet":
                epoch_structure = structure
                epoch_structure_params = params
            competitors = _opt_json(r["competitors_json"])
            rounds = _opt_json(r["rounds_json"])
            standings = _opt_json(r["standings_json"])
            tournaments.append(
                {
                    "tournament_id": r["tournament_id"],
                    "structure": structure,
                    "structure_params": params,
                    "competitors": competitors if isinstance(competitors, list) else [],
                    "rounds": rounds if isinstance(rounds, list) else [],
                    "standings": standings if isinstance(standings, list) else [],
                }
            )

        return {
            "epoch_id": epoch_id,
            "structure": epoch_structure,
            "structure_params": epoch_structure_params,
            "champion_lineage": champion_lineage,
            "matchups": matchups,
            "tournaments": tournaments,
        }
    finally:
        conn.close()


def _verdict(parent: float | None, child: float | None) -> str:
    if parent is not None and child is not None:
        if child < parent:
            return "improved"
        if child > parent:
            return "regressed"
    return "flat"


def build_matchup_detail(paths: WorkspacePaths, generation_id: str) -> dict[str, Any]:
    """``GET /api/tournaments/:generation_id`` — full matchup detail."""
    epoch_id = read_current_epoch(paths)
    try:
        conn = _open_index(paths.index_db)
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": None,
            "decision": None,
            "rejection_reason": None,
            "ran_at": None,
            "parent_scalar": None,
            "child_scalar": None,
            "delta_scalar": None,
            "patches": [],
            "ab_grid": [],
            "note": "index not built; run zicato reindex",
        }
    except sqlite3.Error:
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": None,
            "decision": None,
            "rejection_reason": None,
            "ran_at": None,
            "parent_scalar": None,
            "child_scalar": None,
            "delta_scalar": None,
            "patches": [],
            "ab_grid": [],
        }

    try:
        tour = _query(
            conn,
            "SELECT t.tournament_id, t.parent_generation_id, t.child_generation_id, "
            "t.decision, t.parent_scalar, t.child_scalar, t.delta_scalar, "
            "t.rejection_reason, t.ran_at "
            "FROM tournaments t WHERE t.child_generation_id = ? LIMIT 1",
            (generation_id,),
        )
        tour_row = tour[0] if tour else None

        exp = _query(
            conn,
            "SELECT hypothesis_core_idea, hypothesis_why, hypothesis_json, "
            "tournament_decision, rejection_reason, scalar_score_delta, "
            "drift_loss_delta, pass_rate_delta "
            "FROM experiments WHERE generation_id = ? LIMIT 1",
            (generation_id,),
        )
        exp_row = exp[0] if exp else None

        champion = tour_row["parent_generation_id"] if tour_row else None

        child_losses = _query(
            conn,
            "SELECT entry_id, drift_loss, pass_fail, loss_json FROM loss_profiles "
            "WHERE generation_id = ? ORDER BY entry_id ASC",
            (generation_id,),
        )
        parent_losses = (
            _query(
                conn,
                "SELECT entry_id, drift_loss, pass_fail, loss_json FROM loss_profiles "
                "WHERE generation_id = ? ORDER BY entry_id ASC",
                (champion,),
            )
            if champion
            else []
        )
        ab: dict[str, dict[str, Any]] = {}
        for r in parent_losses:
            key = r["entry_id"] or ""
            cell = ab.setdefault(key, {"entry_id": r["entry_id"]})
            cell["entry_id"] = r["entry_id"]
            cell["parent_drift_loss"] = r["drift_loss"]
            cell["parent_pass_fail"] = r["pass_fail"]
            lj = _opt_json(r["loss_json"])
            if isinstance(lj, dict):
                sid = lj.get("adk_session_id")
                if isinstance(sid, str) and sid:
                    cell["parent_adk_session_id"] = sid
        for r in child_losses:
            key = r["entry_id"] or ""
            cell = ab.setdefault(key, {"entry_id": r["entry_id"]})
            cell["entry_id"] = r["entry_id"]
            cell["child_drift_loss"] = r["drift_loss"]
            cell["child_pass_fail"] = r["pass_fail"]
            lj = _opt_json(r["loss_json"])
            if isinstance(lj, dict):
                sid = lj.get("adk_session_id")
                if isinstance(sid, str) and sid:
                    cell["child_adk_session_id"] = sid
        ab_grid = []
        for key in sorted(ab):
            cell = ab[key]
            cell.setdefault("parent_drift_loss", None)
            cell.setdefault("child_drift_loss", None)
            cell.setdefault("parent_pass_fail", None)
            cell.setdefault("child_pass_fail", None)
            cell["verdict"] = _verdict(cell["parent_drift_loss"], cell["child_drift_loss"])
            ab_grid.append(cell)

        patch_rows = _query(
            conn,
            "SELECT patch_id, mutation_id, op, rationale FROM patches "
            "WHERE generation_id = ? ORDER BY patch_id ASC",
            (generation_id,),
        )
        patches = [
            {
                "patch_id": r["patch_id"],
                "mutation_id": r["mutation_id"],
                "op": r["op"],
                "rationale": r["rationale"],
            }
            for r in patch_rows
        ]

        decision = None
        rejection_reason = None
        if tour_row is not None:
            decision = tour_row["decision"]
            rejection_reason = tour_row["rejection_reason"]
        if decision is None and exp_row is not None:
            decision = exp_row["tournament_decision"]
        if rejection_reason is None and exp_row is not None:
            rejection_reason = exp_row["rejection_reason"]

        delta_scalar = tour_row["delta_scalar"] if tour_row else None
        if delta_scalar is None and exp_row is not None:
            delta_scalar = exp_row["scalar_score_delta"]

        detail: dict[str, Any] = {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": champion,
            "decision": decision,
            "rejection_reason": rejection_reason,
            "ran_at": tour_row["ran_at"] if tour_row else None,
            "parent_scalar": tour_row["parent_scalar"] if tour_row else None,
            "child_scalar": tour_row["child_scalar"] if tour_row else None,
            "delta_scalar": delta_scalar,
            "patches": patches,
            "ab_grid": ab_grid,
        }
        if exp_row is not None:
            if exp_row["drift_loss_delta"] is not None:
                detail["drift_loss_delta"] = exp_row["drift_loss_delta"]
            if exp_row["pass_rate_delta"] is not None:
                detail["pass_rate_delta"] = exp_row["pass_rate_delta"]
            detail["hypothesis"] = {
                "core_idea": exp_row["hypothesis_core_idea"],
                "why": exp_row["hypothesis_why"],
            }
            raw = _opt_json(exp_row["hypothesis_json"])
            if raw is not None:
                detail["hypothesis"]["raw"] = raw
        return detail
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-entry A/B grid — read straight off the persisted per-run loss files
# ---------------------------------------------------------------------------
#
# ``build_matchup_detail`` above sources its ``ab_grid`` from the SQLite
# analytical index. That index is a best-effort dual-write: a completed
# tournament whose index was never (re)built — or a workspace inspected
# before ``zicato reindex`` ran — carries no ``loss_profiles`` rows, so
# the matchup-detail panel renders "No per-entry grid recorded" and a
# finished tournament loses its per-board outcomes.
#
# The per-board telemetry is, however, always on disk: every board run
# writes ``generations/{gen}/runs/{entry}/loss.json`` (the reducer's
# :class:`~zicato.core.LossProfile`), and the orchestrator caches a
# ``generations/{gen}/gen_score.json`` aggregate. ``build_matchup_grid``
# reconstructs the champion-vs-challenger comparison directly from those
# files so a completed tournament's outcomes survive without the index.


def _read_run_loss_files(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, dict[str, Any]]:
    """Read every ``runs/{entry}/loss.json`` under one generation.

    Returns ``{entry_id: {drift_loss, pass_fail, adk_session_id, run_id}}``.
    The entry id keys on the run directory name (the canonical board-run
    layout) and is overridden by the ``entry_id`` field inside the
    ``loss.json`` payload when present. Missing / malformed files are
    skipped silently — a generation with no telemetry yet yields ``{}``.
    """
    out: dict[str, dict[str, Any]] = {}
    runs_dir = paths.epochs / epoch_id / "generations" / generation_id / "runs"
    if not runs_dir.is_dir():
        return out
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        loss = _read_json_value(run_dir / "loss.json")
        if not isinstance(loss, dict):
            continue
        entry_id = loss.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            entry_id = run_dir.name
        drift = loss.get("drift_loss")
        cell: dict[str, Any] = {
            "entry_id": entry_id,
            "drift_loss": drift if isinstance(drift, int | float) else None,
            "pass_fail": loss.get("pass_fail"),
            "run_id": loss.get("run_id") if isinstance(loss.get("run_id"), str) else run_dir.name,
        }
        sid = loss.get("adk_session_id")
        if isinstance(sid, str) and sid:
            cell["adk_session_id"] = sid
        out[entry_id] = cell
    return out


def _read_gen_score(paths: WorkspacePaths, epoch_id: str, generation_id: str) -> dict[str, Any]:
    """Read a generation's cached ``gen_score.json`` aggregate.

    Returns the raw aggregate dict (``scalar`` / ``drift_loss_mean`` /
    ``pass_rate`` / ``scalar_components`` / ...), or ``{}`` when the
    file is absent or malformed.
    """
    score = _read_json_value(
        paths.epochs / epoch_id / "generations" / generation_id / "gen_score.json"
    )
    return score if isinstance(score, dict) else {}


def _grid_won_by(
    parent: float | None, child: float | None, champion: str, challenger: str
) -> str | None:
    """Which side won one board entry — lower drift loss wins.

    Returns the challenger id when it beat the champion on this entry,
    the champion id when it lost, ``None`` on a tie or missing data.
    """
    if parent is None or child is None:
        return None
    if child < parent:
        return challenger
    if child > parent:
        return champion
    return None


def build_matchup_grid(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Per-entry A/B grid for a matchup, read from the persisted loss files.

    ``GET /api/matchup-grid/{epoch_id}/{champion}/{challenger}``. Unlike
    :func:`build_matchup_detail` this never touches the SQLite index — it
    reads ``generations/{gen}/runs/{entry}/loss.json`` for both the
    champion and the challenger generation and the two
    ``gen_score.json`` aggregates, so a *completed* tournament's
    per-board outcomes are recoverable even when the index was never
    built.

    Returns::

        {
          "epoch_id", "champion", "challenger",
          "entry_grid": [ { entry_id, parent_drift_loss, child_drift_loss,
                            parent_pass, child_pass, delta, verdict,
                            won_by, parent_session_id?, child_session_id? } ],
          "scalar": { parent, child, delta, components } | null,
          "source": "loss_files"
        }

    ``entry_grid`` rows are sorted by entry id; an entry that only ran on
    one side still appears (the missing side is ``null``). The ``scalar``
    block is composed from the ``gen_score.json`` aggregates — its
    ``components`` is the challenger-minus-champion delta of each
    ``scalar_components`` term so the breakdown shows what moved.
    """
    base: dict[str, Any] = {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "entry_grid": [],
        "scalar": None,
        "source": "loss_files",
    }
    if not epoch_id or not challenger_id:
        return base

    parent_losses = _read_run_loss_files(paths, epoch_id, champion_id) if champion_id else {}
    child_losses = _read_run_loss_files(paths, epoch_id, challenger_id)

    entry_grid: list[dict[str, Any]] = []
    for entry_id in sorted(set(parent_losses) | set(child_losses)):
        p = parent_losses.get(entry_id)
        c = child_losses.get(entry_id)
        parent_drift = p.get("drift_loss") if p else None
        child_drift = c.get("drift_loss") if c else None
        delta = (
            child_drift - parent_drift
            if isinstance(parent_drift, int | float) and isinstance(child_drift, int | float)
            else None
        )
        row: dict[str, Any] = {
            "entry_id": entry_id,
            "parent_drift_loss": parent_drift,
            "child_drift_loss": child_drift,
            "parent_pass": p.get("pass_fail") if p else None,
            "child_pass": c.get("pass_fail") if c else None,
            "delta": delta,
            "verdict": _verdict(parent_drift, child_drift),
            "won_by": _grid_won_by(parent_drift, child_drift, champion_id, challenger_id),
        }
        if p and p.get("adk_session_id"):
            row["parent_session_id"] = p["adk_session_id"]
        if c and c.get("adk_session_id"):
            row["child_session_id"] = c["adk_session_id"]
        entry_grid.append(row)
    base["entry_grid"] = entry_grid

    parent_score = _read_gen_score(paths, epoch_id, champion_id) if champion_id else {}
    child_score = _read_gen_score(paths, epoch_id, challenger_id)
    parent_scalar = parent_score.get("scalar")
    child_scalar = child_score.get("scalar")
    if isinstance(parent_scalar, int | float) or isinstance(child_scalar, int | float):
        p_scalar = parent_scalar if isinstance(parent_scalar, int | float) else None
        c_scalar = child_scalar if isinstance(child_scalar, int | float) else None
        scalar: dict[str, Any] = {
            "parent": p_scalar,
            "child": c_scalar,
            "delta": (
                c_scalar - p_scalar if p_scalar is not None and c_scalar is not None else None
            ),
        }
        parent_components = parent_score.get("scalar_components")
        child_components = child_score.get("scalar_components")
        # The breakdown bars are the per-component CHANGE champion ->
        # challenger: a negative bar is a component that improved.
        components: dict[str, float] = {}
        names: set[str] = set()
        if isinstance(parent_components, dict):
            names |= set(parent_components)
        if isinstance(child_components, dict):
            names |= set(child_components)
        for name in names:
            pv = parent_components.get(name) if isinstance(parent_components, dict) else None
            cv = child_components.get(name) if isinstance(child_components, dict) else None
            pv = pv if isinstance(pv, int | float) else 0.0
            cv = cv if isinstance(cv, int | float) else 0.0
            components[name] = cv - pv
        if components:
            scalar["components"] = components
        base["scalar"] = scalar

    return base


def _empty_tournament_structure(epoch_id: str, tournament_id: str, source: str) -> dict[str, Any]:
    return {
        "epoch_id": epoch_id,
        "tournament_id": tournament_id,
        "structure": "gauntlet",
        "structure_params": {},
        "competitors": [],
        "rounds": [],
        "standings": [],
        "field_status": [],
        "source": source,
    }


def _structure_from_index(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """The settled structure state from the SQLite ``tournaments`` row.

    Returns ``None`` when the index is absent, the row is missing, or the
    v3 structure columns do not exist (pre-migration index) — every such
    case falls through to the next link in the resolution chain.
    """
    try:
        conn = _open_index(paths.index_db)
    except (_IndexAbsent, sqlite3.Error):
        return None
    try:
        rows = _query(
            conn,
            "SELECT structure, structure_params_json, competitors_json, "
            "rounds_json, standings_json FROM tournaments "
            "WHERE epoch_id = ? AND tournament_id = ? LIMIT 1",
            (epoch_id, tournament_id),
        )
        if not rows:
            return None
        r = rows[0]
        params = _opt_json(r["structure_params_json"])
        competitors = _opt_json(r["competitors_json"])
        rounds = _opt_json(r["rounds_json"])
        standings = _opt_json(r["standings_json"])
        # ``field_status_json`` is a v5 column. A real index is migrated to
        # v5 on open, but a hand-built / pre-migration index may lack the
        # column — query it separately and degrade to an empty list rather
        # than letting a missing column fail the whole resolution.
        field_status: Any = None
        try:
            fs_rows = _query(
                conn,
                "SELECT field_status_json FROM tournaments "
                "WHERE epoch_id = ? AND tournament_id = ? LIMIT 1",
                (epoch_id, tournament_id),
            )
            if fs_rows:
                field_status = _opt_json(fs_rows[0]["field_status_json"])
        except sqlite3.Error:
            field_status = None
        # A row that exists but carries no structure internals (a gauntlet
        # row, or a NULL-backfilled pre-feature row) is not a useful
        # structure read; fall through so the active/loss-file links can
        # offer something richer.
        if rounds is None and standings is None and competitors is None:
            return None
        return {
            "epoch_id": epoch_id,
            "tournament_id": tournament_id,
            "structure": _normalize_structure(r["structure"]),
            "structure_params": params if isinstance(params, dict) else {},
            "competitors": competitors if isinstance(competitors, list) else [],
            "rounds": rounds if isinstance(rounds, list) else [],
            "standings": standings if isinstance(standings, list) else [],
            "field_status": field_status if isinstance(field_status, list) else [],
            "source": "index",
        }
    finally:
        conn.close()


def _structure_from_active(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """The structure state from the live ``active_tournament.json``.

    Returns ``None`` unless the live record matches the requested
    ``(epoch_id, tournament_id)`` coordinate.
    """
    active = read_active_tournament_dict(paths)
    if not isinstance(active, dict):
        return None
    if active.get("tournament_id") != tournament_id:
        return None
    if epoch_id and active.get("epoch_id") not in (None, epoch_id):
        return None
    params = active.get("structure_params")
    competitors = active.get("competitors")
    rounds = active.get("rounds")
    standings = active.get("standings")
    field_status = active.get("field_status")
    return {
        "epoch_id": active.get("epoch_id") or epoch_id,
        "tournament_id": tournament_id,
        "structure": _normalize_structure(active.get("structure")),
        "structure_params": params if isinstance(params, dict) else {},
        "competitors": competitors if isinstance(competitors, list) else [],
        "rounds": rounds if isinstance(rounds, list) else [],
        "standings": standings if isinstance(standings, list) else [],
        "field_status": field_status if isinstance(field_status, list) else [],
        "source": "active",
    }


def _structure_from_loss_files(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any] | None:
    """Reconstruct a degenerate single-match view from per-run loss files.

    The last link in the resolution chain (mirrors ``build_matchup_grid``'s
    index-free read). A tournament id encodes its crowning pair as
    ``{epoch}:{champion}->{challenger}`` (the ingester convention); when
    that decodes, render one round / one match between the two sides with
    their settled drift-loss scalars. When it does not decode, return a
    bare envelope so the handler still answers HTTP 200.
    """
    if not epoch_id:
        return None
    champion, challenger = _decode_crowning_pair(tournament_id)
    if not challenger:
        return None
    parent_score = _read_gen_score(paths, epoch_id, champion) if champion else {}
    child_score = _read_gen_score(paths, epoch_id, challenger)
    parent_scalar = parent_score.get("scalar")
    child_scalar = child_score.get("scalar")
    parent_scalar = parent_scalar if isinstance(parent_scalar, int | float) else None
    child_scalar = child_scalar if isinstance(child_scalar, int | float) else None
    delta = (
        child_scalar - parent_scalar
        if parent_scalar is not None and child_scalar is not None
        else None
    )
    competitors: list[dict[str, Any]] = []
    standings: list[dict[str, Any]] = []
    if champion:
        competitors.append({"generation_id": champion, "seed": 1, "role": "champion"})
        standings.append(
            {"generation_id": champion, "rank": 1, "scalar": parent_scalar, "role": "champion"}
        )
    competitors.append({"generation_id": challenger, "seed": 2, "role": "challenger"})
    standings.append(
        {"generation_id": challenger, "rank": 2, "scalar": child_scalar, "role": "challenger"}
    )
    # The challenger applied (it has a settled scalar), so the proposing
    # step is reconstructed as a single applied entry — never an empty
    # idle tracker for a tournament that actually ran.
    field_status: list[dict[str, Any]] = [
        {"generation_id": challenger, "status": "applied", "reason": "", "seed": 2}
    ]
    match: dict[str, Any] = {
        "match_id": "r0_m0",
        "competitors": [c for c in (champion, challenger) if c],
        "winner": "",
        "decision": "",
        "delta_scalar": delta,
        "bracket_slot": "",
        "bye": False,
    }
    return {
        "epoch_id": epoch_id,
        "tournament_id": tournament_id,
        "structure": "gauntlet",
        "structure_params": {},
        "competitors": competitors,
        "rounds": [{"round_index": 0, "label": "Round 1", "matches": [match]}],
        "standings": standings,
        "field_status": field_status,
        "source": "loss_files",
    }


def _decode_crowning_pair(tournament_id: str) -> tuple[str, str]:
    """Best-effort decode of ``{epoch}:{champion}->{challenger}``.

    Returns ``(champion, challenger)``; either may be ``""`` when the id
    does not follow the convention.
    """
    if not isinstance(tournament_id, str) or "->" not in tournament_id:
        return ("", "")
    left, _, challenger = tournament_id.partition("->")
    champion = left.rsplit(":", 1)[-1] if ":" in left else left
    return (champion.strip(), challenger.strip())


def build_tournament_structure(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str
) -> dict[str, Any]:
    """``GET /api/tournament-structure/{epoch_id}/{tournament_id}``.

    The single read the UI uses to render a bracket / standings / racing
    ladder for one tournament (TOURNAMENT-DATA-MODEL.md §3.2). Resolution
    order mirrors ``build_matchup_grid``'s fallback chain:

    1. the SQLite ``tournaments`` row's structure columns (``source:
       "index"``);
    2. the live ``active_tournament.json`` when it matches the coordinate
       (``source: "active"``);
    3. a degenerate single-match reconstruction from the per-run
       ``loss.json`` / ``gen_score.json`` files (``source: "loss_files"``).

    A malformed / unresolvable id degrades to an empty gauntlet structure
    at HTTP 200 (matching every other handler in ``endpoints.py``).
    """
    if not epoch_id or not tournament_id:
        return _empty_tournament_structure(epoch_id, tournament_id, "loss_files")
    for resolver in (_structure_from_index, _structure_from_active, _structure_from_loss_files):
        result = resolver(paths, epoch_id, tournament_id)
        if result is not None:
            return _enrich_field_status(paths, epoch_id, tournament_id, result)
    return _empty_tournament_structure(epoch_id, tournament_id, "loss_files")


def _enrich_field_status(
    paths: WorkspacePaths, epoch_id: str, tournament_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Backfill ``field_status`` from the live envelope when the resolved
    structure lacks it.

    The per-experiment index row carries the settled bracket but not the
    proposing-step outcomes (the per-challenger applied/rejected records
    live only on ``active_tournament.json``, which the multi-challenger
    path retains with ``phase="completed"``). So when the winning resolver
    is the index (or any source whose ``field_status`` is empty) but the
    live envelope still matches this coordinate, lift its ``field_status``
    onto the result so a just-completed epoch's proposing step survives.
    Purely additive — never overwrites a non-empty field-status.
    """
    if result.get("field_status"):
        return result
    active = _structure_from_active(paths, epoch_id, tournament_id)
    if active is not None and active.get("field_status"):
        result["field_status"] = active["field_status"]
    return result


def _latest_round_report(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for entry in directory.iterdir():
        name = entry.name
        if not name.startswith("round_") or not name.endswith(".json"):
            continue
        num = name[len("round_") : -len(".json")]
        try:
            n = int(num)
        except ValueError:
            continue
        if best is None or n > best[0]:
            best = (n, entry)
    return best[1] if best is not None else None


# ---------------------------------------------------------------------------
# Score trajectory — the environment-wide evolution curve
# ---------------------------------------------------------------------------


def _mean_drift_loss_per_generation(
    conn: sqlite3.Connection, epoch_id: str | None, generation_id: str
) -> tuple[float | None, int]:
    """Return ``(mean_drift_loss, entry_count)`` for one generation.

    A generation can appear in more than one tournament — it is
    re-scored whenever it serves as a later round's champion — so the
    index carries several ``loss_profiles`` rows for the same
    ``(generation_id, entry_id)`` pair, and the index does not record a
    usable per-run timestamp to order them by. To stay deterministic
    regardless of row order, the aggregate is computed in two stages:

    1. Per board entry, average that entry's ``drift_loss`` across every
       run of it (so an entry run twice contributes its mean, not a
       row-order-dependent pick).
    2. The generation's scalar is the mean of those per-entry means.

    Aborted runs ARE included: an aborted run carries a real,
    definite worst-case ``drift_loss`` (the runner synthesises one),
    and the tournament gate's scalar aggregates every entry — excluding
    aborted runs would understate the curve and misrepresent the
    evolution the gate actually saw.

    Returns ``(None, 0)`` when the generation has no loss profiles.
    """
    rows = _query(
        conn,
        "SELECT entry_id, drift_loss FROM loss_profiles "
        "WHERE generation_id = ? AND epoch_id = ?",
        (generation_id, epoch_id),
    )
    per_entry: dict[str, list[float]] = {}
    for r in rows:
        if r["drift_loss"] is None:
            continue
        per_entry.setdefault(r["entry_id"], []).append(float(r["drift_loss"]))
    if not per_entry:
        return None, 0
    entry_means = [sum(v) / len(v) for v in per_entry.values()]
    return sum(entry_means) / len(entry_means), len(entry_means)


def build_score_trajectory(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/score-trajectory`` — the scalar across generations.

    ``epoch_id`` defaults to the current epoch; a validated id scopes the
    trajectory to that epoch's generations instead.

    The environment-wide evolution curve: one point per generation, in
    lineage (creation) order, plotting the generation's aggregate
    drift-loss scalar (the dominant term of the tournament scalar — the
    quantity the gate compares, lower is better).

    The per-generation scalar is computed by
    :func:`_mean_drift_loss_per_generation` — a deterministic,
    row-order-independent mean of per-entry mean ``drift_loss`` that
    includes aborted runs (they carry a real worst-case loss the gate
    scalar uses). A generation with no loss profiles yet yields
    ``scalar = None`` — still plotted as a gap rather than dropped, so
    the x-axis stays continuous across the lineage.

    Returns ``{"epoch_id", "points": [{generation_id, parent_generation_id,
    promoted, scalar, entry_count, created_at}], "note"?}``. Degrades to
    an empty ``points`` list (never raises) when the index is absent.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    # Lineage order is authoritative for the x-axis — the index's
    # ``generations`` rows can carry empty ``created_at`` strings.
    lineage = build_lineage_view(paths)
    ordered = [
        g
        for g in lineage.get("generations", [])
        if epoch_id is None or g.get("epoch_id") == epoch_id
    ]

    try:
        conn = _open_index(paths.index_db)
    except _IndexAbsent:
        return {
            "epoch_id": epoch_id,
            "points": [
                {
                    "generation_id": g["generation_id"],
                    "parent_generation_id": g.get("parent_generation_id"),
                    "promoted": g.get("promoted"),
                    "scalar": None,
                    "entry_count": 0,
                    "created_at": g.get("created_at"),
                }
                for g in ordered
            ],
            "note": "index not built; run zicato reindex",
        }
    except sqlite3.Error:
        return {"epoch_id": epoch_id, "points": []}

    try:
        points: list[dict[str, Any]] = []
        for g in ordered:
            gid = g["generation_id"]
            scalar, entry_count = _mean_drift_loss_per_generation(conn, g.get("epoch_id"), gid)
            points.append(
                {
                    "generation_id": gid,
                    "parent_generation_id": g.get("parent_generation_id"),
                    "promoted": g.get("promoted"),
                    "scalar": scalar,
                    "entry_count": entry_count,
                    "created_at": g.get("created_at"),
                }
            )
        return {"epoch_id": epoch_id, "points": points}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Drift-kind movements — champion -> challenger per-kind count deltas
# ---------------------------------------------------------------------------


def _drift_counts_for_generation(
    conn: sqlite3.Connection, epoch_id: str | None, generation_id: str
) -> dict[str, int]:
    """Per-drift-kind event totals for one generation, averaged per entry.

    Returns ``{drift_kind: total_count}`` where ``total_count`` is the
    sum, over every board entry the generation ran, of that entry's
    *mean* drift count for the kind (averaged across the entry's runs,
    rounded). Averaging per entry — rather than summing raw rows — keeps
    a generation that was re-scored across two tournaments (duplicate
    ``loss_profiles`` rows) from double-counting its drift, exactly as
    :func:`_mean_drift_loss_per_generation` does for the scalar. The
    drift kind is the bare wire string (``metric_counts.name`` with the
    ``"drift:"`` namespace prefix stripped, including ``custom:<judge>``
    namespaced custom-judge kinds).

    Aborted runs are included: a run that drifted and then aborted
    still produced real drift events the movements view must reflect.
    A generation with no drift events yields an empty mapping.
    """
    # entry_id -> run_id -> {kind: count}. Two index hops: which runs
    # belong to the generation, then those runs' drift metric rows.
    run_rows = _query(
        conn,
        "SELECT entry_id, run_id FROM loss_profiles " "WHERE generation_id = ? AND epoch_id = ?",
        (generation_id, epoch_id),
    )
    runs_by_entry: dict[str, set[str]] = {}
    for r in run_rows:
        runs_by_entry.setdefault(r["entry_id"], set()).add(r["run_id"])
    all_run_ids = {rid for rids in runs_by_entry.values() for rid in rids}
    if not all_run_ids:
        return {}

    placeholders = ",".join("?" for _ in all_run_ids)
    metric_rows = _query(
        conn,
        f"SELECT run_id, name, count FROM metric_counts "
        f"WHERE namespace = 'drift' AND run_id IN ({placeholders})",
        tuple(all_run_ids),
    )
    per_run: dict[str, dict[str, int]] = {}
    for r in metric_rows:
        name = str(r["name"] or "")
        kind = name[len("drift:") :] if name.startswith("drift:") else name
        if not kind:
            continue
        bucket = per_run.setdefault(r["run_id"], {})
        bucket[kind] = bucket.get(kind, 0) + int(r["count"] or 0)

    # Per entry: mean count per kind across the entry's runs; then sum
    # those per-entry means across entries.
    totals: dict[str, float] = {}
    for run_ids in runs_by_entry.values():
        entry_kind_sums: dict[str, int] = {}
        for rid in run_ids:
            for kind, cnt in per_run.get(rid, {}).items():
                entry_kind_sums[kind] = entry_kind_sums.get(kind, 0) + cnt
        n_runs = len(run_ids) or 1
        for kind, total in entry_kind_sums.items():
            totals[kind] = totals.get(kind, 0.0) + total / n_runs
    return {kind: round(v) for kind, v in totals.items() if round(v) != 0}


def build_drift_movements(paths: WorkspacePaths, generation_id: str) -> dict[str, Any]:
    """``GET /api/drift-movements/:generation_id`` — champion->challenger drift deltas.

    For the tournament that produced ``generation_id`` (the challenger),
    compares the per-drift-kind event counts of the champion (parent)
    against the challenger and reports the movement of each kind.

    Returns ``{"epoch_id", "generation_id", "champion", "challenger",
    "movements": [{kind, champion_count, challenger_count, delta,
    direction}], "note"?}`` where ``direction`` is ``"worsened"`` (more
    drift on the challenger), ``"improved"`` (fewer), or ``"unchanged"``.
    Movements are sorted by descending ``|delta|`` so the biggest
    regressions and improvements surface first. A kind absent from one
    side counts as zero there.

    Degrades to an empty ``movements`` list (never raises) when the
    index, the tournament, or the parent generation cannot be resolved.
    """
    epoch_id = read_current_epoch(paths)
    empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "champion": None,
        "challenger": generation_id,
        "movements": [],
    }
    try:
        conn = _open_index(paths.index_db)
    except _IndexAbsent:
        return {**empty, "note": "index not built; run zicato reindex"}
    except sqlite3.Error:
        return empty

    try:
        tour = _query(
            conn,
            "SELECT parent_generation_id, child_generation_id FROM tournaments "
            "WHERE child_generation_id = ? LIMIT 1",
            (generation_id,),
        )
        if not tour:
            return {**empty, "note": "no tournament found for this generation"}
        parent_id = tour[0]["parent_generation_id"]
        child_id = tour[0]["child_generation_id"]

        champion_counts = _drift_counts_for_generation(conn, epoch_id, parent_id)
        challenger_counts = _drift_counts_for_generation(conn, epoch_id, child_id)

        movements: list[dict[str, Any]] = []
        for kind in sorted(set(champion_counts) | set(challenger_counts)):
            champ = champion_counts.get(kind, 0)
            chall = challenger_counts.get(kind, 0)
            delta = chall - champ
            if delta > 0:
                direction = "worsened"
            elif delta < 0:
                direction = "improved"
            else:
                direction = "unchanged"
            movements.append(
                {
                    "kind": kind,
                    "champion_count": champ,
                    "challenger_count": chall,
                    "delta": delta,
                    "direction": direction,
                }
            )
        # Biggest absolute movements first; ties broken alphabetically.
        movements.sort(key=lambda m: (-abs(m["delta"]), m["kind"]))
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "champion": parent_id,
            "challenger": child_id,
            "movements": movements,
        }
    finally:
        conn.close()


def build_health_report(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/health-report`` — the latest loop-health report."""
    epoch_id = read_current_epoch(paths)
    healthy_empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "findings": [],
        "healthy": True,
    }
    if epoch_id is None:
        return healthy_empty
    latest = _latest_round_report(paths.epoch_health_dir(epoch_id))
    if latest is None:
        return healthy_empty
    value = _read_json_value(latest)
    if not isinstance(value, dict):
        return healthy_empty
    report: dict[str, Any] = {
        "epoch_id": value.get("epoch_id") if isinstance(value.get("epoch_id"), str) else epoch_id,
        "findings": value.get("findings") if isinstance(value.get("findings"), list) else [],
        "healthy": value.get("healthy") if isinstance(value.get("healthy"), bool) else True,
    }
    checked_at = value.get("checked_at")
    if isinstance(checked_at, str):
        report["checked_at"] = checked_at
    return report


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
        }

    # Open the analytical index once for all epochs. Absent index = every
    # epoch surfaces a ``None`` best scalar but the row list still renders.
    conn: sqlite3.Connection | None
    try:
        conn = _open_index(paths.index_db)
    except (_IndexAbsent, sqlite3.Error):
        conn = None

    try:
        for epoch_dir in sorted(paths.epochs.iterdir()):
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
                for child in sorted(gens_dir.iterdir()):
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

    return {
        "current_epoch_id": current,
        "epochs": rows,
        "sparkline": sparkline,
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

    # Resolve predecessor: epoch directory immediately before ``epoch_id``
    # in sort order.
    predecessor: str | None = None
    if paths.epochs.is_dir():
        ids = sorted(d.name for d in paths.epochs.iterdir() if d.is_dir())
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


# ---------------------------------------------------------------------------
# Consolidated environment view — the single coalesced dashboard read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase-1 light-up: per-judge / per-entry / per-tournament helpers
# ---------------------------------------------------------------------------


def _tournament_id_for(epoch_id: str, parent_gen_id: str, child_gen_id: str) -> str:
    """Compose the tournament id keying convention used by the ingester.

    Mirrors :func:`zicato.index.ingest._tournament_id_for_run` exactly:
    a tournament round is ``{epoch_id}:{parent_gen}->{child_gen}``. Kept
    co-located with the dashboard reader so a downstream rename of the
    ingester's helper does not silently desync the FK-based endpoints.
    """
    return f"{epoch_id}:{parent_gen_id}->{child_gen_id}"


def build_per_judge_trend(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """Per-judge × generation matrix for one epoch.

    Returns ``{epoch_id, generations, judges: [{judge_name,
    by_generation: {gen_id: weighted_loss}}]}``. ``generations`` is the
    spine in lineage order (the promoted lineage when available, else
    every generation in directory order). The ``by_generation`` map is
    populated from :func:`zicato.index.query.judge_loss_trend` per judge.

    Best-effort: a never-indexed workspace yields empty
    ``generations`` / ``judges`` lists with a ``note``.
    """
    from zicato.index.query import judge_loss_trend  # noqa: PLC0415

    # Discover the set of judges seen in this epoch by walking the
    # generations directly. The trend query is per-judge so we need a
    # judge list before we can call it.
    judges: set[str] = set()
    try:
        import sqlite3 as _sql  # noqa: PLC0415

        conn = _sql.connect(str(paths.index_db))
        conn.row_factory = _sql.Row
        try:
            rows = conn.execute(
                "SELECT DISTINCT jl.judge_name "
                "FROM judge_losses AS jl "
                "JOIN runs AS r ON r.run_id = jl.run_id "
                "WHERE r.epoch_id = ? "
                "ORDER BY jl.judge_name",
                (epoch_id,),
            ).fetchall()
            for r in rows:
                if isinstance(r["judge_name"], str):
                    judges.add(r["judge_name"])
        except _sql.Error:
            pass
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {
            "epoch_id": epoch_id,
            "generations": [],
            "judges": [],
            "note": "index not built; run zicato reindex",
        }

    # Resolve the spine — the promoted lineage when available, else
    # every generation in directory order. The L1 heatmap renders only
    # promoted-spine generations so the columns stay narrow.
    lineage_view = build_lineage_view(paths)
    epoch_gens = [g for g in lineage_view.get("generations", []) if g.get("epoch_id") == epoch_id]
    spine = _champion_lineage(epoch_gens)
    if not spine:
        spine = [g["generation_id"] for g in epoch_gens]

    judge_rows: list[dict[str, Any]] = []
    for judge_name in sorted(judges):
        try:
            rows = judge_loss_trend(paths.index_db, epoch_id, judge_name)
        except Exception:  # noqa: BLE001
            rows = []
        by_gen: dict[str, float] = {}
        for r in rows:
            gid = r["generation_id"]
            val = r["total_weighted_loss"]
            if isinstance(gid, str) and isinstance(val, int | float):
                by_gen[gid] = float(val)
        judge_rows.append(
            {
                "judge_name": judge_name,
                "by_generation": by_gen,
            }
        )

    return {
        "epoch_id": epoch_id,
        "generations": spine,
        "judges": judge_rows,
    }


def build_per_judge_for_generation(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """Per-judge table for one generation.

    Returns ``{epoch_id, generation_id, judges: [{judge_name,
    weighted_loss, raw_loss, run_count, weight}]}`` keyed off
    :func:`zicato.index.query.judge_losses_for_generation`. A never-
    indexed workspace yields empty ``judges`` with a ``note``.
    """
    from zicato.index.query import judge_losses_for_generation  # noqa: PLC0415

    try:
        rows = judge_losses_for_generation(paths.index_db, epoch_id, generation_id)
    except Exception:  # noqa: BLE001
        return {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "judges": [],
            "note": "index not built; run zicato reindex",
        }
    judges = [
        {
            "judge_name": r["judge_name"],
            "weighted_loss": (
                float(r["total_weighted_loss"])
                if isinstance(r["total_weighted_loss"], int | float)
                else None
            ),
            "raw_loss": (
                float(r["total_raw_loss"]) if isinstance(r["total_raw_loss"], int | float) else None
            ),
            "run_count": int(r["run_count"]) if isinstance(r["run_count"], int) else None,
            "weight": float(r["weight"]) if isinstance(r["weight"], int | float) else None,
        }
        for r in rows
    ]
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "judges": judges,
    }


def build_per_entry_for_generation(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Per-entry breakdown of one generation, scoped via tournament_id FK.

    Returns ``{epoch_id, generation_id, tournament_id, entries:
    [{entry_id, run_id, drift_loss, pass_fail, runtime_ms,
    wall_clock_budget_exceeded, match_id, rung}]}``. The tournament id is
    composed via :func:`_tournament_id_for` from the child generation's
    ``parent_generation_id`` field (its ``experiment.json``); a v0 seed
    with no parent yields ``tournament_id: None`` and the fallback walks
    :func:`zicato.index.query.loss_profiles_for_generation` directly.

    ``match_id`` is the per-board-run tournament-provenance tag — the
    matchup id this run executed within (e.g. ``"rung0_m2"``,
    ``"racing-final"``) — and ``rung`` is the coarser label derived from
    it (e.g. ``"rung 0"``, ``"final"``) via
    :func:`zicato.selection.strategy.rung_for_match_id`. Both are ``None``
    for an untagged run: a gauntlet duel (which never carries a
    ``match_id``) or a legacy run persisted before the tag existed —
    additive, never an error.

    A never-indexed workspace yields empty ``entries`` with a ``note``.
    """
    from zicato.index.query import (  # noqa: PLC0415
        loss_profiles_for_generation,
        loss_profiles_for_tournament,
    )

    # Resolve the parent_generation_id from the child's experiment.json
    # so we can compose the FK. The reader is best-effort: a missing
    # / malformed file falls back to the generation-scoped query.
    exp_path = paths.epochs / epoch_id / "generations" / generation_id / "experiment.json"
    parent_gen_id: str | None = None
    raw_exp = _read_json_value(exp_path)
    if isinstance(raw_exp, dict):
        raw_parent = raw_exp.get("parent_generation_id")
        if isinstance(raw_parent, str) and raw_parent:
            parent_gen_id = raw_parent

    tournament_id: str | None = None
    rows: list[Any] = []
    if parent_gen_id is not None:
        tournament_id = _tournament_id_for(epoch_id, parent_gen_id, generation_id)
        try:
            rows = loss_profiles_for_tournament(paths.index_db, tournament_id)
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        # Either the FK lookup found nothing (v1 index without
        # backfill) or there is no parent. Walk the generation-scoped
        # query so a completed-but-orphaned tournament still surfaces.
        try:
            rows = loss_profiles_for_generation(paths.index_db, epoch_id, generation_id)
        except Exception:  # noqa: BLE001
            rows = []

    from zicato.selection.strategy import rung_for_match_id  # noqa: PLC0415

    def _match_id_of(row: Any) -> str | None:
        # ``match_id`` lands in schema v4. A stale index opened before
        # the migration ran would not carry the column; tolerate its
        # absence (and a NULL value) so an old index loads, not errors.
        try:
            keys = row.keys()
        except AttributeError:
            keys = ()
        if "match_id" not in keys:
            return None
        value = row["match_id"]
        return value if isinstance(value, str) and value else None

    entries = []
    for r in rows:
        match_id = _match_id_of(r)
        entries.append(
            {
                "entry_id": r["entry_id"],
                "run_id": r["run_id"],
                "generation_id": r["generation_id"],
                "drift_loss": (
                    float(r["drift_loss"]) if isinstance(r["drift_loss"], int | float) else None
                ),
                "pass_fail": r["pass_fail"],
                "runtime_ms": (int(r["runtime_ms"]) if isinstance(r["runtime_ms"], int) else None),
                "wall_clock_budget_exceeded": bool(r["wall_clock_budget_exceeded"])
                if r["wall_clock_budget_exceeded"] is not None
                else None,
                # Per-board-run tournament provenance (additive). ``None``
                # for an untagged run (gauntlet duel / legacy run).
                "match_id": match_id,
                "rung": rung_for_match_id(match_id),
            }
        )

    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "tournament_id": tournament_id,
        "entries": entries,
    }


def build_per_judge_comparison(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Per-judge Δ between two generations.

    Returns ``{epoch_id, champion, challenger, judges: [{judge_name,
    champion_weighted_loss, challenger_weighted_loss, delta}],
    primary_driver}``. ``primary_driver`` is the judge_name with the
    largest absolute delta; ``None`` when no judge fired on either side.

    A never-indexed workspace yields empty ``judges`` with a ``note``.
    """
    from zicato.index.query import judge_losses_for_generation  # noqa: PLC0415

    try:
        champ_rows = judge_losses_for_generation(paths.index_db, epoch_id, champion_id)
        chal_rows = judge_losses_for_generation(paths.index_db, epoch_id, challenger_id)
    except Exception:  # noqa: BLE001
        return {
            "epoch_id": epoch_id,
            "champion": champion_id,
            "challenger": challenger_id,
            "judges": [],
            "primary_driver": None,
            "note": "index not built; run zicato reindex",
        }

    by_judge: dict[str, dict[str, float | None]] = {}
    for r in champ_rows:
        name = r["judge_name"]
        if not isinstance(name, str):
            continue
        v = r["total_weighted_loss"]
        by_judge.setdefault(name, {"champion": None, "challenger": None})["champion"] = (
            float(v) if isinstance(v, int | float) else None
        )
    for r in chal_rows:
        name = r["judge_name"]
        if not isinstance(name, str):
            continue
        v = r["total_weighted_loss"]
        by_judge.setdefault(name, {"champion": None, "challenger": None})["challenger"] = (
            float(v) if isinstance(v, int | float) else None
        )

    judges: list[dict[str, Any]] = []
    primary_driver: str | None = None
    primary_abs: float = -1.0
    for name in sorted(by_judge):
        sides = by_judge[name]
        champ_v = sides.get("champion")
        chal_v = sides.get("challenger")
        delta: float | None = None
        if isinstance(champ_v, int | float) and isinstance(chal_v, int | float):
            delta = float(chal_v) - float(champ_v)
        elif isinstance(chal_v, int | float):
            delta = float(chal_v)
        elif isinstance(champ_v, int | float):
            delta = -float(champ_v)
        judges.append(
            {
                "judge_name": name,
                "champion_weighted_loss": champ_v,
                "challenger_weighted_loss": chal_v,
                "delta": delta,
            }
        )
        if delta is not None and abs(delta) > primary_abs:
            primary_abs = abs(delta)
            primary_driver = name

    return {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "judges": judges,
        "primary_driver": primary_driver,
    }


# ---------------------------------------------------------------------------
# Promote-gate breakdown (L3 decision view).
#
# The gate logic is authoritative in :mod:`zicato.tournament.gate`. This
# reader reconstructs the SAME decision the runner recorded by feeding the
# real :func:`evaluate_gate` (and its helpers) the champion / challenger
# aggregates read off disk, then decomposes the verdict into the gate's
# ordered rules with per-rule status. It never re-implements a threshold.
# ---------------------------------------------------------------------------


def _read_epoch_scoring_weights(paths: WorkspacePaths, epoch_id: str) -> Any:
    """Build the epoch's :class:`ScoringWeights` from its ``scoring.json``.

    The shipped ``workspace_loader`` / ``lifecycle`` parsers intentionally
    drop the gate-only fields (``regression_gate_enabled``,
    ``namespace_weights``, ``namespace_monotonicity``) — they only need the
    scalar weights. The gate breakdown DOES need them, so this reader maps
    every gate-relevant key through, falling back to the dataclass defaults
    when ``scoring.json`` is a partial / legacy document (or absent).
    """
    from zicato.core import ScoringWeights  # noqa: PLC0415

    raw = _read_json_value(paths.epochs / epoch_id / "scoring.json")
    if not isinstance(raw, dict):
        return ScoringWeights()

    defaults = ScoringWeights()
    kwargs: dict[str, Any] = {}

    if isinstance(raw.get("promote_margin"), int | float):
        kwargs["promote_margin"] = float(raw["promote_margin"])
    if "pass_rate_monotonicity" in raw:
        kwargs["pass_rate_monotonicity"] = bool(raw["pass_rate_monotonicity"])
    if "regression_gate_enabled" in raw:
        kwargs["regression_gate_enabled"] = bool(raw["regression_gate_enabled"])

    raw_ns_w = raw.get("namespace_weights")
    if isinstance(raw_ns_w, dict):
        try:
            kwargs["namespace_weights"] = {str(k): float(v) for k, v in raw_ns_w.items()}
        except (TypeError, ValueError):
            pass
    raw_ns_m = raw.get("namespace_monotonicity")
    if isinstance(raw_ns_m, dict):
        kwargs["namespace_monotonicity"] = {str(k): bool(v) for k, v in raw_ns_m.items()}

    try:
        return ScoringWeights(**kwargs)
    except TypeError:
        return defaults


def _gen_agg_for_gate(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any] | None:
    """Assemble the aggregate dict :func:`evaluate_gate` consumes.

    Prefers the cached ``gen_score.json`` (the persisted
    :func:`aggregate_generation_score` output, which already carries
    ``scalar`` / ``pass_rate`` / ``per_entry`` / ``namespace_aggregates`` /
    ``scalar_components``). When ``per_entry`` is missing it is
    reconstructed from the per-run ``loss.json`` files so the pass-rate
    monotonicity rule can still be judged. Returns ``None`` only when there
    is no scalar to compare at all (the rule set then degrades to unknown).
    """
    score = _read_gen_score(paths, epoch_id, generation_id)
    if not isinstance(score, dict):
        score = {}

    agg: dict[str, Any] = dict(score)

    if not isinstance(agg.get("per_entry"), dict):
        # Reconstruct {entry_id: {drift_loss, pass_fail}} from loss files
        # so the monotonicity rule has the two points it compares.
        loss_files = _read_run_loss_files(paths, epoch_id, generation_id)
        per_entry: dict[str, dict[str, Any]] = {}
        for entry_id, cell in loss_files.items():
            per_entry[entry_id] = {
                "drift_loss": cell.get("drift_loss"),
                "pass_fail": cell.get("pass_fail"),
            }
        if per_entry:
            agg["per_entry"] = per_entry

    if not isinstance(agg.get("scalar"), int | float):
        return None
    return agg


def build_gate_breakdown(
    paths: WorkspacePaths,
    epoch_id: str,
    champion_id: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Structured promote-gate decomposition for the L3 decision view.

    ``GET /api/round/{epoch_id}/{champion}/{challenger}/gate``. Reuses the
    authoritative :func:`zicato.tournament.gate.evaluate_gate` and its
    helpers so the breakdown always agrees with what the runner decided.

    Returns the rule-by-rule shape documented on the route handler. Rules
    are emitted in evaluation order (regression suite -> scalar margin ->
    pass-rate monotonicity -> namespace monotonicity). The first failing
    rule has ``status="fail"`` and ``fired=True``; rules after it are
    ``not_reached``; satisfied rules are ``pass``. Disabled rules are
    ``skipped`` (regression suite) / ``disabled`` (monotonicity flags) and
    never ``fired``. A rule whose inputs are unavailable degrades to
    ``unknown`` rather than guessing.
    """
    from zicato.tournament.gate import (  # noqa: PLC0415
        _regressed_entries,
        _regressed_namespaces,
        evaluate_gate,
    )

    weights = _read_epoch_scoring_weights(paths, epoch_id)

    parent_agg = _gen_agg_for_gate(paths, epoch_id, champion_id) if champion_id else None
    child_agg = _gen_agg_for_gate(paths, epoch_id, challenger_id)

    base: dict[str, Any] = {
        "epoch_id": epoch_id,
        "champion": champion_id,
        "challenger": challenger_id,
        "decision": "deferred",
        "reason": "",
        "delta_scalar": None,
        "delta_pass_rate": None,
        "rules": [],
        "scalar_components": {"champion": None, "challenger": None},
        "primary_driver": None,
    }

    # Echo the per-judge primary driver from the same source the L3
    # per-judge-comparison endpoint uses (best-effort; never fatal).
    try:
        comparison = build_per_judge_comparison(paths, epoch_id, champion_id, challenger_id)
        driver_name = comparison.get("primary_driver")
        if isinstance(driver_name, str) and driver_name:
            driver_delta: float | None = None
            for jrow in comparison.get("judges", []):
                if isinstance(jrow, dict) and jrow.get("judge_name") == driver_name:
                    d = jrow.get("delta")
                    driver_delta = float(d) if isinstance(d, int | float) else None
                    break
            base["primary_driver"] = {"judge": driver_name, "delta": driver_delta}
    except Exception:  # noqa: BLE001 — the driver echo is best-effort
        base["primary_driver"] = None

    # Surface the scalar components for both sides regardless of decision.
    if isinstance(parent_agg, dict):
        pc = parent_agg.get("scalar_components")
        if isinstance(pc, dict):
            base["scalar_components"]["champion"] = {
                str(k): float(v) for k, v in pc.items() if isinstance(v, int | float)
            }
    if isinstance(child_agg, dict):
        cc = child_agg.get("scalar_components")
        if isinstance(cc, dict):
            base["scalar_components"]["challenger"] = {
                str(k): float(v) for k, v in cc.items() if isinstance(v, int | float)
            }

    # ---- Build each rule. We assemble all four, then resolve their
    # ---- statuses against the authoritative gate verdict below.
    regression_enabled = bool(getattr(weights, "regression_gate_enabled", False))
    pass_mono_enabled = bool(getattr(weights, "pass_rate_monotonicity", True))

    def _ns_mono_any_enabled() -> bool:
        ns_mono = getattr(weights, "namespace_monotonicity", {}) or {}
        ns_weights = getattr(weights, "namespace_weights", {}) or {}
        return any(
            enabled and float(ns_weights.get(ns, 0.0)) != 0.0 for ns, enabled in ns_mono.items()
        )

    ns_mono_enabled = _ns_mono_any_enabled()

    # Without a comparable scalar on both sides we cannot reconstruct the
    # gate — every numeric rule degrades to "unknown".
    have_both = isinstance(parent_agg, dict) and isinstance(child_agg, dict)

    if not have_both:
        base["rules"] = [
            {
                "id": "regression_suite",
                "label": "Regression suite",
                "status": "skipped" if not regression_enabled else "unknown",
                "detail": (
                    "disabled"
                    if not regression_enabled
                    else "regression-suite outcome not recorded"
                ),
                "fired": False,
            },
            {
                "id": "scalar_margin",
                "label": "Scalar margin",
                "status": "unknown",
                "detail": "champion or challenger aggregate not found",
                "fired": False,
            },
            {
                "id": "pass_rate_monotonicity",
                "label": "Pass-rate monotonicity",
                "status": "disabled" if not pass_mono_enabled else "unknown",
                "detail": "disabled" if not pass_mono_enabled else "aggregates not found",
                "fired": False,
            },
            {
                "id": "namespace_monotonicity",
                "label": "Namespace monotonicity",
                "status": "disabled" if not ns_mono_enabled else "unknown",
                "detail": "disabled" if not ns_mono_enabled else "aggregates not found",
                "fired": False,
            },
        ]
        return base

    # Both aggregates present — run the real gate.
    assert isinstance(parent_agg, dict) and isinstance(child_agg, dict)
    outcome = evaluate_gate(parent_agg, child_agg, weights)
    base["decision"] = outcome.decision
    base["reason"] = outcome.reason
    base["delta_scalar"] = outcome.delta_scalar
    base["delta_pass_rate"] = outcome.delta_pass_rate

    parent_scalar = float(parent_agg["scalar"])
    child_scalar = float(child_agg["scalar"])
    promote_margin = float(getattr(weights, "promote_margin", 0.01))

    # Which rule fired? Re-derive deterministically (mirrors evaluate_gate's
    # short-circuit order) without re-implementing any threshold — we call
    # the same predicate evaluate_gate uses.
    scalar_failed = child_scalar > parent_scalar - promote_margin
    regressed_entries = _regressed_entries(parent_agg, child_agg) if pass_mono_enabled else []
    regressed_ns = _regressed_namespaces(parent_agg, child_agg, weights) if ns_mono_enabled else []

    # The fired rule is the first that rejects, in gate order. Regression
    # suite is a pre-gate the dashboard cannot replay (no recorded
    # outcome on disk), so it is reported as pass/skipped, never fired.
    fired_rule: str | None = None
    if scalar_failed:
        fired_rule = "scalar_margin"
    elif pass_mono_enabled and regressed_entries:
        fired_rule = "pass_rate_monotonicity"
    elif ns_mono_enabled and regressed_ns:
        fired_rule = "namespace_monotonicity"

    order = [
        "regression_suite",
        "scalar_margin",
        "pass_rate_monotonicity",
        "namespace_monotonicity",
    ]
    fired_index = order.index(fired_rule) if fired_rule is not None else len(order)

    # -- regression_suite --------------------------------------------
    if not regression_enabled:
        regression_rule = {
            "id": "regression_suite",
            "label": "Regression suite",
            "status": "skipped",
            "detail": "disabled",
            "fired": False,
        }
    else:
        # Enabled, but the dashboard has no recorded suite outcome to
        # replay. Honest degrade: the gate ran it, we just cannot show
        # which way it went from the on-disk aggregates alone.
        regression_rule = {
            "id": "regression_suite",
            "label": "Regression suite",
            "status": "unknown",
            "detail": "regression-suite outcome not recorded in the dashboard's read path",
            "fired": False,
        }

    # -- scalar_margin -----------------------------------------------
    scalar_detail = (
        f"{parent_scalar:.2f} → {child_scalar:.2f} "
        f"({child_scalar - parent_scalar:+.2f}; needs ≤ "
        f"{-promote_margin:.2f})"
    )
    scalar_rule = {
        "id": "scalar_margin",
        "label": "Scalar margin",
        "status": "fail" if fired_rule == "scalar_margin" else "pass",
        "detail": scalar_detail,
        "fired": fired_rule == "scalar_margin",
    }
    if fired_index < order.index("scalar_margin"):
        scalar_rule["status"] = "not_reached"

    # -- pass_rate_monotonicity --------------------------------------
    if not pass_mono_enabled:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "disabled",
            "detail": "disabled",
            "fired": False,
        }
    elif fired_index < order.index("pass_rate_monotonicity"):
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "not_reached",
            "detail": "not reached (an earlier rule fired)",
            "fired": False,
        }
    elif regressed_entries:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "fail",
            "detail": "regressed: " + ", ".join(regressed_entries),
            "fired": fired_rule == "pass_rate_monotonicity",
        }
    else:
        pass_rule = {
            "id": "pass_rate_monotonicity",
            "label": "Pass-rate monotonicity",
            "status": "pass",
            "detail": "all preserved",
            "fired": False,
        }

    # -- namespace_monotonicity --------------------------------------
    if not ns_mono_enabled:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "disabled",
            "detail": "disabled",
            "fired": False,
        }
    elif fired_index < order.index("namespace_monotonicity"):
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "not_reached",
            "detail": "not reached (an earlier rule fired)",
            "fired": False,
        }
    elif not isinstance(child_agg.get("namespace_aggregates"), dict):
        # The rule is enabled but we lack the namespace aggregates to
        # judge it — degrade honestly rather than claim "all within".
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "unknown",
            "detail": "namespace aggregates not recorded",
            "fired": False,
        }
    elif regressed_ns:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "fail",
            "detail": "regressed: " + ", ".join(regressed_ns),
            "fired": fired_rule == "namespace_monotonicity",
        }
    else:
        ns_rule = {
            "id": "namespace_monotonicity",
            "label": "Namespace monotonicity",
            "status": "pass",
            "detail": "all within bounds",
            "fired": False,
        }

    base["rules"] = [regression_rule, scalar_rule, pass_rule, ns_rule]
    return base


def build_per_judge_for_run(paths: WorkspacePaths, run_id: str) -> dict[str, Any]:
    """Per-judge breakdown for one run.

    Returns ``{run_id, judges: [{judge_name, weighted_loss, raw_loss,
    weight}]}``. A never-indexed workspace yields empty ``judges``.
    """
    from zicato.index.query import judge_losses_for_run  # noqa: PLC0415

    try:
        rows = judge_losses_for_run(paths.index_db, run_id)
    except Exception:  # noqa: BLE001
        return {"run_id": run_id, "judges": [], "note": "index not built; run zicato reindex"}
    judges = [
        {
            "judge_name": r["judge_name"],
            "weighted_loss": (
                float(r["weighted_loss"]) if isinstance(r["weighted_loss"], int | float) else None
            ),
            "raw_loss": (float(r["raw_loss"]) if isinstance(r["raw_loss"], int | float) else None),
            "weight": float(r["weight"]) if isinstance(r["weight"], int | float) else None,
        }
        for r in rows
    ]
    return {"run_id": run_id, "judges": judges}


def _load_run_loss(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any] | None:
    """Read a board-entry run's ``loss.json`` defensively.

    Returns the parsed dict or ``None`` when the file is absent or
    unreadable. Used by :func:`build_expectation_outcomes_for_run` and
    :func:`build_run_header` to project structured fields without
    requiring an indexed workspace.
    """
    loss_path = (
        paths.epochs / epoch_id / "generations" / generation_id / "runs" / entry_id / "loss.json"
    )
    try:
        loss = json.loads(loss_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None
    return loss if isinstance(loss, dict) else None


def build_expectation_outcomes_for_run(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any]:
    """Structured expectation outcomes for a single run (L4).

    The reducer stamps a single ``expectation_result`` on each run's
    ``loss.json`` — a dict shaped ``{kind, passed, detail}`` (see
    :class:`zicato.core.types.ExpectationResult`). This reader projects
    it into a list-shaped payload so the L4 view can render a uniform
    table regardless of whether the entry carried zero, one, or
    (forward-compat) several expectations.

    Returns ``{epoch_id, generation_id, entry_id, outcomes: [...]}``
    where each outcome has the fields:

    * ``kind`` — the matcher discriminant (``predicate``, ``regex``,
      ``expected_text``, ``json_schema``, ``rubric``, or a custom
      kind-like string the reducer happened to stamp).
    * ``passed`` — ``True`` / ``False`` / ``None`` (the matcher could
      not produce a verdict).
    * ``detail`` — human-readable explanation (regex match position,
      judge rationale, predicate return). Empty string when the
      matcher had nothing to say.
    * ``judge_name`` — the rubric's judge identity when ``kind`` is
      ``rubric``; ``None`` otherwise.
    * ``score`` — the rubric's numeric score when present; ``None``
      otherwise.

    An entry with no expectation (``expectation_result`` is ``None``)
    or no on-disk ``loss.json`` yields an empty ``outcomes`` list — the
    L4 view shows ``(no expectations recorded for this run)``.
    """
    empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "outcomes": [],
    }
    loss = _load_run_loss(paths, epoch_id, generation_id, entry_id)
    if loss is None:
        return empty
    raw = loss.get("expectation_result")
    if raw is None:
        return empty

    # The reducer stamps a single dict today; we normalise to a list to
    # keep the wire shape stable when multi-expectation entries land.
    if isinstance(raw, dict):
        items: list[Any] = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        return empty

    outcomes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        kind_str = str(kind) if isinstance(kind, str) else None
        passed_raw = item.get("passed")
        if isinstance(passed_raw, bool):
            passed: bool | None = passed_raw
        else:
            passed = None
        detail = item.get("detail")
        detail_str = detail if isinstance(detail, str) else ""
        judge_raw = item.get("judge_name")
        judge_name = judge_raw if isinstance(judge_raw, str) and judge_raw else None
        score_raw = item.get("score")
        score: float | None
        if isinstance(score_raw, int | float) and not isinstance(score_raw, bool):
            score = float(score_raw)
        else:
            score = None
        outcomes.append(
            {
                "kind": kind_str,
                "passed": passed,
                "detail": detail_str,
                "judge_name": judge_name,
                "score": score,
            }
        )
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
        "outcomes": outcomes,
    }


def build_run_header(
    paths: WorkspacePaths,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> dict[str, Any]:
    """Per-run header metrics (L4).

    Projects the numeric / verdict header fields from a board-entry
    run's ``loss.json``. The L4 page already shows ``drift_loss`` and
    ``pass_fail`` from the per-entry table; this reader surfaces the
    remaining header fields the previous placeholder promised:

    * ``runtime_ms`` — total wall-clock duration in ms.
    * ``tokens_spent`` — LLM token cost as recorded by the harness.
    * ``output_chars`` — characters in the run's final output.
    * ``turns_completed`` — conversational turns executed (multi-turn
      only; ``None`` for single-turn).
    * ``plan_revisions`` — count of plan-revision events observed.
    * ``wall_clock_budget_exceeded`` — ``True`` iff the run was force-
      aborted by its budget.

    Plus the headline verdict numbers so the frontend can render the
    full strip from one payload:

    * ``drift_loss``, ``pass_fail``, ``run_id``.

    Also surfaces the ADK session id persisted in ``loss.json`` by the
    reducer, so the L4 header can deep-link into harmonograf at the
    run's execution trace without a second roundtrip to ``events.jsonl``:

    * ``adk_session_id`` — the goldfive/ADK session id for this run.

    Every field defaults to ``None`` when ``loss.json`` is absent or
    missing the key; the response shape is stable so the L4 renderer
    never branches on whether the file exists.
    """
    keys = (
        "drift_loss",
        "pass_fail",
        "runtime_ms",
        "tokens_spent",
        "output_chars",
        "turns_completed",
        "plan_revisions",
        "wall_clock_budget_exceeded",
        "run_id",
        "adk_session_id",
    )
    header: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "entry_id": entry_id,
    }
    for k in keys:
        header[k] = None
    loss = _load_run_loss(paths, epoch_id, generation_id, entry_id)
    if loss is None:
        return header
    for k in keys:
        v = loss.get(k)
        # Pass scalars (numeric / bool / str) and ``None`` through;
        # discard nested dicts / lists which are not header material.
        if v is None or isinstance(v, int | float | str | bool):
            header[k] = v
    return header


def build_workspace_identity(paths: WorkspacePaths) -> dict[str, Any]:
    """Structured workspace identity block — Phase 1's L0 env object.

    Replaces the bare ``str(paths.root)`` previously surfaced as
    ``state.workspace``. Returns an object with the fields the L0 view's
    environment-configuration table renders:

    * ``root`` — absolute path to the ``.zicato`` directory.
    * ``adk_entrypoint`` — the adapter's entrypoint (e.g.
      ``mod:agent``) when ``config.json`` carries one, else ``None``.
    * ``source_roots`` — every ``mutable_trees`` entry from
      ``config.json`` (empty list when absent).
    * ``board_path`` / ``brief_path`` / ``scoring_path`` — absolute
      paths to the current epoch's contract files when an epoch is
      live, else ``None``.
    * ``mutation_point_count`` — the count of mutation points the
      enumerator finds across ``source_roots``. ``0`` when the
      enumerator fails or there are no source roots — never raises.
    * ``instance_id`` — heartbeat's ``instance_id`` when present,
      else ``"default"`` (the runtime's seed default).
    * ``created_at`` — heartbeat's ``started_at`` when present, else
      ``None`` (the workspace is too young to have a heartbeat).
    """
    cfg = _read_json_value(paths.root / "config.json")
    adapter = cfg.get("adapter") if isinstance(cfg, dict) else None
    adapter = adapter if isinstance(adapter, dict) else {}

    entrypoint = adapter.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        if isinstance(cfg, dict):
            for key in ("adk_entrypoint", "entrypoint"):
                val = cfg.get(key)
                if isinstance(val, str) and val:
                    entrypoint = val
                    break
            else:
                entrypoint = None
        else:
            entrypoint = None

    raw_trees = adapter.get("mutable_trees")
    if not isinstance(raw_trees, list) and isinstance(cfg, dict):
        raw_trees = cfg.get("mutable_trees")
    if isinstance(raw_trees, list):
        source_roots = [t for t in raw_trees if isinstance(t, str)]
    else:
        source_roots = []

    epoch_id = read_current_epoch(paths)
    if epoch_id is not None:
        epoch_dir = paths.epochs / epoch_id
        board_path = str(epoch_dir / "board.jsonl")
        brief_path_candidate = epoch_dir / "brief.md"
        if not brief_path_candidate.exists():
            legacy = epoch_dir / "rubric.md"
            brief_path = str(legacy) if legacy.exists() else str(brief_path_candidate)
        else:
            brief_path = str(brief_path_candidate)
        scoring_path = str(epoch_dir / "scoring.json")
    else:
        board_path = None
        brief_path = None
        scoring_path = None

    # Mutation point enumeration — best-effort so a malformed source
    # tree never bubbles up to the dashboard endpoint as a 500. The
    # enumerator walks every source root for ``# zicato:mutable`` markers
    # plus a goldfive manifest if one exists.
    mutation_point_count = 0
    if source_roots:
        try:
            from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415

            mutation_point_count = len(enumerate_mutations([Path(r) for r in source_roots]))
        except Exception:  # noqa: BLE001 — best-effort
            mutation_point_count = 0

    hb = read_heartbeat_dict(paths)
    instance_id = "default"
    created_at: str | None = None
    if isinstance(hb, dict):
        raw_iid = hb.get("instance_id")
        if isinstance(raw_iid, str) and raw_iid:
            instance_id = raw_iid
        raw_started = hb.get("started_at")
        if isinstance(raw_started, str) and raw_started:
            created_at = raw_started

    return {
        "root": str(paths.root),
        "adk_entrypoint": entrypoint,
        "source_roots": source_roots,
        "board_path": board_path,
        "brief_path": brief_path,
        "scoring_path": scoring_path,
        "mutation_point_count": mutation_point_count,
        "instance_id": instance_id,
        "created_at": created_at,
    }


def build_environment(
    paths: WorkspacePaths, run_log_limit: int = RUN_LOG_DEFAULT_LIMIT
) -> dict[str, Any]:
    """One coalesced snapshot of the whole instantiated zicato environment.

    ``GET /api/environment`` returns this. It folds together every
    cross-view feed the dashboard needs — the epoch contract, the live
    and resolved tournaments, the generation lineage, active runs, loop
    health, the heartbeat, and the run-log tail — so the front-end can
    refresh the entire environment view with ONE request per change
    instead of fanning out to six endpoints many times a second.

    ``epochs`` is a lightweight per-epoch summary list -- ``{epoch_id,
    goal}`` -- so the Overview's epochs table can show what each epoch
    is trying to accomplish without a per-epoch ``/api/epoch`` fetch.

    ``workspace`` is now a structured identity block (see
    :func:`build_workspace_identity`) so the L0 view can render
    entrypoint / source roots / contract paths / mutation-point count
    without a second fetch. The legacy callers that expected a plain
    string still find the root path on ``workspace.root``.

    Every component degrades independently: a missing or unreadable
    input becomes an empty / ``None`` value, never an exception, so this
    function — like every reader here — cannot 500 an endpoint.
    """
    # ``health`` here is the dashboard *service* identity (version /
    # port / build) and is supplied by the /api/health route handler,
    # not this reader — it is intentionally absent from the environment
    # payload. ``heartbeat`` is the orchestrator's runtime heartbeat.
    return {
        "workspace": build_workspace_identity(paths),
        "epoch_id": read_current_epoch(paths),
        "epoch": build_epoch_view(paths),
        "epochs": build_epochs_summary(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "tournaments": build_bracket(paths),
        "generations": build_lineage_view(paths),
        "score_trajectory": build_score_trajectory(paths),
        "active_runs": read_active_runs_view(paths),
        "health_report": build_health_report(paths),
        "heartbeat": read_heartbeat_dict(paths),
        "lock": read_lock_dict(paths),
        "run_log": build_run_log(paths, run_log_limit),
        "generated_at": _iso(_utc_now()),
    }


# ---------------------------------------------------------------------------
# Sidebar search — entries / judges / patches / mutations
# ---------------------------------------------------------------------------
#
# The sidebar exposes an always-visible search bar (no per-page navigation
# away from the current view). ``build_search_results`` walks the live
# workspace for substring + exact matches across four categories:
#
#   * entries   — id substring against the current epoch's ``board.jsonl``
#   * judges    — name substring against in-board judges + index judge_losses
#   * patches   — mutation_id / rationale substring against the index
#   * mutations — mutation_id substring against the index's patches table
#
# Each category is independently capped at :data:`SEARCH_LIMIT_PER_CATEGORY`
# results. Exact matches are sorted before substring matches so a query
# that names an id outright surfaces it first. Empty / whitespace queries
# short-circuit to empty result sets so the caller cannot tax the index
# with a degenerate scan.

#: Per-category cap on the number of results returned. The sidebar UI is
#: narrow; ten matches per category is enough to surface the obvious
#: targets without overwhelming the panel.
SEARCH_LIMIT_PER_CATEGORY = 10


def _empty_search_result() -> dict[str, Any]:
    return {
        "entries": [],
        "judges": [],
        "patches": [],
        "mutations": [],
    }


def _collect_judge_names_from_board_file(path: Path) -> set[str]:
    """Walk a raw ``board.jsonl`` and union every judge name."""
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return names
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("board_meta") is True:
            continue
        judges = obj.get("judges")
        if not isinstance(judges, list):
            continue
        for j in judges:
            if not isinstance(j, dict):
                continue
            name = j.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _collect_judge_names_from_index(db_path: Path) -> set[str]:
    """Union of distinct ``judge_name`` values in the analytical index."""
    names: set[str] = set()
    if not db_path.is_file():
        return names
    try:
        import sqlite3  # noqa: PLC0415

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute("SELECT DISTINCT judge_name FROM judge_losses")
            for row in cur.fetchall():
                if isinstance(row[0], str) and row[0]:
                    names.add(row[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort; missing table is OK
        return names
    return names


def _sort_by_match_quality(
    items: list[dict[str, Any]], key: str, q_lower: str
) -> list[dict[str, Any]]:
    """Sort exact matches (case-insensitive) before substring matches.

    Each item carries a ``match_kind`` field set to ``"exact"`` when the
    item's ``key`` equals the query case-insensitively, ``"substring"``
    otherwise. Ties within a kind are sorted by the matched field.
    """

    def _rank(item: dict[str, Any]) -> tuple[int, str]:
        val = str(item.get(key, "") or "")
        is_exact = val.lower() == q_lower
        item["match_kind"] = "exact" if is_exact else "substring"
        return (0 if is_exact else 1, val.lower())

    items.sort(key=_rank)
    return items


def build_search_results(paths: WorkspacePaths, query: str) -> dict[str, Any]:
    """Search entries / judges / patches / mutations for substring matches.

    Returns a dict keyed by category, each value a list of small match
    records carrying enough fields to build a navigation link client-side.
    Every category is independently capped at
    :data:`SEARCH_LIMIT_PER_CATEGORY`; exact (case-insensitive) matches
    sort before substring matches.

    The current epoch (as recorded by ``current_epoch``) bounds the entry
    + judge scans. Patch + mutation scans cover every epoch in the index
    so an operator can locate a historical mutation across the workspace.
    A degenerate query (empty / whitespace) short-circuits to empty
    results so the caller cannot accidentally fan out a wide scan.
    """
    q = (query or "").strip()
    if not q:
        return _empty_search_result()
    q_lower = q.lower()

    result = _empty_search_result()
    epoch_id = read_current_epoch(paths)

    # --- entries: walk the current epoch's board.jsonl ---------------
    entry_hits: list[dict[str, Any]] = []
    if epoch_id:
        board_path = paths.epochs / epoch_id / "board.jsonl"
        board = _parse_board(board_path)
        if board:
            for entry in board:
                eid = entry.get("id")
                if not isinstance(eid, str) or not eid:
                    continue
                if q_lower in eid.lower():
                    entry_hits.append({"id": eid})
    entry_hits = _sort_by_match_quality(entry_hits, "id", q_lower)
    result["entries"] = entry_hits[:SEARCH_LIMIT_PER_CATEGORY]

    # --- judges: board + index union ---------------------------------
    judge_names: set[str] = set()
    if epoch_id:
        judge_names |= _collect_judge_names_from_board_file(paths.epochs / epoch_id / "board.jsonl")
    judge_names |= _collect_judge_names_from_index(paths.index_db)
    judge_hits: list[dict[str, Any]] = [{"name": n} for n in judge_names if q_lower in n.lower()]
    judge_hits = _sort_by_match_quality(judge_hits, "name", q_lower)
    result["judges"] = judge_hits[:SEARCH_LIMIT_PER_CATEGORY]

    # --- patches + mutations: index scan ------------------------------
    # Both share the same patches table; one scan populates both, since
    # a mutation_id hit also implies the patch is interesting and a
    # rationale-only hit is a patch-only match.
    if paths.index_db.is_file():
        import sqlite3  # noqa: PLC0415

        try:
            conn = sqlite3.connect(str(paths.index_db))
            try:
                conn.row_factory = sqlite3.Row
                # The LIKE patterns mirror the substring semantics the
                # frontend describes to operators. SQLite's LIKE is
                # case-insensitive for ASCII by default — the typical
                # case for mutation ids + rationale text.
                like = f"%{q}%"
                rows = conn.execute(
                    "SELECT patch_id, epoch_id, generation_id, mutation_id, "
                    "       op, rationale FROM patches "
                    "WHERE mutation_id LIKE ? OR rationale LIKE ? "
                    "ORDER BY epoch_id ASC, generation_id ASC",
                    (like, like),
                ).fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — best-effort
            rows = []

        patch_hits: list[dict[str, Any]] = []
        mutation_hits: list[dict[str, Any]] = []
        seen_mutations: set[tuple[str, str, str]] = set()
        for row in rows:
            patch_id = row["patch_id"]
            ep_id = row["epoch_id"]
            gen_id = row["generation_id"]
            mut_id = row["mutation_id"]
            rationale = row["rationale"] or ""
            snippet = _preview(rationale) if rationale else ""
            patch_hits.append(
                {
                    "patch_id": patch_id,
                    "epoch_id": ep_id,
                    "generation_id": gen_id,
                    "mutation_id": mut_id,
                    "rationale_snippet": snippet,
                }
            )
            # A mutation row is interesting only when the substring
            # actually hits the mutation_id (a rationale-only match
            # belongs in patches but not in mutations).
            if isinstance(mut_id, str) and mut_id and q_lower in mut_id.lower():
                key = (mut_id, ep_id or "", gen_id or "")
                if key in seen_mutations:
                    continue
                seen_mutations.add(key)
                mutation_hits.append(
                    {
                        "mutation_id": mut_id,
                        "epoch_id": ep_id,
                        "generation_id": gen_id,
                        "patch_id": patch_id,
                    }
                )

        # Patch records are sorted by mutation_id quality (the most
        # operator-meaningful field); rationale-only hits fall to
        # the substring bucket regardless of whether the mutation_id
        # matches verbatim.
        patch_hits = _sort_by_match_quality(patch_hits, "mutation_id", q_lower)
        mutation_hits = _sort_by_match_quality(mutation_hits, "mutation_id", q_lower)
        result["patches"] = patch_hits[:SEARCH_LIMIT_PER_CATEGORY]
        result["mutations"] = mutation_hits[:SEARCH_LIMIT_PER_CATEGORY]

    return result
