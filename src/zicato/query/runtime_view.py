"""runtime_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from zicato.query.contracts import LivenessPayload, SnapshotPayload
from zicato.query.epoch_view import build_epoch_view
from zicato.query.paths import (
    WorkspacePaths,
    _iso,
    _parse_iso,
    _read_json_value,
    _utc_now,
    read_current_epoch,
)
from zicato.runtime.lock import WorkspaceLock, is_same_process
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
    the heartbeat was rewritten, which is the staleness signal this needs.
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
    structure record — the live figures read the model and never re-derive
    it. The Rust supervisor's ``read_active_tournament`` applies
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


def read_active_runs_view(
    paths: WorkspacePaths, *, now: _dt.datetime | None = None
) -> list[dict[str, Any]]:
    """``active_runs/*.json`` enriched with computed deadline progress.

    Each row inlines every on-disk ``ActiveRun`` field and adds
    ``progress`` (deadline fraction), ``elapsed_seconds`` and
    ``budget_seconds`` — exactly what ``/api/active-runs`` returns from
    the Rust ``read_active_runs_view``.

    Each row also carries the SERVED in-flight verdict ``fresh``
    (:func:`is_run_fresh`): whether this record is one the server counts as
    still beating. Both gates are decided here because only the server can
    run the second one — a browser is never the worker's host, so a client
    that ages rows by timestamp alone keeps counting a record whose worker
    is provably gone until the staleness window expires. ``now`` pins the
    clock the row is aged against (defaults to the read's own wall clock);
    passing it makes a fixture's verdict deterministic.

    ``adk_session_id`` is intentionally NOT read here: opening
    ``events.jsonl`` files in this hot path (called from
    ``build_snapshot`` on every SSE connection) triggers the filesystem
    watchdog and emits a spurious ``run_log`` event, breaking SSE
    ordering invariants.  For completed runs the ``adk_session_id`` is
    available via ``build_matchup_detail`` → ``ab_grid`` cells (the
    reducer persists it in ``loss.json``).
    """
    now = now or _utc_now()
    host_local = _reader_shares_worker_host(paths)
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
        # THE ONE ageable timestamp per run, ms-epoch — the same discipline the
        # heartbeat's `ts` follows. These records OUTLIVE the process that wrote
        # them, so a consumer that counts them without ageing them reports seven
        # units in flight for a run that died in June. Derived from the per-run
        # beater's ``last_progress``, falling back to ``started_at``.
        d["last_progress_ts"] = _heartbeat_ts_ms(d.get("last_progress") or d.get("started_at"))
        d["fresh"] = is_run_fresh(d, now, host_local=host_local)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Liveness — THE one derivation every live surface reads
# ---------------------------------------------------------------------------

#: Heartbeat staleness window, seconds. :class:`~zicato.runtime.heartbeat.HeartbeatBeater`
#: bumps ``heartbeat.json`` every ~2s, so 30s is a generous multiple of the
#: cadence: long enough to ride out a slow tick, short enough that a dead
#: process stops reading live within half a minute. The frontend's
#: ``STALE_HEARTBEAT_MS`` (livestatus.js) is the same number.
STALE_HEARTBEAT_S = 30.0

#: Heartbeat-``phase`` tokens that mean "the loop is at rest". Mirrors the
#: frontend's ``IDLE_PHASES`` (livestatus.js). The empty string is NOT here:
#: an absent phase is *unknown* rather than at-rest, and a fresh heartbeat carrying
#: one belongs to a process that is plainly alive (the beater's very first
#: beat writes ``phase == ""``).
IDLE_PHASE_TOKENS = frozenset(
    {"idle", "done", "complete", "completed", "finished", "stopped", "error"}
)

#: The tri-state vocabulary. ``live`` — something is running right now.
#: ``settled`` — the loop reached an end (terminal progress event, or a
#: heartbeat parked on an at-rest phase), including a workspace that never
#: ran. ``interrupted`` — it stopped mid-flight without ever ending.
LIVENESS_LIVE = "live"
LIVENESS_SETTLED = "settled"
LIVENESS_INTERRUPTED = "interrupted"


def is_active_phase(phase: Any) -> bool:
    """Whether a heartbeat ``phase`` string names in-flight work.

    The phase is a colon path (``tournament:round_0:rung0_m3``,
    ``evolve_n_rounds:done``) and the terminal token can land in ANY
    segment, so a phase is at-rest when any segment is an idle token.
    An empty / absent phase is not active (it carries no claim).
    """
    p = str(phase or "").strip().lower()
    if not p:
        return False
    return not any(seg in IDLE_PHASE_TOKENS for seg in p.split(":"))


def _is_fresh(stamp: Any, now: _dt.datetime) -> bool:
    """Whether an ISO stamp sits within :data:`STALE_HEARTBEAT_S` of ``now``.

    An unparseable / absent stamp is NOT fresh — a record with no ageable
    timestamp must never default to live. A stamp slightly in the future
    (clock skew between writer and reader) reads fresh.
    """
    ts = _parse_iso(stamp)
    if ts is None:
        return False
    return (now - ts).total_seconds() <= STALE_HEARTBEAT_S


def _reader_shares_worker_host(paths: WorkspacePaths | None) -> bool:
    """Whether this reader runs on the host the ``active_runs`` pids name.

    An :class:`~zicato.runtime.state.ActiveRun` records a ``pid`` but no
    host, so reading anything into that pid is only sound when the reader
    and the worker share a machine. The workspace runtime lock supplies
    the proof: the orchestrator that spawns every worker holds the lock on
    its own host and stamps its ``pid`` plus the ``start_time`` identity
    token. When that exact process is live *here*, this reader is on the
    orchestrator's host and the worker pids denote local processes.

    ``False`` whenever host-locality cannot be proven, which is the only safe
    default. Three cases cannot prove it: no lock file at all (a workspace at
    rest), a lock that is unreadable or carries no start-time token, and a lock
    whose owner is not a live local process. The last covers a workspace synced
    or copied to another machine, where the recorded pids mean nothing locally.
    """
    if paths is None:
        return False
    raw = read_lock_dict(paths)
    if not isinstance(raw, dict):
        return False
    try:
        lock = WorkspaceLock.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return False
    if lock.start_time is None:
        return False
    return is_same_process(lock.pid, lock.start_time)


def _is_provably_dead(run: dict[str, Any], *, host_local: bool) -> bool:
    """Whether ``run``'s worker process is KNOWN to be gone.

    Ground truth rather than a proxy: the record carries the worker's
    ``pid`` and the ``pid_start_time`` token that defeats pid reuse, so
    :func:`~zicato.runtime.lock.is_same_process` answers directly. Only
    ever ``True`` when death is provable — off-host the pids are
    meaningless, and a record missing either identity field (a producer
    from before the worker stamped them, or a platform with no readable
    start time) cannot be judged at all.
    """
    if not host_local:
        return False
    pid = run.get("pid")
    start = run.get("pid_start_time")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not isinstance(start, int | float) or isinstance(start, bool):
        return False
    return not is_same_process(pid, float(start))


def is_run_fresh(run: dict[str, Any], now: _dt.datetime, *, host_local: bool) -> bool:
    """Whether ONE ``active_runs`` record is still in flight.

    THE per-record rule :func:`fresh_run_count` tallies and
    :func:`read_active_runs_view` stamps onto each served row, so the count
    and the per-row verdict can never disagree. Both gates, in order:

    1. **Timestamp** — the per-run beater's ``last_progress`` (falling back
       to ``started_at``) is within :data:`STALE_HEARTBEAT_S` of ``now``.
    2. **Process identity** — the record's worker is not *provably* dead
       (:func:`_is_provably_dead`), which only ever TIGHTENS the first.

    ``host_local`` is the proof that the recorded pids denote local
    processes (:func:`_reader_shares_worker_host`); ``False`` leaves the
    staleness window standing alone, which is the correct reading on a
    workspace copied from another machine.
    """
    return _is_fresh(run.get("last_progress") or run.get("started_at"), now) and not (
        _is_provably_dead(run, host_local=host_local)
    )


def fresh_run_count(
    runs: list[dict[str, Any]],
    now: _dt.datetime | None = None,
    *,
    paths: WorkspacePaths | None = None,
) -> int:
    """In-flight records still BEATING — not the record count on disk.

    ``active_runs/*.json`` outlives the process that wrote it (the file is
    removed on a clean run-end, so a killed worker's record lingers), so the
    count of files is not a count of workers. A record counts iff it passes
    both of :func:`is_run_fresh`'s gates.

    The identity gate needs ``paths`` — it proves host-locality off the
    workspace lock — and is unknowable without it, as it is on a workspace
    read from another machine. There the timestamp rule stands alone.

    The client's ``freshRunCount`` (``livestatus.js``) reads the per-row
    ``fresh`` verdict this rule stamps on every served row rather than
    re-deriving it, because a browser can never run the identity gate; it
    falls back to ageing the timestamps only against a server that sends no
    verdict. That fallback carries one deliberate divergence: an
    untimestamped record counts as NOT fresh here (``_is_fresh``'s rule)
    while the client's fallback keeps it, because the real producer always
    stamps ``started_at`` and every Python reader of this tally runs on one
    server and must agree.

    Reaping here is READ-side: the record leaves the tally and the file
    stays. Removing it belongs to the writer and the supervisor; a read
    must not mutate the workspace.
    """
    now = now or _utc_now()
    host_local = _reader_shares_worker_host(paths)
    return sum(1 for r in runs if is_run_fresh(r, now, host_local=host_local))


def _on_disk_heartbeat(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The RAW heartbeat record, or ``None`` when the file is absent.

    Deliberately NOT :func:`read_heartbeat_dict`: that reader synthesizes a
    heartbeat for a post-mortem workspace with a persistent harmonograf,
    stamped with the current time — reading liveness off it would make a
    dead workspace pulse forever. Liveness must key on what the
    orchestrator actually wrote. An unparseable ``last_heartbeat`` falls
    back to the file's mtime, the freshest moment the record was rewritten.
    """
    try:
        hb = read_heartbeat(paths.root)
    except Exception:  # noqa: BLE001 — best-effort
        return None
    if hb is None:
        return None
    out = hb.to_dict()
    if _parse_iso(out.get("last_heartbeat")) is None:
        out["last_heartbeat"] = _heartbeat_file_mtime_iso(paths)
    return out


def _progress_tail(paths: WorkspacePaths) -> tuple[bool, bool, str | None]:
    """``(has_tail, is_terminal, ts)`` of the orchestrator progress log.

    The terminal marker is the AUTHORITATIVE end-of-loop signal: the evolve
    loop appends ``Settled`` / ``Stopped`` on a clean end. Degrades to
    ``(False, False, None)`` for an absent log (a workspace written before
    the progress log existed) so the phase fallback below decides instead.
    """
    try:
        from zicato.runtime import progress_log  # noqa: PLC0415

        tail = progress_log.tail(paths.root)
        if tail is None:
            return (False, False, None)
        ts = tail.ts if isinstance(tail.ts, str) and tail.ts else None
        return (True, progress_log.is_terminal(tail.type), ts)
    except Exception:  # noqa: BLE001 — best-effort
        return (False, False, None)


def derive_liveness(paths: WorkspacePaths, *, now: _dt.datetime | None = None) -> LivenessPayload:
    """THE liveness verdict, including the live epoch when known.

    One derivation, read by every live surface, so "is anything running?"
    has exactly one answer. Liveness is a property of the CLOCK rather than
    of file presence: a workspace whose runtime files froze in June still has
    a ``phase``, an ``active_tournament.json`` reading ``running`` and
    seven ``active_runs`` records — none of which mean anything is running.

    The rules, in order:

    1. **settled** — the progress log's tail is a terminal event, or the
       heartbeat is parked on an at-rest phase (``…:done``). The loop
       reached an end; ``ended_at`` names when. A workspace that never ran
       (no heartbeat, no runs, no progress log) settles too, with no
       timestamps to report.
    2. **live** — something is pulsing within :data:`STALE_HEARTBEAT_S`:
       the orchestrator heartbeat, or any in-flight run's ``last_progress``
       (the per-run beaters bump those independently, so a worker keeps the
       verdict live through a wedged orchestrator beat).
    3. **interrupted** — everything else: it stopped mid-flight and never
       recorded an end. ``ended_at`` is the last moment it was seen alive.

    Keys are omit-when-absent and additive; a consumer that does not know
    the block degrades to whatever it read before.
    """
    now = now or _utc_now()
    hb = _on_disk_heartbeat(paths)
    try:
        runs = read_active_runs_view(paths)
    except Exception:  # noqa: BLE001 — best-effort
        runs = []
    has_tail, terminal, tail_ts = _progress_tail(paths)

    last_heartbeat = hb.get("last_heartbeat") if hb is not None else None
    phase = hb.get("phase") if hb is not None else None
    # A heartbeat parked on an at-rest phase is how a workspace written
    # before the progress log existed records a clean end.
    at_rest = hb is not None and bool(str(phase or "").strip()) and not is_active_phase(phase)

    pulse = _is_fresh(last_heartbeat, now) or fresh_run_count(runs, now, paths=paths) > 0

    if terminal or at_rest:
        state = LIVENESS_SETTLED
        ended_at = tail_ts if terminal else last_heartbeat
    elif pulse:
        state = LIVENESS_LIVE
        ended_at = None
    elif hb is None and not runs and not has_tail:
        # Nothing was ever written here — at rest, with nothing to report.
        state = LIVENESS_SETTLED
        ended_at = None
    else:
        state = LIVENESS_INTERRUPTED
        ended_at = last_heartbeat or tail_ts

    out: LivenessPayload = {"state": state}
    if isinstance(last_heartbeat, str) and last_heartbeat:
        out["last_heartbeat"] = last_heartbeat
    if isinstance(ended_at, str) and ended_at:
        out["ended_at"] = ended_at
    if state == LIVENESS_LIVE:
        tournament = read_active_tournament_dict(paths)
        epoch_id = (tournament.get("epoch_id") if isinstance(tournament, dict) else None) or (
            hb.get("epoch_id") if hb else None
        )
        if isinstance(epoch_id, str) and epoch_id:
            out["epoch_id"] = epoch_id
    return out


# ---------------------------------------------------------------------------
# The run's effective settings
# ---------------------------------------------------------------------------


def read_effective_settings(paths: WorkspacePaths) -> dict[str, Any] | None:
    """Every setting the run is operating under — ``GET /api/config``.

    A knob's value is resolved from an order of priority: the dataclass
    field defaults, the workspace ``config.json``, the flags a command
    pinned for the process, and the host's usable CPU count. Only the
    process that resolves it sees the answer, so a reader that went back to
    ``config.json`` could read a different value than the loop is running
    under, and a ceiling nobody chose would be indistinguishable from one an
    operator picked.

    The loop stamps the whole resolved map onto its heartbeat record
    (:func:`zicato.runtime.effective_settings.effective_settings`), and this
    reader serves it::

        {
          "recorded_at": "2026-08-30T11:02:07Z",   # when the record was written
          "pid": 41231,
          "instance_id": "default",
          "settings": {
            "runtime.parallelism": {"value": 2, "source": "pinned CLI flag"},
            ...
          }
        }

    ``settings`` is an OPEN map keyed by each knob's dotted configuration
    name: a knob added later appears with no schema change, and a reader
    that does not recognise it still renders its ``value`` and ``source``.
    A record written before the map existed, and any process that never
    resolved a runtime configuration, carry an empty map — the record is
    served as it stands rather than withheld, because ``pid`` and
    ``recorded_at`` still answer which process this is.

    ``None`` when the workspace holds no heartbeat record at all: there is
    no run whose settings could be reported, and the client paints the
    honest empty state. The Rust supervisor does not serve this route and
    answers the same ``null`` (09-dashboard-and-query.md, the
    null-degradation duty).

    Reads the RAW record rather than :func:`read_heartbeat_dict`, whose
    synthetic post-mortem heartbeat carries harmonograf fields and no
    process identity; the settings of a run that never ran are not a thing
    to report.
    """
    try:
        hb = read_heartbeat(paths.root)
    except Exception:  # noqa: BLE001 — best-effort, mirrors the sibling readers
        return None
    if hb is None:
        return None
    return {
        "recorded_at": hb.last_heartbeat,
        "pid": hb.pid,
        "instance_id": hb.instance_id,
        "settings": {name: dict(entry) for name, entry in hb.settings.items()},
    }


# ---------------------------------------------------------------------------
# Composite /api/state snapshot
# ---------------------------------------------------------------------------


def build_snapshot(paths: WorkspacePaths) -> SnapshotPayload:
    """The full ``/api/state`` snapshot, mirroring the Rust ``Snapshot``.

    ``paused`` (the operator pause-flag presence) rides top-level too —
    a paused-but-not-running workspace has no heartbeat to carry it, so
    the state read must surface it independently.

    ``liveness`` is the served tri-state (:func:`derive_liveness`) — the
    one answer every live surface reads instead of re-deriving "is
    anything running?" from raw file presence.
    """
    return {
        "heartbeat": read_heartbeat_dict(paths),
        "liveness": derive_liveness(paths),
        "lock": read_lock_dict(paths),
        "active_runs": read_active_runs_view(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "lineage": _read_json_value(paths.lineage),
        "epoch_id": read_current_epoch(paths),
        "epoch": build_epoch_view(paths),
        "paused": read_paused(paths),
        "generated_at": _iso(_utc_now()),
    }
