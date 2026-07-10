"""runtime_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from zicato.query.epoch_view import build_epoch_view
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _parse_iso,
    _read_json_value,
    _utc_now,
    read_current_epoch,
)
from zicato.runtime.state import (
    list_active_runs,
    read_active_tournament,
    read_heartbeat,
)

# ---------------------------------------------------------------------------
# Runtime state — heartbeat / lock / active runs / active tournament
# ---------------------------------------------------------------------------


def read_paused(paths: WorkspacePaths) -> bool:
    """Whether the operator ``pause_epoch`` flag is present.

    The dashboard's pause control writes ``runtime/control/pause_epoch``
    and the orchestrator's ``block_while_paused`` holds scheduling until
    it clears — so flag presence IS the paused state. Cheap existence
    check (no JSON parse); any stat failure reads as not-paused so the
    runtime payload never errors on it.
    """
    try:
        return (paths.control_dir / "pause_epoch").exists()
    except OSError:
        return False


def read_heartbeat_dict(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The heartbeat as a plain dict, or ``None`` when absent.

    Always surfaces an AGEABLE ``last_heartbeat`` so the dashboard's
    staleness gate can decide live-vs-stale. The :class:`Heartbeat`
    writer stamps a fresh ``last_heartbeat`` on every bump, but a record
    that landed on disk with an absent / empty / unparseable stamp (e.g.
    a hand-edited file, a torn-down run, or a producer that omitted it)
    would otherwise leave the frontend with no timestamp to age out —
    and a dead run would read LIVE forever. When the on-disk
    ``last_heartbeat`` is not a usable ISO timestamp we fall back to the
    heartbeat file's mtime so the API always returns an ageable value.

    Standalone-harmonograf injection
    --------------------------------
    When the dashboard PROCESS resolved a persistent per-workspace
    harmonograf (``paths.harmonograf_url`` is non-empty), the resolved URL
    is injected so the frontend's liveness-gated deep-links light up
    against the persisted sessions even though no live evolve is writing
    ``harmonograf_url``. Precedence: a live evolve's heartbeat
    ``harmonograf_url`` WINS — it names the live run's own server — so the
    injected URL only fills the field when the heartbeat has none. A
    distinct ``harmonograf_persistent`` flag is always set on injection so
    the frontend can treat a persistent server as "live" (the standalone
    server does NOT die with a run, unlike the evolve-launched one).

    When there is no heartbeat on disk at all (a never-run / post-mortem
    workspace) but the dashboard resolved a persistent server, a SYNTHETIC
    heartbeat carrying only the harmonograf fields is returned so the
    deep-links still render.
    """
    try:
        hb = read_heartbeat(paths.root)
    except Exception:
        hb = None
    injected_url = getattr(paths, "harmonograf_url", "") or ""
    if hb is None:
        if injected_url:
            # No on-disk heartbeat (post-mortem workspace) — synthesize a
            # minimal one carrying only the harmonograf fields so the
            # standalone deep-links render. Recover the meta-loop session
            # id off the persisted JSONL so the zicato-level "execution"
            # link resolves post-mortem (docs/design/HARMONOGRAF.md §2b).
            synthetic: dict[str, Any] = {
                "harmonograf_url": injected_url,
                "harmonograf_persistent": True,
                "last_heartbeat": _heartbeat_file_mtime_iso(paths),
                "paused": read_paused(paths),
            }
            synthetic["ts"] = _heartbeat_ts_ms(synthetic["last_heartbeat"])
            meta = read_meta_loop_session_id(paths)
            if meta:
                synthetic["harmonograf_meta_session"] = meta
            return synthetic
        return None
    out = hb.to_dict()
    # Pause-flag presence rides on the heartbeat payload so every runtime
    # read (/api/heartbeat, /api/state, /api/environment, the SSE snapshot)
    # carries the paused state without a second fetch. Additive — an older
    # frontend simply ignores it.
    out["paused"] = read_paused(paths)
    if _parse_iso(out.get("last_heartbeat")) is None:
        out["last_heartbeat"] = _heartbeat_file_mtime_iso(paths)
    # THE one typed liveness timestamp: `ts`, integer MILLISECONDS since the
    # epoch, stamped server-side from the ageable `last_heartbeat`. The
    # frontend ages the heartbeat off THIS field alone — no ISO parsing, no
    # sec-vs-ms magnitude guessing, no alternate keys.
    out["ts"] = _heartbeat_ts_ms(out["last_heartbeat"])
    if injected_url:
        # Heartbeat-from-live-evolve wins: only fill the URL when absent.
        existing = out.get("harmonograf_url")
        if not isinstance(existing, str) or not existing.strip():
            out["harmonograf_url"] = injected_url
        out["harmonograf_persistent"] = True
        # Same precedence for the meta-loop session id: a live evolve's
        # heartbeat value wins; only recover off the JSONL when absent
        # (e.g. an older heartbeat from before this field existed, or a
        # standalone dashboard over a finished workspace).
        existing_meta = out.get("harmonograf_meta_session")
        if not isinstance(existing_meta, str) or not existing_meta.strip():
            meta = read_meta_loop_session_id(paths)
            if meta:
                out["harmonograf_meta_session"] = meta
    return out


