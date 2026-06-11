"""Consume operator control commands at the evolve loop's safe points.

The dashboard (and CLI / a bare ``touch``) *produce* control commands under
``.zicato/runtime/control/`` via :mod:`zicato.runtime.control`. For a long
time nothing *consumed* them — the pause / skip / promote / reject / brief
buttons wrote files that no process ever read (RUNTIME-V2.md Phase 2 calls
this "the dead producer-consumer").

This module is the consumer. It is deliberately thin: the claim-once + audit
archive mechanics already live in :mod:`zicato.runtime.control`
(:func:`~zicato.runtime.control.list_pending_commands` +
:func:`~zicato.runtime.control.consume_command`, which atomically moves a
command file into ``control_log/`` with a JSON sidecar). What this module
adds is the *evolve-loop semantics* — what each command MEANS and the only
points at which it is safe to act on it.

Safe points
-----------

The loop coordinates with running tournament writes only at quiescent
boundaries (RUNTIME-V2.md: "between rounds, between board units, at the gate
— never mid-tournament-write"):

* :func:`consume_between_rounds` — drained in :func:`evolve_n_rounds` before
  scheduling the next round. Handles ``pause_epoch`` (block scheduling until
  cleared), ``rubric_replacement`` (a contract edit → roll the epoch), and
  drains any stale ``skip_round`` flag (a between-rounds skip has no in-flight
  round to abort, so it is archived as a no-op).
* :func:`claim_skip_round` — checked at the top of ``evolve_once`` (a clean
  boundary, no tournament write in flight). A pending skip aborts the round
  cleanly, exactly like a wall-clock budget cut.
* :func:`claim_gate_override` — checked at the gate in ``evolve_once``, after
  the tournament settles but before the outcome is persisted. A
  ``promote/<gen>`` or ``reject/<gen>`` targeting the in-flight generation
  OVERRIDES the gate's verdict — and is recorded as an explicit operator
  override in the OutcomeRecord / journal, never silently.

Every consumed command is archived in ``control_log/`` (the audit trail)
with the consuming ``source`` and a ``reason`` — so an override that changed
a promotion decision is always reconstructable from the journal AND the
control log.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zicato.runtime.control import (
    CMD_PAUSE_EPOCH,
    CMD_PROMOTE_PREFIX,
    CMD_REJECT_PREFIX,
    CMD_RUBRIC_REPLACEMENT,
    CMD_SKIP_ROUND,
    ControlCommand,
    consume_command,
    is_paused,
    list_pending_commands,
)

log = logging.getLogger("zicato.runtime.control_consumer")

#: The injectable per-iteration delay :func:`block_while_paused` uses (so
#: tests do not actually sleep). Defaults to ``time.sleep`` at the call site.
_Sleep = Callable[[float], None]

#: The ``source`` stamped on every audit-log record this consumer writes, so
#: the control_log distinguishes commands the orchestrator acted on from a
#: manual ``consume_command`` (the test/CLI default ``"dashboard"``).
CONSUMER_SOURCE = "orchestrator"


# ---------------------------------------------------------------------------
# Result types — what a safe-point consume tells the loop to do.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateOverride:
    """An operator's force-promote / force-reject of the in-flight generation.

    Fields
    ------
    decision:
        ``"promoted"`` or ``"rejected"`` — the verdict the operator forced,
        overriding the gate.
    generation_id:
        The generation the command targeted (its ``arg``). The loop only
        applies an override whose target matches the round's in-flight
        generation; a stale override for a different generation is left
        pending (not consumed) so it cannot silently mis-fire.
    reason:
        The freeform reason recorded in the audit log + journal.
    """

    decision: str
    generation_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RubricReplacement:
    """A pending ``rubric_replacement`` payload — a contract edit.

    The new proposer-brief text the operator submitted. The loop writes it
    to the live brief and lets the contract-hash auto-epoching roll the
    epoch (a brief is part of the evaluation contract — a silent in-place
    patch would make pre- and post-edit generations falsely comparable).
    """

    payload: str
    reason: str


# ---------------------------------------------------------------------------
# Reason extraction
# ---------------------------------------------------------------------------


def _flag_reason(cmd: ControlCommand) -> str:
    """Extract the operator's ``reason`` from a flag command's file body.

    The dashboard writes flag files (``pause_epoch`` / ``skip_round``) and
    targeted files (``promote/<gen>`` / ``reject/<gen>``) with a small JSON
    body — ``{"reason": ..., "ts": ...}`` for pause/skip, ``{"generation_id":
    ..., "ts": ...}`` for promote/reject. :func:`list_pending_commands` only
    parses the payload for ``rubric_replacement``; for the flag/targeted
    commands the body is read here so the operator's reason rides into the
    audit log + journal. An empty / non-JSON / reason-less body yields ``""``
    (a hand-``touch``ed flag has no reason — that is fine).
    """
    path = cmd.file_path
    if path == Path() or not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # A non-JSON body (e.g. a hand-written note) is taken verbatim.
        return raw
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        return reason if isinstance(reason, str) else ""
    return ""


# ---------------------------------------------------------------------------
# pause_epoch — block scheduling until cleared
# ---------------------------------------------------------------------------


def block_while_paused(
    workspace_root: Path,
    *,
    sleep: _Sleep | None = None,
    poll_interval_s: float = 1.0,
    max_polls: int | None = None,
) -> int:
    """Block scheduling while the ``pause_epoch`` flag is present.

    Polls :func:`~zicato.runtime.control.is_paused` and sleeps until the
    operator clears the flag (the dashboard's resume gesture / a manual
    ``rm``). Returns the number of poll iterations spent paused (``0`` when
    the flag was absent — the common, never-paused case). When the pause
    is finally lifted, the pause episode is archived once in ``control_log/``
    so the audit trail records that scheduling WAS held (and the operator's
    reason for it), never silently.

    ``sleep`` is an injectable per-iteration delay (so tests do not actually
    sleep); it defaults to :func:`time.sleep`. ``max_polls`` caps the wait
    (``None`` = wait indefinitely, the production behaviour) — tests pass a
    finite cap with a self-clearing ``sleep`` so the loop terminates.
    """
    if not is_paused(workspace_root):
        return 0

    import time as _time  # noqa: PLC0415 — only needed on the paused path

    do_sleep = sleep if sleep is not None else (lambda _s: _time.sleep(_s))

    # Capture the pausing operator's reason BEFORE we start polling — the
    # flag file (and its reason body) is deleted by the resume gesture.
    pause_cmd: ControlCommand | None = None
    for cmd in list_pending_commands(workspace_root):
        if cmd.name == CMD_PAUSE_EPOCH:
            pause_cmd = cmd
            break
    reason = _flag_reason(pause_cmd) if pause_cmd is not None else ""

    polls = 0
    while is_paused(workspace_root):
        log.info(
            "evolve: pause_epoch flag present — scheduling held (poll %d)%s",
            polls + 1,
            f": {reason}" if reason else "",
        )
        do_sleep(poll_interval_s)
        polls += 1
        if max_polls is not None and polls >= max_polls:
            break

    # The flag cleared (operator resumed) — archive the pause episode once.
    # The source file is already gone, so this records intent only (the
    # control_log is the authoritative trail). We synthesise a flag command
    # so the archive carries the right name even though the file vanished.
    consume_command(
        workspace_root,
        pause_cmd if pause_cmd is not None else ControlCommand(name=CMD_PAUSE_EPOCH),
        source=CONSUMER_SOURCE,
        reason=(
            f"pause held for {polls} poll(s)" + (f"; operator reason: {reason}" if reason else "")
        ),
    )
    return polls


# ---------------------------------------------------------------------------
# skip_round — abort the current round cleanly, like a budget cut
# ---------------------------------------------------------------------------


def claim_skip_round(workspace_root: Path) -> str | None:
    """Claim a pending ``skip_round`` flag, or ``None`` when absent.

    Called at the top of ``evolve_once`` (a clean safe point — no tournament
    write is in flight yet). When a skip is pending it is consumed (archived
    to ``control_log/``) and the operator's reason is returned, signalling
    the round should abort cleanly exactly like a wall-clock budget cut.
    Returns ``None`` when no skip is queued (the common case), leaving the
    round to run normally.
    """
    skip_cmd: ControlCommand | None = None
    for cmd in list_pending_commands(workspace_root):
        if cmd.name == CMD_SKIP_ROUND:
            skip_cmd = cmd
            break
    if skip_cmd is None:
        return None
    reason = _flag_reason(skip_cmd)
    consume_command(
        workspace_root,
        skip_cmd,
        source=CONSUMER_SOURCE,
        reason=reason or "operator skip_round",
    )
    return reason or ""


# ---------------------------------------------------------------------------
# promote / reject — override the gate for the in-flight generation
# ---------------------------------------------------------------------------


def claim_gate_override(workspace_root: Path, generation_id: str) -> GateOverride | None:
    """Claim a promote/reject override TARGETING ``generation_id``, or ``None``.

    Called at the gate in ``evolve_once`` after the tournament settles. If an
    operator queued ``promote/<generation_id>`` or ``reject/<generation_id>``
    for the generation this round just evaluated, it is consumed (archived)
    and returned so the caller can OVERRIDE the gate's verdict and record the
    override explicitly in the OutcomeRecord / journal.

    Only a command whose ``arg`` matches the in-flight ``generation_id`` is
    claimed — a stale override aimed at a different generation is left
    pending (it would mis-fire on the wrong round). When both a promote and a
    reject target the same generation (an operator changed their mind), the
    promote is honoured and the reject is also drained (archived) so it
    cannot fire on a later round; this is deterministic and recorded.
    """
    promote_cmd: ControlCommand | None = None
    reject_cmd: ControlCommand | None = None
    for cmd in list_pending_commands(workspace_root):
        if cmd.arg != generation_id:
            continue
        if cmd.name == CMD_PROMOTE_PREFIX:
            promote_cmd = cmd
        elif cmd.name == CMD_REJECT_PREFIX:
            reject_cmd = cmd

    if promote_cmd is None and reject_cmd is None:
        return None

    # Promote wins a promote+reject tie; drain the loser so it cannot fire
    # on a later round.
    chosen = promote_cmd if promote_cmd is not None else reject_cmd
    assert chosen is not None  # noqa: S101 — guarded by the early return above
    decision = "promoted" if chosen.name == CMD_PROMOTE_PREFIX else "rejected"
    reason = _flag_reason(chosen) or f"operator {decision} override"

    consume_command(
        workspace_root,
        chosen,
        source=CONSUMER_SOURCE,
        reason=reason,
    )
    if promote_cmd is not None and reject_cmd is not None:
        # Both queued — the promote was honoured above; drain the reject too
        # (recorded as superseded) so a stale reject cannot re-fire.
        consume_command(
            workspace_root,
            reject_cmd,
            source=CONSUMER_SOURCE,
            reason="superseded by a promote override for the same generation",
        )

    return GateOverride(decision=decision, generation_id=generation_id, reason=reason)


# ---------------------------------------------------------------------------
# rubric_replacement — a contract edit that rolls the epoch
# ---------------------------------------------------------------------------


def claim_rubric_replacement(workspace_root: Path) -> RubricReplacement | None:
    """Claim a pending ``rubric_replacement`` payload, or ``None`` when absent.

    Called between rounds. The proposer brief is part of the evaluation
    contract, so a replacement is NOT a silent in-place patch — the caller
    writes the payload to the live brief and lets contract-hash
    auto-epoching roll the epoch. The command is consumed (archived) here;
    the audit log preserves the payload (the new brief text) verbatim.
    Returns ``None`` when nothing is queued.
    """
    rubric_cmd: ControlCommand | None = None
    for cmd in list_pending_commands(workspace_root):
        if cmd.name == CMD_RUBRIC_REPLACEMENT:
            rubric_cmd = cmd
            break
    if rubric_cmd is None:
        return None
    payload = rubric_cmd.payload
    consume_command(
        workspace_root,
        rubric_cmd,
        source=CONSUMER_SOURCE,
        reason="rubric replacement — rolling the epoch (contract edit)",
    )
    return RubricReplacement(payload=payload, reason="operator rubric replacement")


__all__ = [
    "CONSUMER_SOURCE",
    "GateOverride",
    "RubricReplacement",
    "block_while_paused",
    "claim_skip_round",
    "claim_gate_override",
    "claim_rubric_replacement",
]
