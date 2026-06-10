"""Dashboard live-state projection for the evolve loop.

Pure presentation code moved verbatim from :mod:`zicato.orchestrator`: the
``active_tournament`` live-state envelope writers, the canonical round /
standings serialisers, the runner-projected overlays, and the durable
field-tournament snapshot. The orchestrator re-imports these by name, so every
call site is unchanged and the live-state JSON the dashboard reads is
byte-identical.

The two helpers these functions share with the orchestrator -- ``_now_iso``
and ``_index_db_path`` -- remain defined in :mod:`zicato.orchestrator` (they
are used broadly there) and are imported lazily inside the functions that need
them, matching the deferred-import style already used throughout this block and
avoiding an import cycle at module load.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

log = logging.getLogger("zicato.orchestrator")


def _publish_active_tournament(
    workspace_root: Path,
    *,
    tournament_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    round_index: int,
    total_rounds: int,
    field_status: list[dict[str, Any]] | None = None,
    phase: str = "running",
    rounds: list[dict[str, Any]] | None = None,
    standings: list[dict[str, Any]] | None = None,
    entries: list[Any] | None = None,
) -> None:
    """Best-effort: publish the live ActiveTournament envelope.

    Populates the structure envelope (``structure`` / ``structure_params``
    / ``competitors``) per the data-model doc so the dashboard can render
    a non-gauntlet field while it runs. ``parent_generation_id`` /
    ``child_generation_id`` are left empty for non-gauntlet structures
    (the data model's documented convention); ``competitors`` is the
    authoritative field.

    ``rounds`` / ``standings`` carry the live (in-flight) structure when
    supplied — the settled rounds PLUS the current scheduled round (its
    matches with ``winner: null`` + ``pending: true``) and the
    standings-so-far, both serialised through the SAME
    :func:`_serialise_rounds` / :func:`_serialise_standings` path the
    settle + durable-record producers use, so the dashboard's
    bracket/ladder/funnel renderers work identically live and post-run.
    Omitting them (e.g. the all-rejected publish, which has no field to
    seed) leaves the envelope's ``rounds`` / ``standings`` empty.

    ``entries`` is the per-entry funnel field
    (:class:`~zicato.runtime.state.ActiveTournamentEntry`). The gauntlet
    A/B path seeds it directly; the multi-challenger / racing path builds
    it via :func:`_field_entries` so the dashboard's per-entry funnel rung
    is non-empty live, not just after the run settles. When omitted (e.g.
    the all-rejected publish) it falls back to an empty list, matching the
    pre-existing behaviour.

    Never raises — a live-state write failure must not abort the round.
    """
    try:
        from zicato.orchestrator import _now_iso  # noqa: PLC0415
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            read_active_tournament,
            write_active_tournament,
        )

        # ``phase`` may arrive as the bare default ``"running"`` sentinel
        # below or as a :class:`~zicato.runtime.state.TournamentPhase`
        # member from a caller; both serialise to the same wire token.
        # PRESERVE the runner-written live fields across a republish. The
        # runner rewrites ``projected`` / ``partial_*_agg`` per settled board
        # via its OWN read-modify-write; this full envelope republish (one per
        # scheduled batch) would otherwise clobber them back to empty, killing
        # the live projected standing. Carry them forward from the on-disk
        # record so the two writers compose instead of racing to zero.
        prior = read_active_tournament(workspace_root)
        projected = dict(prior.projected) if prior is not None else {}
        partial_champion = dict(prior.partial_champion_agg) if prior is not None else {}
        partial_challenger = dict(prior.partial_challenger_agg) if prior is not None else {}

        write_active_tournament(
            workspace_root,
            ActiveTournament(
                tournament_id=tournament_id,
                parent_generation_id="",
                child_generation_id="",
                epoch_id=epoch_id,
                started_at=_now_iso(),
                phase=phase,
                round_index=round_index,
                total_rounds=total_rounds,
                structure=structure,
                structure_params=dict(structure_params),
                competitors=[dict(c) for c in competitors],
                field_status=[dict(f) for f in (field_status or [])],
                rounds=[dict(r) for r in (rounds or [])],
                standings=[dict(s) for s in (standings or [])],
                entries=list(entries or []),
                projected=projected,
                partial_champion_agg=partial_champion,
                partial_challenger_agg=partial_challenger,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament publish skipped: %s", exc)


def _field_entries(
    competitors: list[dict[str, Any]],
    standings: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Build the per-competitor funnel ``entries`` for a non-gauntlet field.

    The gauntlet A/B path seeds :attr:`ActiveTournament.entries` with one
    :class:`~zicato.runtime.state.ActiveTournamentEntry` per board entry
    per side; the dashboard funnel groups by ``side`` to draw rungs. The
    racing / multi-challenger live-publish path historically left
    ``entries`` empty, so the live funnel could not render the per-entry
    rung field for a field tournament until the run settled. This helper
    closes that gap by emitting one row per competitor, mirroring the A/B
    shape: ``side`` carries the competitor's role (``champion`` /
    ``challenger``) — the data-model §2.3 convention for non-gauntlet
    structures — and ``entry_id`` carries its generation id.

    When live ``standings`` are supplied each row's ``status`` and
    ``loss_summary`` are derived from the matching standing (status maps
    alive/competing → ``running``, eliminated/champion → ``completed``; the
    Copeland ``scalar`` rides in ``loss_summary`` so the funnel can colour
    a rung by relative loss). Absent standings (the pre-schedule publish)
    every row is ``queued`` with an empty loss summary.
    """
    from zicato.runtime.state import ActiveTournamentEntry, RunStatus  # noqa: PLC0415

    by_gen: dict[str, dict[str, Any]] = {}
    for s in standings or []:
        gid = str(s.get("generation_id", ""))
        if gid:
            by_gen[gid] = s

    entries: list[Any] = []
    for c in competitors:
        gid = str(c.get("generation_id", ""))
        role = str(c.get("role", "") or "")
        standing = by_gen.get(gid)
        status: RunStatus
        if standing is None:
            status = RunStatus.QUEUED
            loss_summary: dict[str, float] = {}
        else:
            raw_status = str(standing.get("status", "") or "")
            status = (
                RunStatus.COMPLETED
                if raw_status in ("eliminated", "champion")
                else RunStatus.RUNNING
            )
            scalar = standing.get("scalar")
            loss_summary = {"scalar": float(scalar)} if isinstance(scalar, int | float) else {}
            # An IN-FLIGHT competitor carries a live PROJECTED scalar (the
            # runner's running aggregate over boards-so-far). Surface it +
            # the boards progress on the funnel entry so the rung can render
            # the "projected" treatment (dashed, ~prefix, scored sub-bar)
            # before a settled scalar lands.
            if standing.get("in_flight"):
                proj = standing.get("projected_scalar")
                if isinstance(proj, int | float):
                    loss_summary["projected_scalar"] = float(proj)
                loss_summary["in_flight"] = 1.0
                bd = standing.get("boards_done")
                bt = standing.get("boards_total")
                if isinstance(bd, int | float):
                    loss_summary["boards_done"] = float(bd)
                if isinstance(bt, int | float):
                    loss_summary["boards_total"] = float(bt)
        entries.append(
            ActiveTournamentEntry(
                entry_id=gid,
                # Non-gauntlet structures carry the competitor role as the
                # ``side`` key (data-model §2.3); the funnel groups on it.
                side=role or gid,
                status=status,
                loss_summary=loss_summary,
            )
        )
    return entries


def _serialise_rounds(rounds: Any) -> list[dict[str, Any]]:
    """Project a sequence of :class:`RoundRecord` to the dashboard shape.

    The single canonical serialisation of the round-by-round pairings
    (data-model §2.4), shared by ALL THREE producers so they carry
    byte-identical shapes and the dashboard renderers work identically:

    * the LIVE ``active_tournament`` envelope (``strategy.live_rounds()``
      — settled rounds plus the in-flight round whose matches carry
      ``winner: null`` + ``pending: true``);
    * the SETTLED envelope at run end (``strategy.rounds()``);
    * the DURABLE field-tournament snapshot (``strategy.rounds()``).

    ``winner`` is emitted as ``None`` (the contract's ``winner: null``)
    for a match that has not crowned a side — every ``pending`` match, and
    any settled match the strategy left with an empty ``winner`` (a racing
    rung, which cuts rather than crowns).
    """
    return [
        {
            "stage_index": r.stage_index,
            "label": r.label,
            "matches": [
                {
                    "match_id": m.match_id,
                    "competitors": list(m.competitors),
                    "winner": (m.winner or None),
                    "decision": m.decision,
                    "delta_scalar": m.delta_scalar,
                    "bracket_slot": m.bracket_slot,
                    "bye": m.bye,
                    "survivors": list(m.survivors),
                    "cut": list(m.cut),
                    "board_fraction": m.board_fraction,
                    "pending": m.pending,
                    "live_progress": {
                        str(gid): dict(lane) for gid, lane in (m.live_progress or {}).items()
                    },
                }
                for m in r.matches
            ],
        }
        for r in rounds
    ]


def _overlay_projected_live_progress(
    rounds: list[dict[str, Any]],
    workspace_root: Path,
) -> None:
    """Fold the runner's per-board ``projected`` into the rung ``live_progress``.

    The racing strategy publishes the in-flight rung's per-lane
    ``live_progress`` TOPOLOGY (which lanes are racing, each lane's
    ``boards_total`` = the rung's board-slice size, the ``inflight`` flag,
    and the lane's last-known running scalar vs the champion). The runner's
    :class:`_IncrementalScorer` writes the live per-board ``projected`` map
    (``{generation_id: {scalar, boards_done, boards_total, pass_rate}}``)
    onto :attr:`ActiveTournament.projected` as each board unit settles. This
    overlays the latter onto the former IN PLACE so each rung lane carries
    one authoritative progress row the dashboard consumes directly:

    * ``boards_done`` — from the runner's projected row (the strategy can't
      know mid-duel board progress).
    * ``projected_scalar`` / ``projected`` — refreshed from the runner's
      LIVE running aggregate when present (more current than the strategy's
      last-rung scalar). The strategy's seeded scalar (a prior rung's
      result) stays as the fallback when no live projected row exists yet.
    * ``boards_total`` — kept from the strategy's authoritative rung-slice
      size; only filled from the projected row when the strategy left it
      unknown (whole-board fallback construction).

    The separation of concerns is preserved: the STRATEGY owns the
    ``live_progress`` topology (it is written into the serialised rounds),
    the SCORER owns the ``projected`` scalars (read here from the on-disk
    state). Best-effort — a missing / unreadable projected map leaves the
    strategy-published ``live_progress`` untouched. Mutates ``rounds`` in
    place; a no-op for any round whose matches carry no ``live_progress``
    (every non-racing structure, and a racing rung before it is scheduled).
    """
    has_progress = any(m.get("live_progress") for r in rounds for m in (r.get("matches") or []))
    if not has_progress:
        return
    try:
        from zicato.runtime.state import read_active_tournament  # noqa: PLC0415

        active = read_active_tournament(workspace_root)
    except Exception:  # noqa: BLE001 — overlay is best-effort
        active = None
    projected = dict(active.projected) if active is not None else {}
    if not projected:
        return
    for r in rounds:
        for m in r.get("matches") or []:
            lanes = m.get("live_progress")
            if not lanes:
                continue
            for gid, lane in lanes.items():
                proj = projected.get(str(gid))
                if not isinstance(proj, dict):
                    continue
                if "boards_done" in proj:
                    lane["boards_done"] = int(proj["boards_done"])
                if "boards_total" in proj and "boards_total" not in lane:
                    lane["boards_total"] = int(proj["boards_total"])
                if "scalar" in proj:
                    lane["projected_scalar"] = float(proj["scalar"])
                    lane["projected"] = True


def _serialise_standings(standings: Any) -> list[dict[str, Any]]:
    """Project a sequence of :class:`Standing` to the dashboard shape.

    The single canonical serialisation of the Copeland standings
    (data-model §2.5), shared by the live envelope
    (``strategy.live_standings()``), the settled envelope, and the durable
    field-tournament snapshot (both ``decision.standings``). The live
    ``status`` is emitted raw (alive / eliminated / champion / competing);
    the dashboard maps display labels.
    """
    return [
        {
            "generation_id": s.generation_id,
            "rank": s.rank,
            "scalar": s.scalar,
            "wins": s.wins,
            "losses": s.losses,
            "status": s.status,
            "role": s.role,
        }
        for s in standings
    ]


def _overlay_projected_standings(
    standings: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    workspace_root: Path,
    structure: str,
) -> list[dict[str, Any]]:
    """Fold the runner's live PROJECTED standing onto the standings rows.

    Reads :attr:`ActiveTournament.projected` (the runner's per-board
    ``{generation_id: {scalar, boards_done, boards_total, pass_rate}}`` map)
    and overlays it onto the matching standing rows for the competitors that
    are IN FLIGHT — i.e. those appearing in a still-pending match of the
    current live round. Each touched row gains ``projected_scalar``,
    ``in_flight=True``, ``boards_done`` and ``boards_total``; settled rows
    are left untouched (no ``in_flight`` key, original scalar).

    Per-structure ranking rule (substitute the projected scalar into the
    EXISTING sort key for the in-flight competitor ONLY):

    * ``single_elim`` / ``double_elim`` / ``racing`` — scalar rank. The
      projected scalar replaces the row's (still-zero) scalar in the sort
      key, so an in-flight leader bubbles up live; settled rows keep their
      real scalar.
    * ``swiss`` — Copeland points are NEVER projected (a half-finished duel
      has no win). The points-based rank is preserved exactly; the projected
      scalar only nudges the MEAN-SCALAR TIEBREAK among rows on equal points,
      and the pairing is marked in-flight visually. Never re-ranks on points.
    * ``gauntlet`` — not routed here (no multi-competitor standings).

    Best-effort: a missing / unreadable projected map yields the input
    standings unchanged.
    """
    if not standings:
        return standings
    try:
        from zicato.runtime.state import read_active_tournament  # noqa: PLC0415

        active = read_active_tournament(workspace_root)
    except Exception:  # noqa: BLE001 — overlay is best-effort
        active = None
    projected = dict(active.projected) if active is not None else {}
    if not projected:
        return standings

    # The IN-FLIGHT competitor set: every generation in a pending (unresolved)
    # match of the live rounds. A row is only projected when its competitor is
    # actually running right now — a stale projected row for a competitor whose
    # match already settled must not override its real scalar.
    in_flight: set[str] = set()
    for r in rounds:
        for m in r.get("matches", []) or []:
            if not m.get("pending"):
                continue
            for g in m.get("competitors", []) or []:
                if g:
                    in_flight.add(str(g))

    out: list[dict[str, Any]] = []
    for s in standings:
        row = dict(s)
        gid = str(row.get("generation_id", ""))
        proj = projected.get(gid)
        if gid and gid in in_flight and isinstance(proj, dict) and "scalar" in proj:
            row["in_flight"] = True
            row["projected_scalar"] = float(proj["scalar"])
            if "boards_done" in proj:
                row["boards_done"] = int(proj["boards_done"])
            if "boards_total" in proj:
                row["boards_total"] = int(proj["boards_total"])
        out.append(row)

    def _scalar_key(row: dict[str, Any]) -> float:
        # In-flight rows sort on the projected scalar (lower is better);
        # settled rows keep their real scalar.
        if row.get("in_flight") and isinstance(row.get("projected_scalar"), int | float):
            return float(row["projected_scalar"])
        sc = row.get("scalar")
        return float(sc) if isinstance(sc, int | float) else float("inf")

    if structure in ("single_elim", "double_elim", "racing"):
        out.sort(key=_scalar_key)
        for i, row in enumerate(out, start=1):
            row["rank"] = i
    elif structure == "swiss":
        # Points-rank is authoritative; the projected scalar only breaks ties
        # among rows on EQUAL wins (the mean-scalar tiebreak). Never re-rank on
        # points — a half-finished duel has crowned no winner.
        def _swiss_key(row: dict[str, Any]) -> tuple[int, float]:
            wins = row.get("wins")
            w = int(wins) if isinstance(wins, int) else 0
            return (-w, _scalar_key(row))

        out.sort(key=_swiss_key)
        for i, row in enumerate(out, start=1):
            row["rank"] = i
    # Any other structure: overlay the markers but leave the published order.
    return out


def _persist_field_tournament(
    workspace_root: Path,
    *,
    field_tournament_id: str,
    first_challenger_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    standings: list[dict[str, Any]],
    field_status: list[dict[str, Any]],
    decision: Any,
    state: str = "settled",
) -> None:
    """Best-effort: durably persist a FIELD-level structure record.

    Writes the round's field record — round pairings, Copeland
    standings, competitors, proposing field-status, the crowning verdict —
    to its durable ``tournaments/field-*.json`` snapshot AND dual-writes it
    into the analytical index as ONE field-level ``tournaments`` row. The
    snapshot is the canonical source (so ``zicato reindex`` re-derives the
    row); the dual-write puts the swiss / elim ladder in the index.

    ``state`` carries the explicit ``in_progress`` → ``settled`` lifecycle
    (issue #16): the orchestrator OPENS the envelope at round start
    (``state="in_progress"``, ``decision=None``, empty rounds/standings —
    just the competitor field + proposing status so the round is visible to
    every queryable store mid-flight) and FINALISES it at settle
    (``state="settled"`` with the resolved bracket + crowning verdict). The
    record is keyed on the field-level ``tournament_id`` so the settle write
    upserts the same row the open write created — idempotent, crash-safe,
    and safe to re-open on resume (the in_progress record is simply
    overwritten by the next open or the settle).

    A no-op for a degenerate two-competitor (gauntlet) field — the
    per-challenger row already covers it. Never raises: a durable-state
    write failure must not abort the round.
    """
    if len(competitors) < 3:
        return
    from zicato.orchestrator import _index_db_path, _now_iso  # noqa: PLC0415

    crowning_delta: float | None = None
    for r in reversed(rounds):
        matches = r.get("matches") or []
        if matches:
            crowning_delta = matches[-1].get("delta_scalar")
            break
    champion_id = next(
        (c.get("generation_id") for c in competitors if str(c.get("role", "")) == "champion"),
        "",
    )
    # ``decision`` is None while the round is still in flight (the envelope
    # is opened before the bracket resolves); the crowning fields stay empty
    # until settle. getattr tolerates the None case alongside the settled
    # TournamentDecision so the open + settle writes share one code path.
    record: dict[str, Any] = {
        "tournament_id": field_tournament_id,
        "epoch_id": epoch_id,
        "structure": structure,
        "structure_params": dict(structure_params),
        "competitors": [dict(c) for c in competitors],
        "rounds": rounds,
        "standings": standings,
        "field_status": [dict(f) for f in field_status],
        "promoted_generation_id": getattr(decision, "promoted_generation_id", "") or "",
        "champion_generation_id": champion_id or "",
        "decision": getattr(decision, "decision", "") or "",
        "reason": getattr(decision, "reason", "") or "",
        "delta_scalar": crowning_delta,
        "state": state,
        "ran_at": _now_iso(),
    }
    try:
        from zicato.core.workspace import field_tournament_path  # noqa: PLC0415

        path = field_tournament_path(workspace_root, epoch_id, first_challenger_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — durable snapshot is best-effort
        log.debug("field-tournament snapshot skipped: %s", exc)
        return
    try:
        from zicato.index.ingest import ingest_field_tournament  # noqa: PLC0415

        ingest_field_tournament(workspace_root, _index_db_path(workspace_root), record)
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping field-tournament dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug("field-tournament index dual-write skipped: %s", exc)


def _settle_active_tournament(
    workspace_root: Path,
    *,
    tournament_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    strategy: Any,
    decision: Any,
    round_index: int,
    total_rounds: int,
    field_status: list[dict[str, Any]] | None = None,
) -> None:
    """Best-effort: rewrite the live envelope with the settled bracket.

    Serializes the strategy's ``rounds()`` (data-model §2.4) and the
    decision's ``standings`` (§2.5) so the dashboard's structure reader
    sees the final bracket / leaderboard. Never raises.
    """
    try:
        from zicato.orchestrator import _now_iso  # noqa: PLC0415
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            TournamentPhase,
            write_active_tournament,
        )

        rounds = _serialise_rounds(strategy.rounds())
        standings = _serialise_standings(decision.standings)
        write_active_tournament(
            workspace_root,
            ActiveTournament(
                tournament_id=tournament_id,
                parent_generation_id="",
                child_generation_id=decision.promoted_generation_id or "",
                epoch_id=epoch_id,
                started_at=_now_iso(),
                phase=TournamentPhase.COMPLETED,
                round_index=round_index,
                total_rounds=total_rounds,
                structure=structure,
                structure_params=dict(structure_params),
                competitors=[dict(c) for c in competitors],
                rounds=rounds,
                standings=standings,
                field_status=[dict(f) for f in (field_status or [])],
                # Seed the per-competitor funnel field from the SETTLED
                # standings so the retained completed envelope (the
                # dashboard's only live source for a non-gauntlet field
                # until the next round) carries the per-entry rung, not an
                # empty list. Mirrors the live-publish path.
                entries=_field_entries(competitors, standings),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament settle skipped: %s", exc)


def _clear_active_tournament(workspace_root: Path) -> None:
    """Best-effort: clear the live ActiveTournament record. Never raises."""
    try:
        from zicato.runtime.state import clear_active_tournament  # noqa: PLC0415

        clear_active_tournament(workspace_root)
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament clear skipped: %s", exc)


def _mark_run_terminal(workspace_root: Path) -> None:
    """Best-effort: mark a cleanly-ended run terminal so it never reads LIVE.

    A normally-ended evolve loop already stamps a terminal heartbeat phase
    (``evolve_n_rounds:done``), which the dashboard treats as idle. But the
    runtime ``active_tournament.json`` envelope can linger with
    ``phase="running"`` (e.g. a mid-resolution structure whose settle write
    never ran) — and a frontend reading the heartbeat as fresh would then
    show a "LIVE" tournament on a closed epoch. As a defensive measure on
    clean shutdown we flip any lingering ``phase="running"`` envelope to a
    terminal ``"stopped"`` phase, so a normally-ended run does not read as
    live even before the heartbeat freshness window elapses. A SIGKILLed run
    cannot self-clean — that case is covered by the frontend freshness gate.
    Never raises: a teardown-time live-state write failure must not mask the
    real shutdown reason.
    """
    try:
        from zicato.runtime.state import (  # noqa: PLC0415
            TournamentPhase,
            read_active_tournament,
            write_active_tournament,
        )

        current = read_active_tournament(workspace_root)
        if current is None:
            return
        if str(current.phase).strip().lower() == "running":
            write_active_tournament(workspace_root, replace(current, phase=TournamentPhase.STOPPED))
    except Exception as exc:  # noqa: BLE001 — terminal-state write is best-effort
        log.debug("terminal active-tournament mark skipped: %s", exc)
