"""The Channel abstraction — one cross-process exchange shape, two flavours.

zicato's separated processes (orchestrator, dashboard, Rust supervisor,
subprocess workers) coordinate *only through the filesystem*. A mutable
snapshot file with several writers and ad-hoc consumers is the wrong shape
for that: it invites lost-update races, torn writes, live-versus-settled
disagreement, and watchdog false-positives.

This module supplies the *channel abstraction* those exchanges are built
on, in two shapes, both over the storage ``_atomic`` seam. The
tournament live state (:mod:`zicato.runtime.tournament_log`) and the
orchestrator progress signal (:mod:`zicato.runtime.progress_log`) are
event logs on top of it; the operator control protocol
(:mod:`zicato.runtime.control`) is a command queue.

Design notes for both shapes live in ``docs/design/RUNTIME-V2.md``.

The two shapes
--------------

:class:`EventLog`
    Append-only, **single-writer**. Each entry is a typed :class:`Event`
    record carrying a monotonic ``seq`` and a ``ts``. ``append(type,
    payload)`` is one atomic append; ``read(from_seq)`` returns the tail
    of the log past a consumer's cursor; ``tail()`` returns the last
    event. Consumers hold a ``seq`` cursor and fold the log into a view —
    "settled" is just the terminal event. There is one source of truth
    (the log) and the view is derived, consistent by construction.

:class:`CommandQueue`
    Many-writer ``enqueue``, single-consumer **claim-once**. Each
    ``enqueue`` writes a uniquely-keyed pending record; ``claim()``
    atomically moves the oldest pending record into an archive so the
    command fires for exactly one consumer even when several poll
    concurrently. The claim-once guarantee rides on
    :func:`zicato.storage._atomic.atomic_claim` (an :func:`os.rename`,
    which succeeds for one racing caller and ``FileNotFoundError``\\s for
    the rest).

Why a ``seq`` (and not just the ``ts``)
---------------------------------------
The timestamp is second-precision (:func:`zicato.util.iso_time.now_iso`)
and is for humans; two events in the same second share a ``ts``. The
``seq`` is the machine-readable order and cursor: strictly increasing,
gap-free, assigned by the single writer from the current tail. It is what
makes SSE ordering correct, digest-gating principled (re-render iff ``seq``
advanced), and a ``seq``-as-liveness watchdog possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from zicato.storage import atomic_claim
from zicato.storage.base import StorageBackend
from zicato.util.iso_time import now_iso as _utc_now_iso

# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One entry in an :class:`EventLog` — a typed, sequenced record.

    Fields
    ------
    seq:
        Monotonic sequence number, assigned by the single writer. The
        first event in a log is ``seq == 1`` and every subsequent append
        is exactly ``+1`` — strictly increasing and gap-free, so a
        consumer can cursor on it (``read(from_seq=last_seen)``) and a
        watchdog can ask "is ``seq`` advancing?".
    ts:
        ISO-8601 UTC timestamp (second precision, ``Z`` suffix) of the
        append. For humans and journals; NOT the ordering key — that is
        :attr:`seq`. Two events appended in the same wall-clock second
        share a ``ts`` but never a ``seq``.
    type:
        The record kind — a free-form short string the producer and
        consumer agree on (e.g. ``"MatchupStarted"``). The channel does
        not interpret it; folding logic does.
    payload:
        The record body — any JSON-serialisable value. The channel stores
        and returns it verbatim (round-tripped through the backend's JSON
        seam, so it lands JSON-clean).
    """

    seq: int
    ts: str
    type: str
    payload: Any = None

    def to_record(self) -> dict[str, Any]:
        """Serialise to the on-disk JSONL record shape."""
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "payload": self.payload}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Event:
        """Reconstruct an :class:`Event` from a decoded JSONL record."""
        return cls(
            seq=record["seq"],
            ts=record["ts"],
            type=record["type"],
            payload=record.get("payload"),
        )


