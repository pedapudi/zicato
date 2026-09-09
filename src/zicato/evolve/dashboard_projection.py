"""Live tournament projections and their round and standings serializers.

The evolve loop publishes live envelopes, overlays run progress, and opens
in-progress field snapshots through the tournament record owner. Resolved
settlement receipts authorize the transition to a settled durable snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.runtime.lock import WorkspaceLock
from zicato.tournament.records import field_tournament_record, write_field_tournament_record

log = logging.getLogger("zicato.orchestrator")


def _publish_active_tournament(
    writer: WorkspaceLock,
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
    is non-empty while the run is live rather than only after it settles.
    When omitted (for example the all-rejected publish) it falls back to an
    empty list.

    Never raises — a live-state write failure must not abort the round.
    """
    workspace_root = writer.workspace_root
    try:
        from zicato.evolve.lifecycle_services import _now_iso  # noqa: PLC0415
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            read_active_tournament,
            write_active_tournament,
        )

        # A strategy republish retains results from this tournament's running matchups.
        prior = read_active_tournament(workspace_root)
        if prior is not None and prior.tournament_id != tournament_id:
            prior = None
        if not standings and phase == "running":
            standings = [
                {
                    "generation_id": c["generation_id"],
                    "role": c.get("role", "challenger"),
                    "rank": i,
                    "scalar": None,
                    "wins": 0,
                    "losses": 0,
                    "status": "competing",
                }
                for i, c in enumerate(competitors, 1)
            ]
        projected = dict(prior.projected) if prior is not None else {}
        partial_champion = dict(prior.partial_champion_agg) if prior is not None else {}
        partial_challenger = dict(prior.partial_challenger_agg) if prior is not None else {}

        write_active_tournament(
            writer,
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
            # the "projected" treatment (dashed, ~ prefix, a scored sub-bar)
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


def _open_field_tournament(
    workspace_root: Path,
    *,
    field_tournament_id: str,
    first_challenger_id: str,
    epoch_id: str,
    structure: str,
    structure_params: dict[str, Any],
    competitors: list[dict[str, Any]],
    field_status: list[dict[str, Any]],
) -> None:
    """Best-effort: publish a field record in ``in_progress`` state.

    The replayable settlement commit owns the only transition to ``settled``.
    This writer exposes the competitors and proposal status while execution is
    active. A two-competitor gauntlet needs no separate field record.
    """
    from zicato.evolve.lifecycle_services import _now_iso  # noqa: PLC0415

    record = field_tournament_record(
        field_tournament_id=field_tournament_id,
        epoch_id=epoch_id,
        structure=structure,
        structure_params=structure_params,
        competitors=competitors,
        rounds=[],
        standings=[],
        field_status=field_status,
        decision=None,
        ran_at=_now_iso(),
        state="in_progress",
    )
    if record is None:
        return
    try:
        write_field_tournament_record(
            workspace_root,
            epoch_id=epoch_id,
            first_challenger_id=first_challenger_id,
            record=record,
        )
    except Exception as exc:  # noqa: BLE001 — durable snapshot is best-effort here
        log.debug("field-tournament snapshot skipped: %s", exc)
        return
    _ingest_field_tournament_record(workspace_root, record.to_dict())


def _ingest_field_tournament_record(
    workspace_root: Path,
    record: dict[str, Any],
) -> None:
    """Best-effort refresh of one derived field-tournament index row."""
    try:
        from zicato.evolve.ingest import _index_db_path  # noqa: PLC0415
        from zicato.index.ingest import ingest_field_tournament  # noqa: PLC0415

        ingest_field_tournament(workspace_root, _index_db_path(workspace_root), record)
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping field-tournament dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug("field-tournament index dual-write skipped: %s", exc)


def _settle_active_tournament(
    writer: WorkspaceLock,
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
        from zicato.evolve.lifecycle_services import _now_iso  # noqa: PLC0415
        from zicato.runtime.state import (  # noqa: PLC0415
            ActiveTournament,
            TournamentPhase,
            write_active_tournament,
        )

        rounds = _serialise_rounds(strategy.rounds())
        standings = _serialise_standings(decision.standings)
        write_active_tournament(
            writer,
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
                # until the next round) carries the per-entry rung rather than
                # an empty list. Mirrors the live-publish path.
                entries=_field_entries(competitors, standings),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament settle skipped: %s", exc)


def _clear_active_tournament(writer: WorkspaceLock) -> None:
    """Best-effort: clear the live ActiveTournament record. Never raises."""
    try:
        from zicato.runtime.state import clear_active_tournament  # noqa: PLC0415

        clear_active_tournament(writer)
    except Exception as exc:  # noqa: BLE001 — live state is best-effort
        log.debug("active-tournament clear skipped: %s", exc)


def _mark_run_terminal(writer: WorkspaceLock) -> None:
    """Best-effort: mark a cleanly-ended run terminal so it never reads LIVE.

    A normally-ended evolve loop already stamps a terminal heartbeat phase
    (``evolve_n_rounds:done``), which the dashboard treats as idle. But the
    runtime ``active_tournament.events.jsonl`` envelope can linger with
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
    workspace_root = writer.workspace_root
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
            write_active_tournament(writer, replace(current, phase=TournamentPhase.STOPPED))
    except Exception as exc:  # noqa: BLE001 — terminal-state write is best-effort
        log.debug("terminal active-tournament mark skipped: %s", exc)
