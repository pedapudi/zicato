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
round start, propose, apply, tournament start / settle, gate,
promote / reject. The log's monotonic ``seq`` therefore advances only on
real progress, never on a timer, so it is the TRUE liveness signal:

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

from pathlib import Path

from zicato.runtime._storage import progress_log_key
from zicato.runtime.channel import Event, EventLog
from zicato.runtime.paths import ensure_runtime_dirs
from zicato.storage import workspace_backend

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
#: The challenger patch set was applied into a fresh snapshot.
APPLY = "Apply"
#: The tournament for this round started executing.
TOURNAMENT_START = "TournamentStart"
#: The tournament settled (a winner / decision is resolved).
TOURNAMENT_SETTLE = "TournamentSettle"
#: The gate evaluated the settled decision (promote-margin check).
GATE = "Gate"
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


def append_progress(workspace_root: Path, type: str, payload: object | None = None) -> int:
    """Append one progress transition and return the new tail ``seq``.

    The single producer (the evolve loop) calls this on each genuine
    transition; ``seq`` advances by exactly one per call. The returned
    ``seq`` is what the caller stamps into the heartbeat (and what the
    dashboard surfaces) — the machine-readable liveness cursor. Best-effort
    callers can ignore the return value.
    """
    ensure_runtime_dirs(workspace_root)
    return _log(workspace_root).append(type, payload).seq


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


def clear_log(workspace_root: Path) -> None:
    """Remove the progress event log. Idempotent.

    Called on a fresh evolve boot (and crash-resume reconciliation) so a
    new invocation's ``seq`` starts from ``1`` rather than inheriting a
    prior run's tail — a stale ``seq`` must never read as live progress.
    """
    workspace_backend(workspace_root, start=False).delete(progress_log_key())


__all__ = [
    "LOOP_START",
    "ROUND_START",
    "PROPOSE",
    "APPLY",
    "TOURNAMENT_START",
    "TOURNAMENT_SETTLE",
    "GATE",
    "PROMOTE",
    "REJECT",
    "SETTLED",
    "STOPPED",
    "is_terminal",
    "append_progress",
    "tail",
    "tail_seq",
    "tail_is_terminal",
    "clear_log",
]