class EventLog:
    """An append-only, single-writer, monotonic-``seq`` event log.

    Backed by a JSONL stream at one storage ``key`` (via
    :meth:`StorageBackend.append_jsonl` / :meth:`~StorageBackend.read_jsonl`).
    The append-only JSONL shape is the same one telemetry already uses; an
    ``EventLog`` adds the typed-record + monotonic-``seq`` contract on top.

    Single-writer contract
    -----------------------
    Exactly one process appends to a given log. The ``seq`` is derived from
    the current tail on each append, so a *second* concurrent writer would
    assign a duplicate ``seq`` — the abstraction does not defend against
    that because the migration targets each have a single producer by
    design (the runner is the only writer of the tournament log, etc.).
    The single-writer rule is the precondition that makes the gap-free
    ``seq`` correct.

    Reads are unrestricted and lock-free: any number of consumers can
    :meth:`read` / :meth:`tail` concurrently with the writer. A reader sees
    a prefix of the appended events — never a partial line — because each
    append writes one complete ``\\n``-terminated line.
    """

    def __init__(self, backend: StorageBackend, key: str) -> None:
        """Bind a log to ``key`` on ``backend``.

        No I/O happens here — the stream is created lazily on first
        :meth:`append`, and reads tolerate an absent stream (empty log).
        """
        self._backend = backend
        self._key = key

    @property
    def key(self) -> str:
        """The storage key the log's JSONL stream lives at."""
        return self._key

    def append(self, type: str, payload: Any = None) -> Event:
        """Append one event and return it with its assigned ``seq`` + ``ts``.

        The ``seq`` is ``tail().seq + 1`` (or ``1`` for the first event),
        read from the current stream tail — so appends are strictly
        increasing and gap-free under the single-writer contract. The
        append itself is one :meth:`StorageBackend.append_jsonl` call: a
        single complete line, so a concurrent reader never observes a
        half-written event.
        """
        last = self.tail()
        seq = 1 if last is None else last.seq + 1
        event = Event(seq=seq, ts=_utc_now_iso(), type=type, payload=payload)
        self._backend.append_jsonl(self._key, event.to_record())
        return event

    def read(self, from_seq: int = 0) -> list[Event]:
        """Return every event with ``seq > from_seq``, in append order.

        ``from_seq`` is a consumer cursor: pass the ``seq`` of the last
        event already folded into your view and get back only what is new
        (``from_seq=0``, the default, returns the whole log). The result is
        in append order, which — under the single-writer contract — is
        ``seq`` order.
        """
        out: list[Event] = []
        for record in self._backend.read_jsonl(self._key):
            event = Event.from_record(record)
            if event.seq > from_seq:
                out.append(event)
        return out

    def tail(self) -> Event | None:
        """Return the last appended event, or ``None`` if the log is empty.

        Used internally to derive the next ``seq`` and externally as the
        "current state" of an event-sourced view (the terminal event *is*
        the settled state).
        """
        last: Event | None = None
        for record in self._backend.read_jsonl(self._key):
            last = Event.from_record(record)
        return last


