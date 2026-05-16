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


def read_active_tournament_dict(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The active tournament as a plain dict, or ``None`` when absent."""
    try:
        t = read_active_tournament(paths.root)
    except Exception:
        # Fall back to the raw file so a shape the typed reader rejects
        # still surfaces rather than vanishing.
        return _read_json_value(paths.active_tournament)
    return t.to_dict() if t is not None else None


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


def read_active_runs_view(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """``active_runs/*.json`` enriched with computed deadline progress.

    Each row inlines every on-disk ``ActiveRun`` field and adds
    ``progress`` (deadline fraction), ``elapsed_seconds`` and
    ``budget_seconds`` — exactly what ``/api/active-runs`` returns from
    the Rust ``read_active_runs_view``.
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


def build_epoch_view(paths: WorkspacePaths) -> dict[str, Any]:
    """The current epoch's full evaluation contract.

    Matches the Rust ``epoch::build_epoch_view`` shape: no current epoch
    yields ``{"epoch_id": null}``; every other component degrades to
    empty / ``null``.
    """
    epoch_id = read_current_epoch(paths)
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
    # legacy name and is read as a fallback so pre-rename epochs still
    # display. Any read error degrades to an empty string.
    view["brief"] = ""
    for name in ("brief.md", "rubric.md"):
        try:
            view["brief"] = (epoch_dir / name).read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            continue
        except OSError:
            break

    scoring = _read_json_value(epoch_dir / "scoring.json")
    if scoring is not None:
        view["scoring"] = scoring

    # mutations.json is optional; absent -> empty list (never null).
    mutations = _parse_mutations(epoch_dir / "mutations.json")
    view["mutations"] = mutations if mutations is not None else []
    return view


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


def build_run_log(paths: WorkspacePaths, limit: int) -> dict[str, Any]:
    """``GET /api/run-log`` body — the last ``limit`` parseable events."""
    path = locate_events_file(paths)
    events = _tail_events(path, limit) if path is not None else []
    return {"events": events}


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


def build_bracket(paths: WorkspacePaths) -> dict[str, Any]:
    """``GET /api/tournaments`` — the bracket for the current epoch."""
    epoch_id = read_current_epoch(paths)
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
        return {
            "epoch_id": epoch_id,
            "champion_lineage": champion_lineage,
            "matchups": matchups,
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
            "SELECT entry_id, drift_loss, pass_fail FROM loss_profiles "
            "WHERE generation_id = ? ORDER BY entry_id ASC",
            (generation_id,),
        )
        parent_losses = (
            _query(
                conn,
                "SELECT entry_id, drift_loss, pass_fail FROM loss_profiles "
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
        for r in child_losses:
            key = r["entry_id"] or ""
            cell = ab.setdefault(key, {"entry_id": r["entry_id"]})
            cell["entry_id"] = r["entry_id"]
            cell["child_drift_loss"] = r["drift_loss"]
            cell["child_pass_fail"] = r["pass_fail"]
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


def find_run_events_path(paths: WorkspacePaths, run_id: str) -> Path | None:
    """Locate the ``events.jsonl`` for one run id.

    Tries, in order: the run's ``active_runs/{run_id}.json``
    (``events_jsonl_path``); a directory named ``run_id`` anywhere under
    ``epochs/*/generations/*/runs/`` carrying an ``events.jsonl``.
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
