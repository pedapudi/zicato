"""The ORCHESTRATOR progress EVENT LOG.

The age of a heartbeat *timestamp* is not a liveness signal for this loop.
Reading ``heartbeat.json`` and treating a ``last_heartbeat`` more than a few
intervals old as a stalled orchestrator is wrong in both directions:

* **False-positive.** A single slow LLM call ages the timestamp past the
  threshold even though the loop is making genuine progress — the beater
  cannot bump while the event loop is parked in an ``await``.
* **False-negative.** A wedged loop whose beater thread keeps stamping
  ``now()`` (or whose periodic timer keeps firing) looks alive forever
  even though no real transition has happened in minutes.

This module supplies a signal that is right in both directions: a
**single-writer, append-only EVENT LOG**
(built on :class:`zicato.runtime.channel.EventLog`) that the evolve loop
appends ONE typed event to on each *genuine* orchestrator transition —
round start, propose, each settled proposal episode, tournament start, each
settled board unit, tournament settle, promote / reject. The log's
monotonic ``seq`` therefore advances only on real progress, never on a
timer, so it is the TRUE liveness signal:

* a watchdog asks "has ``seq`` advanced since I last looked?" rather than
  "is the timestamp fresh?", so a slow LLM call does not read as stalled
  (the round is simply between two transitions) and a wedged loop does not
  read as alive (``seq`` is frozen);
* a SETTLED run is distinguishable from a STALLED one because the loop
  appends a terminal :data:`SETTLED` event on a clean end — the tail
  event ``type`` names the terminal state rather than leaving the reader
  to guess from a stale timestamp.

It mirrors the tournament EventLog wiring (:mod:`zicato.runtime.tournament_log`)
exactly: the single producer appends typed deltas; a reader cursors on
``seq``. The orchestrator is the single writer (one evolve loop per
workspace, guarded by the workspace lock), which is the precondition that
makes the gap-free ``seq`` correct.

Degrading when the log is absent
--------------------------------
The log lives at its own storage key (``runtime/progress.events.jsonl``),
and touches no other file. :func:`tail_seq` reads ``0`` for an absent log,
which is also what a heartbeat carrying no ``seq`` field reads back as, so
a workspace with no progress log degrades to "no progress observed" rather
than to an error.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from pathlib import Path

from zicato.runtime._storage import progress_log_key
from zicato.runtime.channel import Event, EventLog
from zicato.runtime.lock import WorkspaceLock
from zicato.storage import workspace_backend

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transition vocabulary — the single producer + every reader agree on these.
# A transition advances ``seq`` exactly once. The token is free-form (the
# channel does not interpret it); these names the evolve loop appends.
# ---------------------------------------------------------------------------

#: Loop boot — the evolve invocation started (epoch resolved, lock held).
LOOP_START = "LoopStart"
#: A fresh evolve round began.
ROUND_START = "RoundStart"
#: The proposer minted (or attempted) a challenger this round.
PROPOSE = "Propose"
#: The tournament for this round started executing.
TOURNAMENT_START = "TournamentStart"
#: One proposal episode ended (a slate slot's sample, or a challenger's whole
#: proposal), with a candidate or an error. A proposal phase with several
#: episodes advances ``seq`` at each one.
EPISODE_SETTLED = "EpisodeSettled"
#: One board unit of a tournament finished and its losses were scored. A
#: tournament phase lasts minutes; this event advances ``seq`` while it runs.
UNIT_SETTLED = "UnitSettled"
#: The tournament settled (a winner / decision is resolved).
TOURNAMENT_SETTLE = "TournamentSettle"
#: The round's challenger was promoted to the new head.
PROMOTE = "Promote"
#: The round's challenger was rejected (champion retained).
REJECT = "Reject"

#: Terminal markers — the loop appends one on a clean end so a reader can
#: tell a SETTLED run (the work finished) from a STALLED one (``seq``
#: frozen mid-flight with no terminal event). :func:`is_terminal` /
#: :func:`tail_is_terminal` answer that question.
SETTLED = "Settled"
#: A terminal end forced by a budget / circuit-breaker cut (still a clean,
#: orchestrator-produced end — distinct from a wedge that never terminates).
STOPPED = "Stopped"

#: The set of event types that mark a terminal (cleanly-ended) loop.
_TERMINAL_TYPES = frozenset({SETTLED, STOPPED})


def is_terminal(event_type: str) -> bool:
    """Return ``True`` iff ``event_type`` marks a cleanly-ended loop.

    A terminal event distinguishes a SETTLED run (the loop reached its
    end and appended :data:`SETTLED` / :data:`STOPPED`) from a STALLED one
    (``seq`` is frozen mid-flight with the tail still a progress event).
    """
    return event_type in _TERMINAL_TYPES


def _log(workspace_root: Path) -> EventLog:
    """Bind the orchestrator progress :class:`EventLog` for a workspace."""
    return EventLog(workspace_backend(workspace_root, start=False), progress_log_key())


# ---------------------------------------------------------------------------
# Appender — the single-writer producer surface. ONE atomic append.
# ---------------------------------------------------------------------------


def append_progress(writer: WorkspaceLock, type: str, payload: object | None = None) -> int:
    """Append one progress transition and return the new tail ``seq``.

    The single producer (the evolve loop) calls this on each genuine
    transition, only through ``zicato.evolve.lifecycle_services._beat``, which
    stamps the returned ``seq`` on the heartbeat in the same step; ``seq``
    advances by exactly one per call. The returned
    ``seq`` is what the caller stamps into the heartbeat (and what the
    dashboard surfaces) — the machine-readable liveness cursor. Best-effort
    callers can ignore the return value.
    """
    return writer.progress_log.append(type, payload).seq


# ---------------------------------------------------------------------------
# Transitions inside a phase — the evolve loop binds a recorder; the
# tournament scheduler and the proposer call it.
# ---------------------------------------------------------------------------

_TransitionRecorder = Callable[[str], None]

#: The recorder the evolve loop binds for its invocation. Unbound (``None``)
#: outside a loop, so a standalone ``zicato tournament run`` appends nothing
#: and cannot turn a settled loop's terminal tail back into a live one.
_recorder: contextvars.ContextVar[_TransitionRecorder | None] = contextvars.ContextVar(
    "zicato_progress_transition_recorder", default=None
)


def bind_transition_recorder(
    recorder: _TransitionRecorder,
) -> contextvars.Token[_TransitionRecorder | None]:
    """Bind the callable that records one transition inside a phase.

    The evolve loop binds it once, beside its heartbeat beater; every asyncio
    task the loop creates afterwards inherits the binding. Pass the returned
    token to :func:`reset_transition_recorder` at teardown.
    """
    return _recorder.set(recorder)


def reset_transition_recorder(token: contextvars.Token[_TransitionRecorder | None]) -> None:
    """Undo :func:`bind_transition_recorder`. Never raises."""
    try:
        _recorder.reset(token)
    except (ValueError, LookupError) as exc:  # reset from a different context
        log.debug("progress recorder reset skipped: %s", exc)


def record_transition(transition: str) -> None:
    """Record ``transition`` through the bound recorder, if any.

    The tournament scheduler records :data:`UNIT_SETTLED` as each unit's
    losses are scored, and the proposer records :data:`EPISODE_SETTLED` as
    each proposal episode ends. A recorder failure is logged and dropped:
    liveness bookkeeping must never abort a round.
    """
    recorder = _recorder.get()
    if recorder is None:
        return
    try:
        recorder(transition)
    except Exception as exc:  # noqa: BLE001 — liveness bookkeeping is best-effort
        log.debug("progress transition record skipped: %s", exc)


def tail(workspace_root: Path) -> Event | None:
    """Return the last progress event, or ``None`` when the log is empty."""
    return _log(workspace_root).tail()


def tail_seq(workspace_root: Path) -> int:
    """Return the current tail ``seq``, or ``0`` for an absent / empty log.

    ``0`` is the safe back-compat default: a heartbeat written before this
    phase (no ``seq`` key) reads back as ``seq == 0``, and a workspace whose
    orchestrator never wrote a progress log reports ``0`` here too — neither
    can be confused with a real first transition (``seq == 1``).
    """
    last = _log(workspace_root).tail()
    return last.seq if last is not None else 0


def tail_is_terminal(workspace_root: Path) -> bool:
    """Return ``True`` iff the tail event marks a cleanly-ended loop.

    Lets a reader distinguish a SETTLED run from a STALLED one without
    re-deriving it from a (possibly stale) heartbeat timestamp.
    """
    last = _log(workspace_root).tail()
    return last is not None and is_terminal(last.type)


def clear_log(writer: WorkspaceLock) -> None:
    """Remove the progress event log. Idempotent.

    Called on a fresh evolve boot (and crash-resume reconciliation) so a
    new invocation's ``seq`` starts from ``1`` rather than inheriting a
    prior run's tail — a stale ``seq`` must never read as live progress.
    """
    writer.progress_log.clear()


__all__ = [
    "LOOP_START",
    "ROUND_START",
    "PROPOSE",
    "EPISODE_SETTLED",
    "TOURNAMENT_START",
    "UNIT_SETTLED",
    "TOURNAMENT_SETTLE",
    "PROMOTE",
    "REJECT",
    "SETTLED",
    "STOPPED",
    "is_terminal",
    "append_progress",
    "bind_transition_recorder",
    "reset_transition_recorder",
    "record_transition",
    "tail",
    "tail_seq",
    "tail_is_terminal",
    "clear_log",
]