# ---------------------------------------------------------------------------
# CommandQueue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Command:
    """One entry in a :class:`CommandQueue` — a typed, claimable record.

    Fields
    ------
    id:
        The unique id of this enqueued command — an ordinal prefix (FIFO
        ordering) plus a uuid (collision-free across many concurrent
        enqueuers). It names the pending record and, after a claim, the
        archive record.
    type:
        The command kind (e.g. ``"pause_epoch"``). The queue does not
        interpret it; the consumer dispatches on it.
    payload:
        The command body — any JSON-serialisable value (a target run id, a
        replacement rubric, ...). Empty/``None`` for argument-less flags.
    ts:
        ISO-8601 UTC timestamp of the enqueue.
    """

    id: str
    type: str
    payload: Any = None
    ts: str = ""

    def to_record(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON record shape."""
        return {"id": self.id, "type": self.type, "payload": self.payload, "ts": self.ts}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Command:
        """Reconstruct a :class:`Command` from a decoded JSON record."""
        return cls(
            id=record["id"],
            type=record["type"],
            payload=record.get("payload"),
            ts=record.get("ts", ""),
        )


class CommandQueue:
    """A many-writer enqueue / single-consumer **claim-once** queue.

    Each command is its own keyed JSON record under a ``pending/`` prefix;
    a claim atomically moves the record into an ``archive/`` prefix. Two
    layout properties matter:

    * **Many-writer enqueue.** Every ``enqueue`` writes a *distinct* key
      (ordinal + uuid), so concurrent producers never collide and never
      overwrite each other — there is no shared mutable file to race on.
    * **Claim-once.** :meth:`claim` resolves the oldest pending record to
      its concrete path and moves it via
      :func:`zicato.storage._atomic.atomic_claim` (an :func:`os.rename`).
      The rename succeeds for exactly one racing consumer; the rest see
      the record already gone and move on to the next. So each command is
      delivered to one and only one consumer even when several poll the
      queue at once.

    Unlike :class:`EventLog`, this class is **file-backed specifically**:
    the claim-once guarantee rides on filesystem ``rename`` atomicity, so
    it is rooted at a workspace directory and resolves keys to concrete
    paths for the atomic move. (The enqueue + archive *records* still go
    through the storage atomic-write seam.)

    FIFO is best-effort: pending records sort by their ordinal prefix, so a
    single consumer drains them oldest-first, but the queue's contract is
    *each command claimed exactly once* rather than strict global ordering across
    racing enqueuers.
    """

    #: Subdirectory (under the queue root) holding not-yet-claimed commands.
    PENDING = "pending"
    #: Subdirectory (under the queue root) holding claimed (archived) commands.
    ARCHIVE = "archive"

    def __init__(self, backend: StorageBackend, root: Path, *, prefix: str) -> None:
        """Bind a queue to ``prefix`` on ``backend``, rooted at ``root``.

        ``backend`` carries the enqueue/archive *record* writes (atomic
        JSON). ``root`` is the concrete workspace directory the queue's
        files live under and is what the claim-once :func:`os.rename`
        operates on — it MUST be the same directory tree ``backend``
        resolves ``prefix`` keys into (the file backend rooted at the
        workspace). ``prefix`` is the queue's logical namespace, e.g.
        ``"runtime/control_q"``; the queue keeps its ``pending/`` and
        ``archive/`` records beneath it.
        """
        self._backend = backend
        self._root = Path(root)
        self._prefix = prefix.strip("/")
        #: Monotonic per-process ordinal so a single enqueuer's commands sort
        #: in submission order; combined with a uuid for cross-process
        #: collision-freedom.
        self._ordinal = 0

    @property
    def prefix(self) -> str:
        """The queue's logical key namespace."""
        return self._prefix

    def _pending_key(self, command_id: str) -> str:
        return f"{self._prefix}/{self.PENDING}/{command_id}.json"

    def _archive_key(self, command_id: str) -> str:
        return f"{self._prefix}/{self.ARCHIVE}/{command_id}.json"

    def _key_path(self, key: str) -> Path:
        """Resolve a logical key to its concrete path under the queue root."""
        return self._root / key

    def enqueue(self, type: str, payload: Any = None) -> Command:
        """Enqueue a command and return it with its assigned id + ``ts``.

        Safe to call from many writers concurrently: the id is an ordinal
        prefix plus a uuid, so two enqueuers never produce the same key and
        the single :meth:`StorageBackend.write_json` is atomic. The ordinal
        gives a single enqueuer's commands FIFO order; the uuid keeps ids
        globally unique across processes.
        """
        self._ordinal += 1
        command_id = f"{self._ordinal:012d}-{uuid4().hex}"
        command = Command(id=command_id, type=type, payload=payload, ts=_utc_now_iso())
        self._backend.write_json(self._pending_key(command_id), command.to_record())
        return command

    def pending(self) -> list[Command]:
        """Return the not-yet-claimed commands, oldest-first.

        A read-only peek for the dashboard and tests — it does NOT claim.
        Records sort by key (ordinal prefix first), giving best-effort FIFO
        order. A record that vanishes mid-listing (claimed by a racing
        consumer between the list and the read) is simply skipped.
        """
        out: list[Command] = []
        for key in self._backend.list_keys(f"{self._prefix}/{self.PENDING}"):
            record = self._backend.read_json(key)
            if record is None:
                continue  # claimed out from under us between list and read
            out.append(Command.from_record(record))
        return out

    def claim(self) -> Command | None:
        """Claim the oldest pending command exactly once, or ``None``.

        Single-consumer contract in the common case, but **safe under
        concurrent consumers**: each candidate is moved from ``pending/`` to
        ``archive/`` via :func:`zicato.storage._atomic.atomic_claim`. That
        move succeeds for exactly one racing consumer; a consumer that
        loses the race (the record was already claimed) advances to the
        next candidate rather than returning a duplicate. Returns ``None``
        only when the queue is genuinely drained.

        The claimed record is preserved in ``archive/`` (an audit trail of
        what fired, mirroring the control protocol's ``control_log/``), and
        the in-memory :class:`Command` is reconstructed from the archived
        copy so the return value reflects exactly what was claimed.
        """
        for key in self._backend.list_keys(f"{self._prefix}/{self.PENDING}"):
            command_id = Path(key).stem
            src = self._key_path(key)
            dst = self._key_path(self._archive_key(command_id))
            if not atomic_claim(src, dst):
                continue  # another consumer claimed this one first
            record = self._backend.read_json(self._archive_key(command_id))
            if record is None:
                # Should not happen — we just moved it there — but stay
                # defensive rather than return a torn command.
                continue
            return Command.from_record(record)
        return None

    def archived(self) -> list[Command]:
        """Return every claimed (archived) command, oldest-first.

        The audit trail of commands that have fired — used by the dashboard
        and tests to confirm claim-once delivery. Ordered by key (ordinal
        prefix), so archived commands appear in enqueue order.
        """
        out: list[Command] = []
        for key in self._backend.list_keys(f"{self._prefix}/{self.ARCHIVE}"):
            record = self._backend.read_json(key)
            if record is None:
                continue
            out.append(Command.from_record(record))
        return out


__all__ = [
    "Event",
    "EventLog",
    "Command",
    "CommandQueue",
]