def _heartbeat_ts_ms(last_heartbeat: Any) -> int | None:
    """``last_heartbeat`` (ISO) as integer ms-epoch, or ``None`` if unparseable."""
    parsed = _parse_iso(last_heartbeat)
    if parsed is None:
        return None
    return int(parsed.timestamp() * 1000)


def _heartbeat_file_mtime_iso(paths: WorkspacePaths) -> str:
    """ISO-8601 UTC mtime of ``heartbeat.json``, or current time on error.

    The fallback ageable timestamp when a heartbeat record carries no
    usable ``last_heartbeat``. The file's mtime is the freshest moment
    the heartbeat was rewritten — exactly the staleness signal we want.
    Degrades to "now" only when the file cannot be stat'd, which keeps
    the record from spuriously reading stale on a transient stat error.
    """
    try:
        mtime = paths.heartbeat.stat().st_mtime
        return _iso(_dt.datetime.fromtimestamp(mtime, _dt.UTC))
    except OSError:
        return _iso(_utc_now())


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

    An elim (``single_elim`` / ``double_elim``) payload additionally
    carries the served ELIM MODEL (``attach_elim_states``: canonicalized
    rounds + top-level ``gen_states``), exactly as on the settled
    structure record — the live figures read the model, never re-derive
    it (DQ1). The Rust supervisor's ``read_active_tournament`` applies
    the same fold (``crates/supervisor/src/elim_states.rs``).
    """
    # Lazy import: tournament_view imports THIS module for the settled path.
    from zicato.query.tournament_view import attach_elim_states  # noqa: PLC0415

    try:
        t = read_active_tournament(paths.root)
    except Exception:
        # Fall back to the raw file so a shape the typed reader rejects
        # still surfaces rather than vanishing.
        raw = _normalize_tournament_statuses(_read_json_value(paths.active_tournament))
        return attach_elim_states(raw) if isinstance(raw, dict) else raw
    if t is None:
        return None
    out = _normalize_tournament_statuses(t.to_dict())
    return attach_elim_states(out) if isinstance(out, dict) else out


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


def read_meta_loop_session_id(paths: WorkspacePaths) -> str:
    """Best-effort recovery of the zicato meta-loop harmonograf session id.

    The meta-loop session id is deterministic from the evolve start ISO
    (``meta_loop_session_id`` in the supervisor), but the dashboard does
    not otherwise know that start time — so for a post-mortem workspace
    (no live heartbeat carrying ``harmonograf_meta_session``) we recover
    the id by reading ``session_id`` / ``sessionId`` off the first event
    line of ``<ws>/.zicato/runtime/meta_loop_events.jsonl``. That JSONL is
    written by every meta-loop emit (``telemetry/meta_loop.py``) and
    survives the evolve, so the zicato-level deep-link still resolves
    post-mortem. Returns ``""`` on any failure (no meta-loop run yet, a
    degraded install that wrote no JSONL, malformed lines) so the caller
    degrades to "no execution link" rather than crashing.

    See ``docs/design/HARMONOGRAF.md`` §2b for the session taxonomy.
    """
    try:
        jsonl = paths.runtime / "meta_loop_events.jsonl"
        if not jsonl.exists():
            return ""
        with open(jsonl, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                evt = json.loads(raw)
                if not isinstance(evt, dict):
                    continue
                sid = evt.get("session_id") or evt.get("sessionId") or ""
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
# Composite /api/state snapshot
# ---------------------------------------------------------------------------


def build_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """The full ``/api/state`` snapshot, mirroring the Rust ``Snapshot``.

    ``paused`` (the operator pause-flag presence) rides top-level too —
    a paused-but-not-running workspace has no heartbeat to carry it, so
    the state read must surface it independently.
    """
    return {
        "heartbeat": read_heartbeat_dict(paths),
        "lock": read_lock_dict(paths),
        "active_runs": read_active_runs_view(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "lineage": _read_json_value(paths.lineage),
        "epoch_id": read_current_epoch(paths),
        "epoch": build_epoch_view(paths),
        "paused": read_paused(paths),
        "generated_at": _iso(_utc_now()),
    }
