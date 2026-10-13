"""The queue of operator rounds for the evolve loop.

``zicato proposer submit`` validates one or more edited copies of
generations and queues them as one round: a targeted control command,
``control/candidate/<queue_id>``, whose JSON body is a :class:`QueuedRound`.
The submit takes no workspace lock, so it can run while an evolve loop runs.
The loop reads the queue at the start of each round's candidate step. The
oldest usable item fills the first slots of that round, one candidate per
slot; the proposer fills the other slots unless the item is exclusive.

An item leaves the queue through :func:`~zicato.runtime.control.consume_command`,
so the control log keeps its full body and the reason:

* the round could not use it (a stale epoch, an unreadable body, more
  candidates than slots, or no candidate that could be minted) — consumed
  at once, with the findings;
* its round settled — consumed by settlement, with the generations;
* a restarted round found every candidate minted into a settled
  generation — consumed then, because the settlement that should have
  consumed it was cut short.

An item with a candidate minted into a generation that is still pending
stays queued. If recovery resumes that generation, its settlement consumes
the item. If recovery discards the field, the item runs again.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zicato.core.types import OperatorProvenance
from zicato.runtime.control import (
    CMD_CANDIDATE_PREFIX,
    ControlCommand,
    consume_command,
    list_pending_commands,
    write_command,
)
from zicato.runtime.paths import control_log_dir

log = logging.getLogger("zicato.runtime.operator_queue")

#: Version of the queued-round body. A reader refuses any other value.
QUEUED_ROUND_FORMAT = 1

#: The ``source`` recorded in the control log for every consumption here.
CONSUMER_SOURCE = "evolve"

_PATCH_KEYS = frozenset({"mutation_id", "op", "new_content", "new_numeric", "new_enum"})
_PAYLOAD_KEY = {"replace": "new_content", "set_numeric": "new_numeric", "set_enum": "new_enum"}


def _check_patch(patch: Any) -> None:
    """Refuse a queued patch whose keys or value types the applier cannot take."""
    if not isinstance(patch, dict) or set(patch) != _PATCH_KEYS:
        raise ValueError(f"each queued patch must carry exactly {sorted(_PATCH_KEYS)}")
    if not isinstance(patch["mutation_id"], str) or not patch["mutation_id"]:
        raise ValueError("a queued patch mutation_id must be a non-empty string")
    payload_key = _PAYLOAD_KEY.get(patch["op"])
    if payload_key is None:
        raise ValueError(f"a queued patch op must be one of {sorted(_PAYLOAD_KEY)}")
    for key in ("new_content", "new_numeric", "new_enum"):
        value = patch[key]
        if key != payload_key:
            if value is not None:
                raise ValueError(f"a queued {patch['op']} patch cannot carry {key}")
        elif key == "new_numeric":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("a queued set_numeric patch needs a number")
        elif not isinstance(value, str):
            raise ValueError(f"a queued {patch['op']} patch needs a string {key}")


@dataclass(frozen=True, slots=True)
class QueuedCandidate:
    """One candidate of a queued round.

    ``patches`` are the operator's edits relative to ``based_on``, one
    ``replace`` / ``set_numeric`` / ``set_enum`` object per mutation point,
    without ids or rationale. ``hypothesis`` is the ``hypothesis`` object of
    the proposer's response schema. The round parses both again against the
    current champion, so the record keeps them as raw JSON values.
    """

    based_on: str
    hypothesis: Mapping[str, Any]
    patches: tuple[Mapping[str, Any], ...]

    def body(self) -> dict[str, Any]:
        """The JSON form of the candidate inside its round."""
        return {
            "based_on": self.based_on,
            "hypothesis": dict(self.hypothesis),
            "patches": [dict(patch) for patch in self.patches],
        }

    @classmethod
    def from_body(cls, body: Any) -> QueuedCandidate:
        """Decode one candidate. Raises :class:`ValueError` when it is malformed."""
        if not isinstance(body, dict) or set(body) != {"based_on", "hypothesis", "patches"}:
            raise ValueError("a queued candidate must carry based_on, hypothesis and patches")
        if not isinstance(body["based_on"], str) or not body["based_on"]:
            raise ValueError("a queued candidate based_on must be a non-empty string")
        if not isinstance(body["hypothesis"], dict):
            raise ValueError("a queued candidate hypothesis must be an object")
        patches = body["patches"]
        if not isinstance(patches, list) or not patches:
            raise ValueError("a queued candidate needs a non-empty list of patches")
        for patch in patches:
            _check_patch(patch)
        return cls(based_on=body["based_on"], hypothesis=body["hypothesis"], patches=tuple(patches))


@dataclass(frozen=True, slots=True)
class QueuedRound:
    """One round of operator candidates, as ``zicato proposer submit`` queued it.

    The candidates fill the round's first slots in order. ``exclusive`` asks
    the round to run them alone, with no proposal episodes.
    ``holdout_access`` is the author's statement of whether they could read
    holdout results, or ``None`` when they did not state it.
    """

    queue_id: str
    epoch_id: str
    author: str
    submitted_at: str
    candidates: tuple[QueuedCandidate, ...]
    exclusive: bool = False
    holdout_access: bool | None = None

    def body(self) -> dict[str, Any]:
        """The JSON body of the queue file."""
        return {
            "format": QUEUED_ROUND_FORMAT,
            "queue_id": self.queue_id,
            "epoch_id": self.epoch_id,
            "author": self.author,
            "submitted_at": self.submitted_at,
            "exclusive": self.exclusive,
            "holdout_access": self.holdout_access,
            "candidates": [candidate.body() for candidate in self.candidates],
        }

    @classmethod
    def from_body(cls, body: Any) -> QueuedRound:
        """Decode a queue file body. Raises :class:`ValueError` when it is malformed."""
        if not isinstance(body, dict):
            raise ValueError("a queued round must be a JSON object")
        if body.get("format") != QUEUED_ROUND_FORMAT:
            raise ValueError(
                f"queued round format {body.get('format')!r} is not {QUEUED_ROUND_FORMAT}"
            )
        for key in ("queue_id", "epoch_id", "author", "submitted_at"):
            if not isinstance(body.get(key), str) or not body[key]:
                raise ValueError(f"queued round {key} must be a non-empty string")
        if not isinstance(body.get("exclusive"), bool):
            raise ValueError("queued round exclusive must be a boolean")
        holdout_access = body.get("holdout_access")
        if holdout_access is not None and not isinstance(holdout_access, bool):
            raise ValueError("queued round holdout_access must be a boolean or null")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("a queued round needs a non-empty list of candidates")
        return cls(
            queue_id=body["queue_id"],
            epoch_id=body["epoch_id"],
            author=body["author"],
            submitted_at=body["submitted_at"],
            candidates=tuple(QueuedCandidate.from_body(c) for c in candidates),
            exclusive=body["exclusive"],
            holdout_access=holdout_access,
        )

    def provenance(
        self, candidate: QueuedCandidate, reverted_points: Sequence[str] = ()
    ) -> OperatorProvenance:
        """The provenance that the experiment minted from ``candidate`` records."""
        return OperatorProvenance(
            author=self.author,
            based_on=candidate.based_on,
            queue_id=self.queue_id,
            submitted_at=self.submitted_at,
            holdout_access=self.holdout_access,
            reverted_points=tuple(reverted_points),
        )


def new_queue_id() -> str:
    """A queue id that sorts by submit time: ``<UTC time to the nanosecond>-<6 hex>``.

    Nanoseconds keep two submits in one second in the order they were made;
    the hex suffix keeps two ids apart if the clock repeats a value.
    """
    now = time.time_ns()
    seconds = time.strftime("%Y%m%d%H%M%S", time.gmtime(now // 1_000_000_000))
    return f"{seconds}{now % 1_000_000_000:09d}-{uuid.uuid4().hex[:6]}"


def enqueue_operator_round(workspace_root: Path, queued: QueuedRound) -> Path:
    """Write one queued round atomically and return its path."""
    return write_command(
        workspace_root,
        ControlCommand(
            name=CMD_CANDIDATE_PREFIX,
            arg=queued.queue_id,
            payload=json.dumps(queued.body(), indent=2, sort_keys=True) + "\n",
        ),
    )


@dataclass(frozen=True, slots=True)
class PendingOperatorRound:
    """The queued round a round takes, and the command that holds it."""

    command: ControlCommand
    queued: QueuedRound


@dataclass(frozen=True, slots=True)
class MintedQueueItem:
    """A generation that a candidate of a queued round was minted into."""

    generation_id: str
    settled: bool


def _pending_candidate_commands(workspace_root: Path) -> list[ControlCommand]:
    """Every queued item, with its body as the payload so the control log keeps it.

    An item that cannot be read now (an ``OSError``) is left out and stays
    queued: the error can be temporary, and consuming the item would lose a
    valid round. Bytes that are not UTF-8 are malformed for good, so the
    item keeps an empty payload and is consumed as unreadable.
    """
    commands: list[ControlCommand] = []
    for cmd in list_pending_commands(workspace_root):
        if cmd.name != CMD_CANDIDATE_PREFIX:
            continue
        try:
            payload = cmd.file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            payload = ""
        except OSError as exc:
            log.warning("operator round %s cannot be read now (%s); it stays queued", cmd.arg, exc)
            continue
        commands.append(replace(cmd, payload=payload))
    return commands


def next_operator_round(
    workspace_root: Path,
    epoch_id: str,
    *,
    slots: int,
    minted: Callable[[], Mapping[str, Sequence[MintedQueueItem]]],
) -> PendingOperatorRound | None:
    """The oldest queued round that this epoch's next round can take.

    Items are read in queue-id order, which is submit order. On the way to
    the first usable item, this consumes the items that can never run: an
    unreadable body, an item for another epoch, an item with more candidates
    than the round's ``slots``, and an item whose candidates are all minted
    into settled generations. It skips, and leaves queued, an item with a
    candidate minted into a generation that is still pending. ``minted``
    maps each minted queue id to its generations. It is called only when an
    item is queued.
    """
    commands = _pending_candidate_commands(workspace_root)
    if not commands:
        return None
    minted_items = minted()

    def _consume(cmd: ControlCommand, reason: str) -> None:
        consume_command(workspace_root, cmd, source=CONSUMER_SOURCE, reason=reason)

    for cmd in commands:
        try:
            queued = QueuedRound.from_body(json.loads(cmd.payload))
        except ValueError as exc:
            _consume(cmd, f"unreadable operator round: {exc}")
            continue
        if queued.queue_id != cmd.arg:
            _consume(cmd, f"operator round file {cmd.arg!r} names queue id {queued.queue_id!r}")
            continue
        if queued.epoch_id != epoch_id:
            _consume(
                cmd,
                f"operator round for epoch {queued.epoch_id!r} cannot run in epoch "
                f"{epoch_id!r}; check out the current champion again",
            )
            continue
        items = minted_items.get(queued.queue_id, ())
        if items and all(item.settled for item in items):
            generations = ", ".join(item.generation_id for item in items)
            _consume(cmd, f"operator round already minted as {generations}")
            continue
        if items:
            log.info("operator round %s has a pending generation; it stays queued", cmd.arg)
            continue
        if len(queued.candidates) > slots:
            _consume(
                cmd,
                f"operator round has {len(queued.candidates)} candidates, but the round "
                f"has {slots} slot(s)",
            )
            continue
        return PendingOperatorRound(command=cmd, queued=queued)
    return None


@dataclass(frozen=True, slots=True)
class OperatorRoundRecord:
    """One submitted round, as the queue and the control log record it.

    ``queued`` is ``None`` when the body cannot be decoded. ``consumed_at``
    and ``reason`` are ``None`` while the item is still queued.
    """

    queue_id: str
    queued: QueuedRound | None
    consumed_at: str | None = None
    reason: str | None = None


def _decode(payload: str) -> QueuedRound | None:
    try:
        return QueuedRound.from_body(json.loads(payload))
    except ValueError:
        return None


def operator_round_records(workspace_root: Path) -> list[OperatorRoundRecord]:
    """Every submitted round of the workspace, in submit order. Read-only.

    A queued item comes from ``control/candidate/``; a consumed one from its
    audit record in ``control_log/``, which keeps the body and the reason.
    When a crash left both copies, the audit record wins: consumption writes
    it before it deletes the queue file.
    """
    records: dict[str, OperatorRoundRecord] = {
        cmd.arg: OperatorRoundRecord(queue_id=cmd.arg, queued=_decode(cmd.payload))
        for cmd in _pending_candidate_commands(workspace_root)
    }
    log_dir = control_log_dir(workspace_root)
    if log_dir.is_dir():
        for path in sorted(log_dir.glob(f"*_{CMD_CANDIDATE_PREFIX}_*.json")):
            try:
                audit = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(audit, dict) or audit.get("command") != CMD_CANDIDATE_PREFIX:
                continue
            queue_id = str(audit.get("arg") or "")
            if not queue_id:
                continue
            records[queue_id] = OperatorRoundRecord(
                queue_id=queue_id,
                queued=_decode(str(audit.get("payload") or "")),
                consumed_at=str(audit.get("consumed_at") or "") or None,
                reason=str(audit.get("reason") or ""),
            )
    return [records[queue_id] for queue_id in sorted(records)]


def consume_operator_round(workspace_root: Path, queue_id: str, *, reason: str) -> bool:
    """Consume the queued item ``queue_id`` with ``reason``; ``False`` when absent."""
    for cmd in _pending_candidate_commands(workspace_root):
        if cmd.arg == queue_id:
            consume_command(workspace_root, cmd, source=CONSUMER_SOURCE, reason=reason)
            return True
    return False


__all__ = [
    "CONSUMER_SOURCE",
    "QUEUED_ROUND_FORMAT",
    "MintedQueueItem",
    "OperatorRoundRecord",
    "PendingOperatorRound",
    "QueuedCandidate",
    "QueuedRound",
    "consume_operator_round",
    "enqueue_operator_round",
    "new_queue_id",
    "next_operator_round",
    "operator_round_records",
]
